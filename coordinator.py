import asyncio
import logging
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

def calculate_cbus_checksum(hex_string: str) -> str:
    """Calculate the standard C-Bus 2's complement checksum of a hex string."""
    try:
        total = sum(int(hex_string[i:i+2], 16) for i in range(0, len(hex_string), 2))
        remainder = total % 256
        checksum_val = (256 - remainder) % 256
        return f"{checksum_val:02X}"
    except Exception as err:
        _LOGGER.error("Failed calculating checksum for %s: %s", hex_string, err)
        return "00"

class CBusCoordinator(DataUpdateCoordinator):
    """Handles persistent connection and lifecycle status sync for C-Bus CNIs."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, lighting_map: dict):
        super().__init__(hass, _LOGGER, name="cbus_native_coordinator")
        self.host = host
        self.port = port
        self.lighting_map = lighting_map
        self.reader = None
        self.writer = None
        self.is_connected = False
        self._intentional_disconnect = False
        self._tasks = []
        
        # Internal state cache for all tracked group addresses
        self.states = {ga: {"state": False, "brightness": 0} for ga in lighting_map}

    async def connect(self):
        """Establish persistent asynchronous connection with the gateway."""
        if self.is_connected:
            return
            
        self._intentional_disconnect = False
        try:
            _LOGGER.info("C-Bus Connecting: Opening TCP socket to %s:%s", self.host, self.port)
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            
            # Reset CNI buffers
            self.writer.write(b"\\0500000000g\r")
            await self.writer.drain()
            await asyncio.sleep(1.0)

            # Enable local socket monitoring options
            init_cmds = [
                "\\05140021C6g\r",  # Monitor Mode ON
                "\\05140023C4g\r",  # Smart Mode & System IO ON
                "\\05140038F3g\r",  # Enable MMI reporting for Application 56
                "\\05090000F8g\r"   # Request initial full MMI block status
            ]
            for cmd in init_cmds:
                self.writer.write(cmd.encode('ascii'))
                await self.writer.drain()
                await asyncio.sleep(0.3)
            
            # Spin up managed background loops
            self._tasks.append(self.hass.loop.create_task(self._listen_loop()))
            self._tasks.append(self.hass.loop.create_task(self._heartbeat_loop()))
            self._tasks.append(self.hass.loop.create_task(self._sync_loop()))
            _LOGGER.info("C-Bus Connection: Fully initialized and background loops started.")
            
        except Exception as err:
            _LOGGER.error("C-Bus Connection: Failed to establish ASCII link to gateway: %s", err)
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
        """Paced query loop to safely sync all group states on startup without bus congestion."""
        _LOGGER.info("C-Bus Sync: Initiating paced startup status poll for %d groups...", len(self.lighting_map))
        await asyncio.sleep(3)
        
        for ga in self.lighting_map:
            if not self.is_connected or self._intentional_disconnect:
                break
            try:
                # Command 03 is the standard CAL Status Request for a specific GA
                base_hex = f"05380003{ga:02X}"
                cmd = f"\\{base_hex}{calculate_cbus_checksum(base_hex)}g\r"
                self.writer.write(cmd.encode('ascii'))
                await self.writer.drain()
                _LOGGER.debug("C-Bus Sync: Polled GA %d (Hex: %s)", ga, base_hex)
                # 150ms delay to spread commands and avoid flooding buffers or network
                await asyncio.sleep(0.15)
            except Exception as e:
                _LOGGER.error("C-Bus Sync: Poll aborted for GA %d: %s", ga, e)
                break
                
        _LOGGER.info("C-Bus Sync: Startup polling sequence complete.")

    async def disconnect(self):
        """Gracefully close sockets and cancel active tasks (HA required lifecycle method)."""
        self._intentional_disconnect = True
        self.is_connected = False
        
        # Cleanly cancel running tasks
        _LOGGER.info("C-Bus Disconnecting: Cancelling active loop tasks...")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()
        
        if self.writer:
            try:
                _LOGGER.info("C-Bus Disconnecting: Terminating network socket stream cleanly...")
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=3.0)
            except Exception as err:
                _LOGGER.debug("C-Bus Disconnecting: Exception while closing write channel: %s", err)
            finally:
                self.writer = None
                self.reader = None
        _LOGGER.info("C-Bus Disconnecting: Disconnected completely.")

    async def _heartbeat_loop(self):
        """Send standard monitor status query as a heartbeat to avoid CNI timeouts."""
        while self.is_connected and not self._intentional_disconnect:
            try:
                if self.writer:
                    # Request status update for Application 56 to act as keep-alive payload
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
                    _LOGGER.warning("C-Bus Listener: Connection lost on remote socket interface.")
                    break
                    
                buffer += data.decode('ascii', errors='ignore')
                while '\r' in buffer and not self._intentional_disconnect:
                    line, buffer = buffer.split('\r', 1)
                    line = line.strip().upper()
                    
                    if not line or line.startswith("!G"):
                        continue
                        
                    _LOGGER.debug("C-Bus Listener: Raw line received: %s", line)
                    try:
                        if "3800" in line:
                            self._process_event_update(line)
                        elif any(prefix in line for prefix in ["F638", "8638", "D838", "D638"]):
                            self._process_mmi_response(line)
                    except Exception as parse_err:
                        _LOGGER.debug("C-Bus Listener: Parsing exception on line %s: %s", line, parse_err)
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
            mmi_marker = next((p for p in ["F638", "8638", "D838", "D638"] if p in line), None)
            if not mmi_marker:
                return

            mmi_idx = line.find(mmi_marker)
            start_ga = int(line[mmi_idx+4:mmi_idx+6], 16)
            idx_data = mmi_idx + 6
            num_bytes = min((len(line) - idx_data - 2) // 2, 22)
            
            state_updated = False
            for i in range(num_bytes):
                byte_hex = line[idx_data + i*2 : idx_data + i*2 + 2]
                if len(byte_hex) < 2:
                    break
                byte_data = int(byte_hex, 16)
                for ga_offset in range(4):
                    ga = start_ga + (i * 4) + ga_offset
                    if ga in self.lighting_map:
                        # C-Bus state encoding: LSB represents lower GA
                        shift = ga_offset * 2
                        state_val = (byte_data >> shift) & 0x03
                        is_on = state_val in (0x01, 0x03)
                        
                        current_brightness = self.states[ga].get("brightness", 0)
                        if is_on:
                            # Preserve existing dim level, fall back to 255 if level untracked
                            new_brightness = current_brightness if current_brightness > 0 else 255
                        else:
                            new_brightness = 0
                            
                        self.states[ga].update({"state": is_on, "brightness": new_brightness})
                        state_updated = True

            if state_updated:
                self.async_set_updated_data(dict(self.states))
                _LOGGER.debug("C-Bus MMI Sync: Handled block starting at GA %d", start_ga)
        except Exception as e:
            _LOGGER.error("C-Bus MMI: Parsing failure: %s", e)

    def _process_event_update(self, line):
        """Handle incoming asynchronous event telegram updates (including level dimming)."""
        try:
            idx = line.find("3800")
            if idx == -1 or len(line) < idx + 8:
                return
                
            cmd_hex = line[idx+4:idx+6]
            ga_hex = line[idx+6:idx+8]
            ga = int(ga_hex, 16)
            
            if ga in self.states:
                cmd_byte = int(cmd_hex, 16)
                
                # Check if it is a "Ramp to Level" command ending with level hex bytes
                if (cmd_byte & 0x03) == 0x02 and len(line) >= idx + 10:
                    level_hex = line[idx+8:idx+10]
                    level = int(level_hex, 16)
                    is_on = level > 0
                    brightness = level
                else:
                    # Basic standard switch commands (ON/OFF toggling)
                    is_on = cmd_hex != "01"
                    brightness = 255 if is_on else 0
                    
                self.states[ga].update({"state": is_on, "brightness": brightness})
                self.async_set_updated_data(dict(self.states))
                _LOGGER.info("C-Bus Event Sync: GA %d -> State: %s, Brightness: %d", ga, "ON" if is_on else "OFF", brightness)
        except Exception as e:
            _LOGGER.error("C-Bus Event Sync: Exception processing telegram: %s", e)

    async def send_command(self, ga: int, turn_on: bool, brightness: int = None):
        """Formulate and dispatch direct binary-level C-Bus lighting commands."""
        if not self.writer or self._intentional_disconnect:
            return

        cmd_byte = "02" # Standard lighting command
        level_hex = f"{brightness:02X}" if brightness is not None else ("FF" if turn_on else "00")
        base_hex = f"053800{cmd_byte}{ga:02X}{level_hex}"
        cmd_ascii = f"\\{base_hex}{calculate_cbus_checksum(base_hex)}g\r"
        
        try:
            self.writer.write(cmd_ascii.encode('ascii'))
            await self.writer.drain()
            
            self.states[ga] = {
                "state": (brightness or 0) > 0 or turn_on, 
                "brightness": brightness or (255 if turn_on else 0)
            }
            self.async_set_updated_data(dict(self.states))
            _LOGGER.info("C-Bus Command Sent: GA %d -> Level %s", ga, level_hex)
        except Exception as e:
            _LOGGER.error("C-Bus Command Failed: Network write error on GA %d: %s", ga, e)
            self.is_connected = False