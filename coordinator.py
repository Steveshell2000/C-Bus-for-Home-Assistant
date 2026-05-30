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
    """Handles raw persistent connection to the C-Bus CNI using the ASCII Protocol."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, lighting_map: dict):
        super().__init__(hass, _LOGGER, name="cbus_native_coordinator")
        self.host = host
        self.port = port
        self.lighting_map = lighting_map
        self.reader = None
        self.writer = None
        self.is_connected = False
        self.states = {}  # Tracks running states locally: {group_address: state_bool}

    async def connect(self):
        """Establish persistent asynchronous connection."""
        try:
            _LOGGER.info("Opening raw ASCII connection socket to C-Bus CNI at %s:%s", self.host, self.port)
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            
            # Start background read listener and heartbeat loops
            self.hass.loop.create_task(self._listen_loop())
            self.hass.loop.create_task(self._heartbeat_loop())
        except Exception as err:
            _LOGGER.error("Failed to establish ASCII stream to CNI: %s", err)
            self.is_connected = False

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
                
                # Sleep to let CNI release the single TCP slot
                _LOGGER.info("Cooldown sleep initiated to prevent CNI port hanging...")
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

                # Decode incoming stream as ASCII characters
                ascii_data = data.decode('ascii', errors='ignore')
                
                # Parse line-by-line
                lines = [line.strip() for line in ascii_data.replace('\n', '\r').split('\r') if line.strip()]
                
                for line in lines:
                    idx = line.find("3800")
                    if idx != -1 and len(line) >= idx + 8:
                        cmd_hex = line[idx+4:idx+6]
                        ga_hex = line[idx+6:idx+8]
                        
                        try:
                            ga = int(ga_hex, 16)
                            # Command 01 is OFF. Command 02 is RAMP TO 0 (OFF). All others are ON.
                            is_on = cmd_hex != "01" and cmd_hex != "02"
                            
                            self.states[ga] = is_on
                            self.async_set_updated_data(self.states)
                            _LOGGER.info("C-Bus Sync: Group Address %d (0x%s) updated to %s", ga, ga_hex, "ON" if is_on else "OFF")
                        except ValueError:
                            continue
            except Exception as err:
                _LOGGER.error("Error encountered inside serial listener loop: %s", err)
                break

    async def send_command(self, ga: int, turn_on: bool):
        """Build and dispatch an ASCII formatted C-Bus packet with checksum."""
        if not self.is_connected or not self.writer:
            _LOGGER.error("CNI engine currently offline. Command dropped.")
            return

        try:
            cmd_byte = "79" if turn_on else "01"
            base_hex = f"053800{cmd_byte}{ga:02X}"
            checksum = calculate_cbus_checksum(base_hex)
            cmd_ascii = f"\\{base_hex}{checksum}g\r"
            
            _LOGGER.info("C-Bus Control: Sending command -> %s", cmd_ascii.strip())
            
            self.writer.write(cmd_ascii.encode('ascii'))
            await self.writer.drain()
            
            self.states[ga] = turn_on
            self.async_set_updated_data(self.states)
        except Exception as err:
            _LOGGER.error("Failed handling outgoing physical transmission: %s", err)