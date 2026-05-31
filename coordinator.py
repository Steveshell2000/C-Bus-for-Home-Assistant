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
            
            # Start background read listener and heartbeat loops
            self.hass.loop.create_task(self._listen_loop())
            self.hass.loop.create_task(self._heartbeat_loop())
            
            # Force status sync
            self.hass.loop.create_task(self._initial_status_poll())
        except Exception as err:
            _LOGGER.error("Failed to establish ASCII stream to CNI: %s", err)
            self.is_connected = False
            if not self._intentional_disconnect:
                self.hass.loop.create_task(self._reconnect_later())

    async def _reconnect_later(self):
        """Wait safely and then trigger a reconnection."""
        if self._intentional_disconnect:
            return
        await asyncio.sleep(5)
        if not self._intentional_disconnect:
            await self.connect()

    async def _initial_status_poll(self):
        """Request the current status of all group addresses on Application 56."""
        await asyncio.sleep(3)
        if self.is_connected and self.writer and not self._intentional_disconnect:
            try:
                mmi_queries = ["\\05FF007A38004Ag\r", "\\05FF007A382024g\r"]
                for query in mmi_queries:
                    if self._intentional_disconnect:
                        break
                    self.writer.write(query.encode('ascii'))
                    await self.writer.drain()
                    await asyncio.sleep(0.5)
            except Exception as err:
                _LOGGER.error("Failed sending global MMI status query: %s", err)

    async def disconnect(self):
        """Clean connection teardown preventing background reconnection loops."""
        self._intentional_disconnect = True
        self.is_connected = False
        
        if self.writer:
            try:
                _LOGGER.info("Closing C-Bus socket connection cleanly...")
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as err:
                _LOGGER.debug("Socket disconnect exception: %s", err)
            finally:
                self.writer = None
                self.reader = None
        
        # Enforce a 1.5-second cooldown to let the CNI/Wiser release the TCP socket slot cleanly
        _LOGGER.info("CNI cooldown complete.")
        await asyncio.sleep(1.5)

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
            self.hass.loop.create_task(self._reconnect_later())

    async def _listen_loop(self):
        """Monitor incoming stream with strict error recovery and a TCP buffer."""
        buffer = ""
        while self.is_connected and not self._intentional_disconnect:
            try:
                if not self.reader: break
                
                # Use a timeout to ensure the read loop doesn't block indefinitely
                data = await asyncio.wait_for(self.reader.read(1024), timeout=60.0)
                if not data: 
                    _LOGGER.warning("CNI interface closed stream.")
                    break

                # Add new data to the text buffer
                buffer += data.decode('ascii', errors='ignore')
                
                # Only process complete lines ending in a carriage return
                while '\r' in buffer and not self._intentional_disconnect:
                    line, buffer = buffer.split('\r', 1)
                    line = line.strip()
                    if not line: continue
                    
                    if "F638" in line or "8638" in line:
                        self._process_mmi_response(line)
                    elif "3800" in line:
                        self._process_event_update(line)
            except asyncio.TimeoutError:
                continue
            except Exception as err:
                if not self._intentional_disconnect:
                    _LOGGER.error("Error in listener loop: %s", err)
                buffer = ""
                await asyncio.sleep(1)
        
        self.is_connected = False
        if not self._intentional_disconnect:
            self.hass.loop.create_task(self._reconnect_later())

    def _process_mmi_response(self, line):
        try:
            mmi_idx = line.find("F638")
            if mmi_idx == -1: mmi_idx = line.find("8638")
            
            # Bounds checking to prevent loop crashes
            if mmi_idx == -1 or len(line) < mmi_idx + 22: return
            
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
        try:
            idx = line.find("3800")
            # Bounds checking to prevent index out of range crashes
            if idx == -1 or len(line) < idx + 8: return
            
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
        if not self.writer or self._intentional_disconnect: return

        cmd_byte = "02"
        level_hex = f"{brightness:02X}" if brightness is not None else ("FF" if turn_on else "00")
        base_hex = f"053800{cmd_byte}{ga:02X}{level_hex}"
        cmd_ascii = f"\\{base_hex}{calculate_cbus_checksum(base_hex)}g\r"
        
        try:
            self.writer.write(cmd_ascii.encode('ascii'))
            await self.writer.drain()
            self.states[ga] = {"state": (brightness or 0) > 0 or turn_on, "brightness": brightness or (255 if turn_on else 0)}
            self.async_set_updated_data(self.states)
        except Exception as e:
            _LOGGER.error("Failed to write to CNI socket: %s", e)
            self.is_connected = False