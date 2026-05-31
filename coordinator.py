import asyncio
import logging
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

def calculate_cbus_checksum(hex_string: str) -> str:
    """Calculate the C-Bus 2's complement checksum of a hex string byte-pair."""
    try:
        total = sum(int(hex_string[i:i+2], 16) for i in range(0, len(hex_string), 2))
        remainder = total % 256
        checksum_val = (256 - remainder) % 256
        return f"{checksum_val:02X}"
    except Exception as err:
        _LOGGER.error("Failed calculating checksum for %s: %s", hex_string, err)
        return "00"

class CBusCoordinator(DataUpdateCoordinator):
    """Handles persistent connection to C-Bus CNI using ASCII Dimming and MMI Polling."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, lighting_map: dict):
        super().__init__(hass, _LOGGER, name="cbus_native_coordinator")
        self.host = host
        self.port = port
        self.lighting_map = lighting_map
        self.reader = None
        self.writer = None
        self.is_connected = False
        self._intentional_disconnect = False
        self._tasks = []  # Tracking list to cleanly dispose of async loops on unload
        
        self.states = {ga: {"state": False, "brightness": 0} for ga in lighting_map}

    async def connect(self):
        """Establish persistent asynchronous connection."""
        if self.is_connected:
            return
            
        self._intentional_disconnect = False
        try:
            _LOGGER.info("Opening raw ASCII connection socket to C-Bus CNI at %s:%s", self.host, self.port)
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            
            # 1. Initialize CNI buffer
            _LOGGER.info("C-Bus: Sending initialization handshake to clear CNI buffers...")
            self.writer.write(b"\r\r")
            await self.writer.drain()
            await asyncio.sleep(0.5)

            # 2. Configure CNI Interface Options (Force Monitor and Smart Mode)
            _LOGGER.info("C-Bus: Configuring CNI interface options (Monitor/Smart Mode)...")
            init_cmds = [
                "\\05140021C6g\r",  # Option 1: Monitor & Connection mode ON
                "\\05140023C4g\r",  # Option 3: Smart Mode ON
            ]
            for cmd in init_cmds:
                self.writer.write(cmd.encode('ascii'))
                await self.writer.drain()
                await asyncio.sleep(0.3)
            
            # Spin up background tasks and track them for cleanup on reload
            self._tasks.append(self.hass.loop.create_task(self._listen_loop()))
            self._tasks.append(self.hass.loop.create_task(self._heartbeat_loop()))
            self._tasks.append(self.hass.loop.create_task(self._periodic_status_poll()))
            
        except Exception as err:
            _LOGGER.error("Failed to establish ASCII stream to CNI: %s", err)
            self.is_connected = False
            if not self._intentional_disconnect:
                self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    async def _reconnect_later(self):
        """Wait safely and then trigger a reconnection."""
        if self._intentional_disconnect:
            return
        await asyncio.sleep(5)
        if not self._intentional_disconnect:
            await self.connect()

    async def _initial_status_poll(self):
        """Request the current status of all group addresses on Application 56."""
        if not self.is_connected or not self.writer or self._intentional_disconnect:
            return
        try:
            # 1. Trigger global MMI status dump first
            query = "\\05FF007A38004Ag\r"
            _LOGGER.info("C-Bus Polling: Dispatching global MMI state query onto network...")
            self.writer.write(query.encode('ascii'))
            await self.writer.drain()
            await asyncio.sleep(1.0)  # Wait for MMI block responses to finish transmitting

            # 2. Query each mapped Group Address individually for guaranteed real-time status (Bridges/Segment recovery)
            _LOGGER.info("C-Bus Polling: Querying %d mapped Group Addresses individually for real-time levels...", len(self.lighting_map))
            for ga in self.lighting_map:
                if self._intentional_disconnect or not self.is_connected:
                    break
                
                # Command 03 is the standard CAL Status Request for an individual Group Address
                base_hex = f"05380003{ga:02X}"
                checksum = calculate_cbus_checksum(base_hex)
                ga_query = f"\\{base_hex}{checksum}g\r"
                
                self.writer.write(ga_query.encode('ascii'))
                await self.writer.drain()
                await asyncio.sleep(0.15)  # 150ms delay to spacing commands and avoid bus congestion
                
        except Exception as err:
            _LOGGER.error("Failed sending status queries: %s", err)

    async def _periodic_status_poll(self):
        """Periodically trigger status updates to catch any missed states."""
        # Run a poll immediately after boot stabilization
        await asyncio.sleep(3)
        await self._initial_status_poll()

        while self.is_connected and not self._intentional_disconnect:
            try:
                # Triggers a slow poll every 5 minutes (300 seconds)
                await asyncio.sleep(300)
                if self.is_connected and not self._intentional_disconnect:
                    _LOGGER.info("C-Bus Polling: Triggering scheduled background status poll...")
                    await self._initial_status_poll()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("C-Bus Polling: Error in periodic status cycle: %s", err)
                await asyncio.sleep(30)

    async def disconnect(self):
        """Clean connection teardown preventing background reconnection loops."""
        self._intentional_disconnect = True
        self.is_connected = False
        
        # Instantly cancel all scheduled and active background tasks to prevent socket fighting
        _LOGGER.info("Cancelling background tasks...")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()
        
        if self.writer:
            try:
                _LOGGER.info("Closing C-Bus socket connection cleanly...")
                try:
                    self.writer.write(b"\r")
                    await asyncio.wait_for(self.writer.drain(), timeout=1.0)
                except Exception:
                    pass
                
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=3.0)
            except Exception as err:
                _LOGGER.debug("Socket disconnect exception: %s", err)
            finally:
                self.writer = None
                self.reader = None
        
        # Enforce a 2.0-second cooldown to let Wiser completely clean up the old file descriptor
        _LOGGER.info("CNI cooldown initiated.")
        await asyncio.sleep(2.0)
        _LOGGER.info("CNI cooldown complete.")

    async def _heartbeat_loop(self):
        """Send keep-alive carriage return periodically."""
        while self.is_connected and not self._intentional_disconnect:
            try:
                if self.writer:
                    self.writer.write(b"\r")
                    await self.writer.drain()
            except Exception as err:
                _LOGGER.error("CNI Keep-alive lost connection: %s.", err)
                self.is_connected = False
                break
            await asyncio.sleep(30)
        
        if not self.is_connected and not self._intentional_disconnect:
            await self.disconnect()
            self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    async def _listen_loop(self):
        """Monitor incoming stream with strict error recovery and a TCP buffer."""
        buffer = ""
        while self.is_connected and not self._intentional_disconnect:
            try:
                if not self.reader: break
                
                data = await asyncio.wait_for(self.reader.read(1024), timeout=60.0)
                if not data: 
                    _LOGGER.warning("CNI interface closed stream.")
                    break

                buffer += data.decode('ascii', errors='ignore')
                
                while '\r' in buffer and not self._intentional_disconnect:
                    line, buffer = buffer.split('\r', 1)
                    
                    # Normalize incoming string to uppercase to avoid case-sensitivity bugs
                    line = line.strip().upper()
                    if not line: continue
                    
                    # Added native Wiser/CNI CAL MMI patterns: D838, D638 along with F638, 8638
                    if any(prefix in line for prefix in ["F638", "8638", "D838", "D638"]):
                        _LOGGER.info("C-Bus Polling: Received MMI Response: %s", line)
                        self._process_mmi_response(line)
                    elif "3800" in line:
                        self._process_event_update(line)
                    else:
                        _LOGGER.debug("C-Bus Raw: Received unhandled message from CNI: %s", line)
            except asyncio.TimeoutError:
                continue
            except Exception as err:
                if not self._intentional_disconnect:
                    _LOGGER.error("Error in listener loop: %s", err)
                buffer = ""
                await asyncio.sleep(1)
        
        self.is_connected = False
        if not self._intentional_disconnect:
            self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    def _process_mmi_response(self, line):
        """Parse MMI blocks while safely preserving active dimming brightness."""
        try:
            # Dynamically seek any of our 4 expected headers
            mmi_idx = -1
            for prefix in ["F638", "8638", "D838", "D638"]:
                found_idx = line.find(prefix)
                if found_idx != -1:
                    mmi_idx = found_idx
                    break
            
            # Bounds checking to prevent loop crashes
            if mmi_idx == -1 or len(line) < mmi_idx + 22: return
            
            block_start_hex = line[mmi_idx+4 : mmi_idx+6]
            start_ga = int(block_start_hex, 16)
            idx_data = mmi_idx + 6
            
            # Dynamic calculation of byte payload length to safely capture the entire C-Bus block response
            # (Length of line - starting index of hex data - 2 characters for trailing checksum) // 2
            num_bytes = (len(line) - idx_data - 2) // 2
            if num_bytes > 22:
                num_bytes = 22  # Hard safety cap
            
            state_updated = False
            for i in range(num_bytes):
                byte_hex = line[idx_data + i*2 : idx_data + i*2 + 2]
                if len(byte_hex) < 2:
                    break
                byte_data = int(byte_hex, 16)
                for ga_offset in range(4):
                    ga = start_ga + (i * 4) + ga_offset
                    if ga in self.lighting_map:
                        # C-Bus protocol maps lowest GA N to LSB (bits 0-1) and highest GA N+3 to MSB (bits 6-7)
                        shift = ga_offset * 2
                        state_val = (byte_data >> shift) & 0x03
                        
                        # In C-Bus:
                        # 0x00 = Unassigned/Off
                        # 0x01 = Stable On
                        # 0x02 = Stable Off (Unit present)
                        # 0x03 = Active ramping / timer active On (Unit present)
                        is_on = state_val in (0x01, 0x03)
                        
                        # Preserve existing dimmed brightness level instead of overriding to 255
                        current_brightness = self.states[ga].get("brightness", 0)
                        if is_on:
                            new_brightness = current_brightness if current_brightness > 0 else 255
                        else:
                            new_brightness = 0
                            
                        self.states[ga].update({"state": is_on, "brightness": new_brightness})
                        state_updated = True
            
            if state_updated:
                # Force shallow copy dictionary representation so HA triggers listener redraws!
                self.async_set_updated_data(dict(self.states))
                _LOGGER.info("C-Bus Polling: Successfully synchronized status block starting at GA %d (%d bytes)", start_ga, num_bytes)
        except Exception as e:
            _LOGGER.debug("MMI Parse error: %s", e)

    def _process_event_update(self, line):
        """Handle real-time point-to-point updates."""
        try:
            idx = line.find("3800")
            if idx == -1 or len(line) < idx + 8: return
            
            cmd_hex = line[idx+4:idx+6]
            ga_hex = line[idx+6:idx+8]
            ga = int(ga_hex, 16)
            
            if ga in self.states:
                cmd_byte = int(cmd_hex, 16)
                
                # Check for "Ramp to Level" commands
                if (cmd_byte & 0x03) == 0x02 and len(line) >= idx + 10:
                    level_hex = line[idx+8:idx+10]
                    level = int(level_hex, 16)
                    is_on = level > 0
                    brightness = level
                else:
                    # Standard ON/OFF controls
                    is_on = cmd_hex != "01"
                    brightness = 255 if is_on else 0
                    
                self.states[ga].update({"state": is_on, "brightness": brightness})
                # Force shallow copy dictionary representation so HA triggers listener redraws!
                self.async_set_updated_data(dict(self.states))
                _LOGGER.info("C-Bus Sync: Real-time update GA %d -> State: %s, Brightness: %d", ga, "ON" if is_on else "OFF", brightness)
        except Exception as e:
            _LOGGER.debug("Event Parse error: %s", e)

    async def send_command(self, ga: int, turn_on: bool, brightness: int = None):
        """Build and dispatch an ASCII formatted C-Bus packet."""
        if not self.writer or self._intentional_disconnect: return

        cmd_byte = "02"
        level_hex = f"{brightness:02X}" if brightness is not None else ("FF" if turn_on else "00")
        base_hex = f"053800{cmd_byte}{ga:02X}{level_hex}"
        cmd_ascii = f"\\{base_hex}{calculate_cbus_checksum(base_hex)}g\r"
        
        try:
            self.writer.write(cmd_ascii.encode('ascii'))
            await self.writer.drain()
            self.states[ga] = {"state": (brightness or 0) > 0 or turn_on, "brightness": brightness or (255 if turn_on else 0)}
            # Force shallow copy dictionary representation so HA triggers listener redraws!
            self.async_set_updated_data(dict(self.states))
        except Exception as e:
            _LOGGER.error("Failed to write to CNI socket: %s", e)
            self.is_connected = False