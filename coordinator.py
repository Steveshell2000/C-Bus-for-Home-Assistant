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
        
        # Track states dynamically as dictionary objects containing both state and brightness
        self.states = {ga: {"state": False, "brightness": 0} for ga in lighting_map}

    async def connect(self):
        """Establish persistent asynchronous connection."""
        try:
            _LOGGER.info("Opening raw ASCII connection socket to C-Bus CNI at %s:%s", self.host, self.port)
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            
            # Start background read listener and heartbeat loops
            self.hass.loop.create_task(self._listen_loop())
            self.hass.loop.create_task(self._heartbeat_loop())
            
            # Request network-wide MMI status poll after connection stabilizes
            self.hass.loop.create_task(self._initial_status_poll())
        except Exception as err:
            _LOGGER.error("Failed to establish ASCII stream to CNI: %s", err)
            self.is_connected = False

    async def _initial_status_poll(self):
        """Request the current status of all group addresses on Application 56 on startup."""
        await asyncio.sleep(2)  # Wait for socket channels to clear
        if self.is_connected and self.writer:
            try:
                # 05FF007A3800 is the standard C-Bus MMI status query for Application 56 (Lighting)
                # Mathematical 2's complement checksum for '05FF007A3800' is '4A'
                mmi_query = "\\05FF007A38004Ag\r"
                _LOGGER.info("C-Bus Polling: Dispatched global MMI state query onto network.")
                self.writer.write(mmi_query.encode('ascii'))
                await self.writer.drain()
            except Exception as err:
                _LOGGER.error("Failed sending global MMI status query: %s", err)

    async def disconnect(self):
        """Clean connection teardown with a socket cooldown period."""
        self.is_connected = False
        if self.writer:
            try:
                self.writer.write(b"\r")
                await self.writer.drain()
                self.writer.close()
                await self.writer.wait_closed()
                _LOGGER.info("Successfully disconnected from C-Bus CNI.")
            except Exception as err:
                _LOGGER.debug("Socket disconnected with outstanding exception: %s", err)
            finally:
                self.writer = None
                self.reader = None
                await asyncio.sleep(1.5)

    async def _heartbeat_loop(self):
        """Send standard C-Bus keep-alive carriage return periodically."""
        while self.is_connected:
            try:
                if self.writer:
                    self.writer.write(b"\r")
                    await self.writer.drain()
            except Exception as err:
                _LOGGER.error("CNI Keep-alive lost connection: %s. Reconnecting...", err)
                self.is_connected = False
                break
            await asyncio.sleep(30)
        
        if not self.is_connected:
            await asyncio.sleep(5)
            if self.is_connected is False:
                await self.connect()

    async def _listen_loop(self):
        """Monitor incoming stream decoding ASCII hex representations."""
        while self.is_connected:
            try:
                if not self.reader:
                    break
                data = await self.reader.read(1024)
                if not data:
                    _LOGGER.warning("CNI interface closed stream cleanly.")
                    self.is_connected = False
                    break

                ascii_data = data.decode('ascii', errors='ignore')
                lines = [line.strip() for line in ascii_data.replace('\n', '\r').split('\r') if line.strip()]
                
                for line in lines:
                    # 1. PARSE GLOBAL MMI STATUS BLOCK RESPONSES (Look for F638 or 8638)
                    mmi_idx = -1
                    for prefix in ["F638", "8638", "f638"]:
                        found_idx = line.find(prefix)
                        if found_idx != -1:
                            mmi_idx = found_idx
                            break

                    if mmi_idx != -1 and len(line) >= mmi_idx + 22:
                        try:
                            # Extract block starting address offset (e.g., "00" for GA 0-31, "20" for GA 32-63)
                            block_start_hex = line[mmi_idx+4 : mmi_idx+6]
                            start_ga = int(block_start_hex, 16)
                            
                            idx_data = mmi_idx + 6
                            state_updated = False
                            
                            for i in range(8):
                                char_pair = line[idx_data + i*2 : idx_data + i*2 + 2]
                                if len(char_pair) < 2:
                                    break
                                b = int(char_pair, 16)
                                
                                # Extract 4 Group Address states from each byte (2-bits each)
                                for ga_offset in range(4):
                                    ga = start_ga + (i * 4) + ga_offset
                                    state_val = (b >> (ga_offset * 2)) & 0x03
                                    
                                    # 0x00 = OFF, 0x01 = ON (0x02 = Error, 0x03 = Unused/Unknown)
                                    if state_val in (0x00, 0x01) and ga in self.lighting_map:
                                        is_on = (state_val == 0x01)
                                        current_brightness = self.states.get(ga, {}).get("brightness", 0)
                                        
                                        # Maintain local tracking logic
                                        if is_on and current_brightness == 0:
                                            brightness = 255
                                        elif not is_on:
                                            brightness = 0
                                        else:
                                            brightness = current_brightness
                                            
                                        self.states[ga] = {"state": is_on, "brightness": brightness}
                                        state_updated = True
                                        
                            if state_updated:
                                self.async_set_updated_data(self.states)
                                _LOGGER.info("C-Bus Polling: Successfully synced MMI status block starting at GA %d", start_ga)
                            continue
                        except Exception as mmi_err:
                            _LOGGER.debug("Failed parsing MMI line %s: %s", line, mmi_err)

                    # 2. PARSE REAL-TIME POINT-TO-POINT FEEDBACK EVENTS (3800...)
                    idx = line.find("3800")
                    if idx != -1 and len(line) >= idx + 8:
                        cmd_hex = line[idx+4:idx+6]
                        ga_hex = line[idx+6:idx+8]
                        
                        try:
                            ga = int(ga_hex, 16)
                            cmd_byte = int(cmd_hex, 16)
                            
                            # Check if command is "Ramp to Level" (lower bits have '2' mask)
                            if (cmd_byte & 0x07) == 0x02 and len(line) >= idx + 10:
                                level_hex = line[idx+8:idx+10]
                                level = int(level_hex, 16)
                                is_on = level > 0
                                brightness = level
                            else:
                                is_on = cmd_hex != "01" and cmd_hex != "02"
                                brightness = 255 if is_on else 0
                            
                            self.states[ga] = {"state": is_on, "brightness": brightness}
                            self.async_set_updated_data(self.states)
                            _LOGGER.info("C-Bus Sync: GA %d updated -> State: %s, Brightness: %d", ga, "ON" if is_on else "OFF", brightness)
                        except ValueError:
                            continue
            except Exception as err:
                _LOGGER.error("Error encountered inside serial listener loop: %s", err)
                break

    async def send_command(self, ga: int, turn_on: bool, brightness: int = None):
        """Build and dispatch an ASCII formatted C-Bus packet."""
        if not self.is_connected or not self.writer:
            _LOGGER.error("CNI engine currently offline. Command dropped.")
            return

        try:
            # 02 sets an instant transition, which feels perfect on dimming sliders
            cmd_byte = "02"
            if brightness is not None:
                level_hex = f"{brightness:02X}"
                target_brightness = brightness
                target_state = brightness > 0
            else:
                if turn_on:
                    level_hex = "FF"
                    target_brightness = 255
                    target_state = True
                else:
                    level_hex = "00"
                    target_brightness = 0
                    target_state = False

            base_hex = f"053800{cmd_byte}{ga:02X}{level_hex}"
            checksum = calculate_cbus_checksum(base_hex)
            cmd_ascii = f"\\{base_hex}{checksum}g\r"
            
            _LOGGER.info("C-Bus Control: Transmitting ASCII -> %s", cmd_ascii.strip())
            
            self.writer.write(cmd_ascii.encode('ascii'))
            await self.writer.drain()
            
            self.states[ga] = {"state": target_state, "brightness": target_brightness}
            self.async_set_updated_data(self.states)
        except Exception as err:
            _LOGGER.error("Failed handling outgoing physical transmission: %s", err)