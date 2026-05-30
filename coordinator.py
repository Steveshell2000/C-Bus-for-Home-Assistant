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
            
            # Force status sync with an expanded polling strategy
            self.hass.loop.create_task(self._initial_status_poll())
        except Exception as err:
            _LOGGER.error("Failed to establish ASCII stream to CNI: %s", err)
            self.is_connected = False

    async def _initial_status_poll(self):
        """Request the current status of all group addresses on Application 56."""
        await asyncio.sleep(3)  # Wait for socket channels to clear
        if self.is_connected and self.writer:
            try:
                # Query Application 56 (Lighting)
                # 05 = Length, FF = Source, 00 = Destination, 7A = MMI Status, 38 = App 56
                mmi_queries = ["\\05FF007A38004Ag\r", "\\05FF007A382024g\r"]
                for query in mmi_queries:
                    _LOGGER.info("C-Bus Polling: Dispatching MMI state query: %s", query.strip())
                    self.writer.write(query.encode('ascii'))
                    await self.writer.drain()
                    await asyncio.sleep(0.5) # Space out queries for the Wiser internal buffer
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
            except Exception as err:
                _LOGGER.debug("Socket disconnect exception: %s", err)
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
                _LOGGER.error("CNI Keep-alive lost connection: %s.", err)
                self.is_connected = False
                break
            await asyncio.sleep(30)
        
        if not self.is_connected:
            await asyncio.sleep(5)
            await self.connect()

    async def _listen_loop(self):
        """Monitor incoming stream decoding ASCII hex representations."""
        while self.is_connected:
            try:
                data = await self.reader.read(1024)
                if not data: break

                ascii_data = data.decode('ascii', errors='ignore')
                # Split by potential line endings
                lines = [line.strip() for line in ascii_data.replace('\n', '\r').split('\r') if line.strip()]
                
                for line in lines:
                    # Look for F638/8638 (MMI Response)
                    if "F638" in line or "8638" in line:
                        self._process_mmi_response(line)
                    # Look for standard 3800 point-to-point updates
                    elif "3800" in line:
                        self._process_event_update(line)
            except Exception as err:
                _LOGGER.error("Error in listener loop: %s", err)
                break

    def _process_mmi_response(self, line):
        """Parse the hex status block response."""
        try:
            # Find the start of the hex block
            mmi_idx = line.find("F638")
            if mmi_idx == -1: mmi_idx = line.find("8638")
            
            block_start_hex = line[mmi_idx+4 : mmi_idx+6]
            start_ga = int(block_start_hex, 16)
            idx_data = mmi_idx + 6
            
            for i in range(8):
                byte_data = int(line[idx_data + i*2 : idx_data + i*2 + 2], 16)
                for ga_offset in range(4):
                    ga = start_ga + (i * 4) + ga_offset
                    if ga in self.lighting_map:
                        state_val = (byte_data >> (ga_offset * 2)) & 0x03
                        is_on = (state_val == 0x01)
                        self.states[ga].update({"state": is_on, "brightness": 255 if is_on else 0})
            
            self.async_set_updated_data(self.states)
        except Exception as e:
            _LOGGER.debug("MMI Parse error: %s", e)

    def _process_event_update(self, line):
        """Parse point-to-point real-time events."""
        try:
            idx = line.find("3800")
            cmd_hex = line[idx+4:idx+6]
            ga_hex = line[idx+6:idx+8]
            ga = int(ga_hex, 16)
            
            if ga in self.states:
                is_on = cmd_hex not in ["01", "02"]
                self.states[ga].update({"state": is_on, "brightness": 255 if is_on else 0})
                self.async_set_updated_data(self.states)
        except Exception as e:
            _LOGGER.debug("Event Parse error: %s", e)

    async def send_command(self, ga: int, turn_on: bool, brightness: int = None):
        """Build and dispatch an ASCII formatted C-Bus packet."""
        if not self.writer: return

        cmd_byte = "02"
        level_hex = f"{brightness:02X}" if brightness is not None else ("FF" if turn_on else "00")
        base_hex = f"053800{cmd_byte}{ga:02X}{level_hex}"
        cmd_ascii = f"\\{base_hex}{calculate_cbus_checksum(base_hex)}g\r"
        
        self.writer.write(cmd_ascii.encode('ascii'))
        await self.writer.drain()
        
        # Update state locally immediately
        self.states[ga] = {"state": (brightness or 0) > 0 or turn_on, "brightness": brightness or (255 if turn_on else 0)}
        self.async_set_updated_data(self.states)