import asyncio
import logging
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

def calculate_cbus_checksum(hex_string: str) -> str:
    total = sum(int(hex_string[i:i+2], 16) for i in range(0, len(hex_string), 2))
    return f"{(256 - (total % 256)) % 256:02X}"

class CBusCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, host, port, lighting_map):
        super().__init__(hass, _LOGGER, name="cbus_native_coordinator")
        self.host = host
        self.port = port
        self.lighting_map = lighting_map
        self.reader = None
        self.writer = None
        self.is_connected = False
        self._intentional_disconnect = False
        self._tasks = []
        self.states = {ga: {"state": False, "brightness": 0} for ga in lighting_map}

    async def connect(self):
        if self.is_connected: return
        self._intentional_disconnect = False
        try:
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            self.writer.write(b"\\0500000000g\r")
            await self.writer.drain()
            await asyncio.sleep(1.0)
            
            init_cmds = [
                "\\05140021C6g\r", 
                "\\05140023C4g\r", 
                "\\05140038F3g\r",
                "\\05090000F8g\r"
            ]
            for cmd in init_cmds:
                self.writer.write(cmd.encode('ascii'))
                await self.writer.drain()
                await asyncio.sleep(0.3)
            
            self._tasks.extend([
                self.hass.loop.create_task(self._listen_loop()), 
                self.hass.loop.create_task(self._heartbeat_loop()), 
                self.hass.loop.create_task(self._sync_loop())
            ])
        except Exception as e:
            _LOGGER.error("C-Bus: Connection failed: %s", e)
            self.is_connected = False
            if not self._intentional_disconnect:
                self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    async def _heartbeat_loop(self):
        while self.is_connected and not self._intentional_disconnect:
            try:
                self.writer.write(b"\\05090038F3g\r")
                await self.writer.drain()
                await asyncio.sleep(45) 
            except Exception: break

    async def _listen_loop(self):
        while self.is_connected and not self._intentional_disconnect:
            try:
                data = await self.reader.read(1024)
                if not data: break
                
                raw_data = data.decode('ascii', errors='ignore').strip().upper()
                if raw_data:
                    if "3800" in raw_data: self._process_event_update(raw_data)
                    elif any(p in raw_data for p in ["F638", "8638", "D838", "D638"]):
                        self._process_mmi_response(raw_data)
                        
            except Exception as e:
                _LOGGER.error("C-Bus: Listen loop error: %s", e)
                await asyncio.sleep(5)
                break
        
        self.is_connected = False
        if not self._intentional_disconnect:
            self._tasks.append(self.hass.loop.create_task(self._reconnect_later()))

    async def _sync_loop(self):
        await asyncio.sleep(3)
        for ga in self.lighting_map:
            if not self.is_connected: break
            base = f"05380003{ga:02X}"
            cmd = f"\\{base}{calculate_cbus_checksum(base)}g\r"
            self.writer.write(cmd.encode('ascii'))
            await self.writer.drain()
            await asyncio.sleep(0.1)

    def _process_mmi_response(self, line):
        try:
            mmi_marker = next((p for p in ["F638", "8638", "D838", "D638"] if p in line), None)
            if not mmi_marker: return
            mmi_idx = line.find(mmi_marker)
            start_ga = int(line[mmi_idx+4:mmi_idx+6], 16)
            idx = mmi_idx + 6
            num_bytes = min((len(line) - idx - 2) // 2, 22)
            for i in range(num_bytes):
                data = int(line[idx+i*2:idx+i*2+2], 16)
                for off in range(4):
                    ga = start_ga + (i * 4) + off
                    if ga in self.lighting_map:
                        is_on = ((data >> (off * 2)) & 0x03) in (1, 3)
                        self.states[ga].update({"state": is_on, "brightness": 255 if is_on else 0})
            self.async_set_updated_data(dict(self.states))
        except: pass

    def _process_event_update(self, line):
        try:
            idx = line.find("3800")
            ga = int(line[idx+6:idx+8], 16)
            if ga in self.states:
                on = line[idx+4:idx+6] != "01"
                self.states[ga].update({"state": on, "brightness": 255 if on else 0})
                self.async_set_updated_data(dict(self.states))
        except: pass

    async def send_command(self, ga, turn_on, brightness=None):
        if not self.writer: return
        level = f"{brightness:02X}" if brightness is not None else ("FF" if turn_on else "00")
        base = f"05380002{ga:02X}{level}"
        cmd = f"\\{base}{calculate_cbus_checksum(base)}g\r"
        self.writer.write(cmd.encode('ascii'))
        await self.writer.drain()