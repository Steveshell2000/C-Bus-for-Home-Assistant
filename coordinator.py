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
        # e.g., { ga_int: {"state": True, "brightness": 255} }
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
                # Checksum for '05FF007A3800' is '49'
                mmi_query = "\\05FF007A380049g\r"
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
                    idx = line.find("3800")
                    if idx != -1 and len(line) >= idx + 8:
                        cmd_hex = line[idx+4:idx+6]
                        ga_hex = line[idx+6:idx+8]
                        
                        try:
                            ga = int(ga_hex, 16)
                            cmd_byte = int(cmd_hex, 16)
                            
                            # Check if this is a "Ramp to Level" command (command ends in 0x02)
                            if (cmd_byte & 0x07) == 0x02 and len(line) >= idx + 10:
                                level_hex = line[idx+8:idx+10]
                                level = int(level_hex, 16)
                                is_on = level > 0
                                brightness = level
                            else:
                                # Standard ON/OFF fallbacks
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
        """Build and dispatch an ASCII formatted C-Bus packet with dimming levels."""
        if not self.is_connected or not self.writer:
            _LOGGER.error("CNI engine currently offline. Command dropped.")
            return

        try:
            if brightness is not None:
                # Dimmer ramp level (instant rate = '02')
                cmd_byte = "02"
                level_hex = f"{brightness:02X}"
                base_hex = f"053800{cmd_byte}{ga:02X}{level_hex}"
                target_state = brightness > 0
                target_brightness = brightness
            else:
                # Basic ON/OFF presets
                cmd_byte = "79" if turn_on else "01"
                base_hex = f"053800{cmd_byte}{ga:02X}"
                target_state = turn_on
                target_brightness = 255 if turn_on else 0

            checksum = calculate_cbus_checksum(base_hex)
            cmd_ascii = f"\\{base_hex}{checksum}g\r"
            
            _LOGGER.info("C-Bus Control: Transmitting ASCII -> %s", cmd_ascii.strip())
            
            self.writer.write(cmd_ascii.encode('ascii'))
            await self.writer.drain()
            
            # Optimistically update localized state dictionary
            self.states[ga] = {"state": target_state, "brightness": target_brightness}
            self.async_set_updated_data(self.states)
        except Exception as err:
            _LOGGER.error("Failed handling outgoing physical transmission: %s", err)