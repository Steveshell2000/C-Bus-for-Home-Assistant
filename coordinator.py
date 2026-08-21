import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_TRANSITION, MAX_TRANSITION
from .protocol import (
    LIGHT_OFF,
    LIGHT_ON,
    build_lighting_command,
    calculate_cbus_checksum,
    interpolate_ramp_level,
    parse_lighting_event,
    ramp_command_for_transition,
    ramp_duration_seconds,
)

_LOGGER = logging.getLogger(__name__)

RAMP_UPDATE_INTERVAL = 0.25


class CBusCoordinator(DataUpdateCoordinator):
    """Handles persistent connection and lifecycle status sync for C-Bus CNIs."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        lighting_map: dict,
        default_transition: float | int = DEFAULT_TRANSITION,
    ):
        super().__init__(hass, _LOGGER, name="cbus_native_coordinator")
        self.host = host
        self.port = port
        self.lighting_map = lighting_map
        self.default_transition = min(
            MAX_TRANSITION, max(0.0, float(default_transition))
        )
        self.reader = None
        self.writer = None
        self.is_connected = False
        self._intentional_disconnect = False
        self._tasks = []

        # Internal state cache for all tracked group addresses.
        self.states = {ga: {"state": False, "brightness": 0} for ga in lighting_map}

        # C-Bus reports a ramp's destination and full-scale rate, but not each
        # intermediate level. Track active ramps so HA slider state follows the
        # physical fade instead of jumping directly to the terminal level.
        self._ramp_tasks: dict[int, asyncio.Task] = {}
        self._active_ramps: dict[int, dict[str, float | int]] = {}

    async def connect(self):
        """Establish persistent asynchronous connection with the gateway."""
        if self.is_connected:
            return

        self._intentional_disconnect = False
        try:
            _LOGGER.info(
                "C-Bus Connecting: Opening TCP socket to %s:%s", self.host, self.port
            )
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True

            # Reset CNI buffers.
            self.writer.write(b"\\0500000000g\r")
            await self.writer.drain()
            await asyncio.sleep(1.0)

            # Enable local socket monitoring options.
            init_cmds = [
                "\\05140021C6g\r",  # Monitor Mode ON
                "\\05140023C4g\r",  # Smart Mode & System IO ON
                "\\05140038F3g\r",  # Enable MMI reporting for Application 56
                "\\05090000F8g\r",  # Request initial full MMI block status
            ]
            for cmd in init_cmds:
                self.writer.write(cmd.encode("ascii"))
                await self.writer.drain()
                await asyncio.sleep(0.3)

            # Spin up managed background loops.
            self._tasks.append(self.hass.loop.create_task(self._listen_loop()))
            self._tasks.append(self.hass.loop.create_task(self._heartbeat_loop()))
            self._tasks.append(self.hass.loop.create_task(self._sync_loop()))
            _LOGGER.info(
                "C-Bus Connection: Fully initialized and background loops started."
            )

        except Exception as err:
            _LOGGER.error(
                "C-Bus Connection: Failed to establish ASCII link to gateway: %s", err
            )
            self.is_connected = False
            if not self._intentional_disconnect:
                self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    async def _reconnect_later(self):
        """Wait safely and then trigger a reconnection loop."""
        if self._intentional_disconnect:
            return
        await asyncio.sleep(5)
        if not self._intentional_disconnect:
            await self.connect()

    async def _sync_loop(self):
        """Paced query loop to safely sync all group states on startup."""
        _LOGGER.info(
            "C-Bus Sync: Initiating paced startup status poll for %d groups...",
            len(self.lighting_map),
        )
        await asyncio.sleep(3)

        for ga in self.lighting_map:
            if not self.is_connected or self._intentional_disconnect:
                break
            try:
                # Command 03 is the standard CAL Status Request for a specific GA.
                base_hex = f"05380003{ga:02X}"
                cmd = f"\\{base_hex}{calculate_cbus_checksum(base_hex)}g\r"
                self.writer.write(cmd.encode("ascii"))
                await self.writer.drain()
                _LOGGER.debug("C-Bus Sync: Polled GA %d (Hex: %s)", ga, base_hex)
                await asyncio.sleep(0.15)
            except Exception as err:
                _LOGGER.error("C-Bus Sync: Poll aborted for GA %d: %s", ga, err)
                break

        _LOGGER.info("C-Bus Sync: Startup polling sequence complete.")

    async def disconnect(self):
        """Gracefully close sockets and cancel active tasks."""
        self._intentional_disconnect = True
        self.is_connected = False

        _LOGGER.info("C-Bus Disconnecting: Cancelling active loop tasks...")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()

        for task in self._ramp_tasks.values():
            if not task.done():
                task.cancel()
        self._ramp_tasks.clear()
        self._active_ramps.clear()

        if self.writer:
            try:
                _LOGGER.info(
                    "C-Bus Disconnecting: Terminating network socket stream cleanly..."
                )
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=3.0)
            except Exception as err:
                _LOGGER.debug(
                    "C-Bus Disconnecting: Exception while closing write channel: %s",
                    err,
                )
            finally:
                self.writer = None
                self.reader = None
        _LOGGER.info("C-Bus Disconnecting: Disconnected completely.")

    async def _heartbeat_loop(self):
        """Send a monitor status query as a heartbeat to avoid CNI timeouts."""
        while self.is_connected and not self._intentional_disconnect:
            try:
                if self.writer:
                    self.writer.write(b"\\05090038F3g\r")
                    await self.writer.drain()
            except Exception as err:
                _LOGGER.warning("C-Bus Heartbeat: Keep-alive write failure: %s", err)
                self.is_connected = False
                break
            await asyncio.sleep(30)

        if not self.is_connected and not self._intentional_disconnect:
            await self.disconnect()
            self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    async def _listen_loop(self):
        """Asynchronously stream data packets and direct to parsing handlers."""
        buffer = ""
        while self.is_connected and not self._intentional_disconnect:
            try:
                if not self.reader:
                    break
                data = await self.reader.read(1024)
                if not data:
                    _LOGGER.warning(
                        "C-Bus Listener: Connection lost on remote socket interface."
                    )
                    break

                buffer += data.decode("ascii", errors="ignore")
                while "\r" in buffer and not self._intentional_disconnect:
                    line, buffer = buffer.split("\r", 1)
                    line = line.strip().upper()

                    if not line or line.startswith("!G"):
                        continue

                    _LOGGER.debug("C-Bus Listener: Raw line received: %s", line)
                    try:
                        if self._process_event_update(line):
                            continue
                        if any(
                            prefix in line for prefix in ["F638", "8638", "D838", "D638"]
                        ):
                            self._process_mmi_response(line)
                    except Exception as parse_err:
                        _LOGGER.debug(
                            "C-Bus Listener: Parsing exception on line %s: %s",
                            line,
                            parse_err,
                        )
            except Exception as err:
                _LOGGER.error("C-Bus Listener: General socket execution error: %s", err)
                buffer = ""
                await asyncio.sleep(2)
                break

        self.is_connected = False
        if not self._intentional_disconnect:
            self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    def _process_mmi_response(self, line):
        """Parse MMI (Monitor Message Interface) block response data."""
        try:
            mmi_marker = next(
                (p for p in ["F638", "8638", "D838", "D638"] if p in line), None
            )
            if not mmi_marker:
                return

            mmi_idx = line.find(mmi_marker)
            start_ga = int(line[mmi_idx + 4 : mmi_idx + 6], 16)
            idx_data = mmi_idx + 6
            num_bytes = min((len(line) - idx_data - 2) // 2, 22)

            state_updated = False
            for index in range(num_bytes):
                byte_hex = line[idx_data + index * 2 : idx_data + index * 2 + 2]
                if len(byte_hex) < 2:
                    break
                byte_data = int(byte_hex, 16)
                for ga_offset in range(4):
                    ga = start_ga + (index * 4) + ga_offset
                    if ga not in self.lighting_map or ga in self._active_ramps:
                        continue

                    shift = ga_offset * 2
                    state_val = (byte_data >> shift) & 0x03
                    is_on = state_val in (0x01, 0x03)

                    current_brightness = self.states[ga].get("brightness", 0)
                    if is_on:
                        new_brightness = (
                            current_brightness if current_brightness > 0 else 255
                        )
                    else:
                        new_brightness = 0

                    self.states[ga].update(
                        {"state": is_on, "brightness": new_brightness}
                    )
                    state_updated = True

            if state_updated:
                self.async_set_updated_data(dict(self.states))
                _LOGGER.debug(
                    "C-Bus MMI Sync: Handled block starting at GA %d", start_ga
                )
        except Exception as err:
            _LOGGER.error("C-Bus MMI: Parsing failure: %s", err)

    def _process_event_update(self, line: str) -> bool:
        """Handle ON, OFF, ramp-to-level, and terminate-ramp telegrams."""
        event = parse_lighting_event(line)
        if event is None or event.group_address not in self.states:
            return False

        ga = event.group_address
        if event.command == "terminate":
            level = self._estimated_ramp_level(ga)
            self._cancel_ramp(ga)
            self._publish_level(ga, level)
            _LOGGER.info("C-Bus Event Sync: GA %d -> Ramp terminated at %d", ga, level)
            return True

        if event.command == "ramp":
            assert event.target_level is not None
            assert event.ramp_command is not None
            self._start_ramp(ga, event.target_level, event.ramp_command)
            _LOGGER.info(
                "C-Bus Event Sync: GA %d -> Ramp to %d at %.0f s full-scale rate",
                ga,
                event.target_level,
                event.ramp_rate_seconds,
            )
            return True

        assert event.target_level is not None
        self._cancel_ramp(ga)
        self._publish_level(ga, event.target_level)
        _LOGGER.info(
            "C-Bus Event Sync: GA %d -> State: %s, Brightness: %d",
            ga,
            "ON" if event.target_level else "OFF",
            event.target_level,
        )
        return True

    def _start_ramp(self, ga: int, target_level: int, command: int) -> None:
        """Track a C-Bus ramp and publish intermediate levels to Home Assistant."""
        start_level = self._estimated_ramp_level(ga)
        self._cancel_ramp(ga)
        duration = ramp_duration_seconds(command, start_level, target_level)

        if duration <= 0 or start_level == target_level:
            self._publish_level(ga, target_level)
            return

        started_at = self.hass.loop.time()
        self._active_ramps[ga] = {
            "start_level": start_level,
            "target_level": target_level,
            "started_at": started_at,
            "duration": duration,
        }
        task = self.hass.loop.create_task(self._animate_ramp(ga))
        self._ramp_tasks[ga] = task
        task.add_done_callback(
            lambda completed_task, group=ga: self._ramp_task_finished(
                group, completed_task
            )
        )

    async def _animate_ramp(self, ga: int) -> None:
        """Publish estimated ramp progress until the terminal level is reached."""
        last_level = self.states[ga].get("brightness", 0)
        while ga in self._active_ramps:
            ramp = self._active_ramps[ga]
            elapsed = self.hass.loop.time() - float(ramp["started_at"])
            duration = float(ramp["duration"])
            level = interpolate_ramp_level(
                int(ramp["start_level"]),
                int(ramp["target_level"]),
                elapsed,
                duration,
            )
            if level != last_level:
                self._publish_level(ga, level)
                last_level = level
            if elapsed >= duration:
                break
            await asyncio.sleep(min(RAMP_UPDATE_INTERVAL, duration - elapsed))

    def _ramp_task_finished(self, ga: int, task: asyncio.Task) -> None:
        """Remove completed ramp bookkeeping without disturbing a replacement ramp."""
        if self._ramp_tasks.get(ga) is task:
            self._ramp_tasks.pop(ga, None)
            self._active_ramps.pop(ga, None)

    def _estimated_ramp_level(self, ga: int) -> int:
        """Return the current interpolated level for an active ramp."""
        ramp = self._active_ramps.get(ga)
        if not ramp:
            return int(self.states[ga].get("brightness", 0))

        elapsed = self.hass.loop.time() - float(ramp["started_at"])
        return interpolate_ramp_level(
            int(ramp["start_level"]),
            int(ramp["target_level"]),
            elapsed,
            float(ramp["duration"]),
        )

    def _cancel_ramp(self, ga: int) -> None:
        """Cancel one ramp task and discard its metadata."""
        task = self._ramp_tasks.pop(ga, None)
        self._active_ramps.pop(ga, None)
        if task and not task.done():
            task.cancel()

    def _publish_level(self, ga: int, brightness: int) -> None:
        """Publish one clamped group level through DataUpdateCoordinator."""
        level = max(0, min(255, round(brightness)))
        self.states[ga] = {"state": level > 0, "brightness": level}
        self.async_set_updated_data(dict(self.states))

    async def send_command(
        self,
        ga: int,
        turn_on: bool,
        brightness: int | None = None,
        transition: float | int | None = None,
    ):
        """Send a C-Bus lighting command, including native ramp transitions."""
        if not self.writer or self._intentional_disconnect:
            return

        start_level = self._estimated_ramp_level(ga)
        effective_transition = (
            self.default_transition if transition is None else float(transition)
        )
        has_transition = effective_transition > 0

        if brightness is not None or has_transition:
            target_level = (
                max(0, min(255, round(brightness)))
                if brightness is not None
                else (255 if turn_on else 0)
            )
            ramp_command = ramp_command_for_transition(
                effective_transition, start_level, target_level
            )
            command_ascii = build_lighting_command(
                ga, ramp_command, target_level=target_level
            )
        else:
            target_level = 255 if turn_on else 0
            ramp_command = None
            command_ascii = build_lighting_command(
                ga, LIGHT_ON if turn_on else LIGHT_OFF
            )

        try:
            self.writer.write(command_ascii.encode("ascii"))
            await self.writer.drain()

            if ramp_command is None:
                self._cancel_ramp(ga)
                self._publish_level(ga, target_level)
            else:
                self._start_ramp(ga, target_level, ramp_command)

            _LOGGER.info(
                "C-Bus Command Sent: GA %d -> Level %02X%s",
                ga,
                target_level,
                (
                    f" (ramp command {ramp_command:02X})"
                    if ramp_command is not None
                    else ""
                ),
            )
        except Exception as err:
            _LOGGER.error(
                "C-Bus Command Failed: Network write error on GA %d: %s", ga, err
            )
            self.is_connected = False
