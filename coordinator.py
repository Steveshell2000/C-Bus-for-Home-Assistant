import asyncio
import logging
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

class CBusCoordinator(DataUpdateCoordinator):
    """Handles raw persistent connection to the CNI framework."""

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
            _LOGGER.info("Opening raw connection socket to C-Bus CNI at %s:%s", self.host, self.port)
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            
            # Fire concurrent loops for active tracking and keep-alives
            self.hass.loop.create_task(self._listen_loop())
            self.hass.loop.create_task(self._heartbeat_loop())
        except Exception as err:
            _LOGGER.error("Failed to establish raw stream to CNI: %s", err)
            self.is_connected = False

    async def disconnect(self):
        """Clean connection drop wrapped safely against network faults."""
        self.is_connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as err:
                _LOGGER.debug("Socket closed with outstanding exception: %s", err)
            finally:
                self.writer = None
                self.reader = None

    async def _heartbeat_loop(self):
        """Maintains socket integrity against aggressive server timeouts."""
        while self.is_connected:
            try:
                if self.writer:
                    self.writer.write(bytes.fromhex("050000000000"))
                    await self.writer.drain()
            except Exception as err:
                _LOGGER.error("Keep-alive dropped: %s. Reconnecting...", err)
                self.is_connected = False
                break
            await asyncio.sleep(30)
        
        if not self.is_connected:
            await asyncio.sleep(5)
            if self.is_connected is False:  # Ensure a concurrent connection isn't running
                await self.connect()

    async def _listen_loop(self):
        """Direct stream monitor decoding packet notifications on the fly."""
        while self.is_connected:
            try:
                if not self.reader:
                    break
                data = await self.reader.read(1024)
                if not data:
                    _LOGGER.warning("CNI interface closed stream cleanly.")
                    self.is_connected = False
                    break

                # Parse operational binary payloads (0x38 stands for App 56 Lighting)
                if len(data) >= 4 and data[1] == 0x38:
                    ga = int(data[2])
                    command = data[3]
                    
                    # 0x79 maps to standard ON; 0x01 maps to standard OFF
                    is_on = True if command == 0x79 or command > 0x01 else False
                    
                    self.states[ga] = is_on
                    self.async_set_updated_data(self.states)
            except Exception as err:
                _LOGGER.error("Error encountered inside serial listener loop: %s", err)
                break

    async def send_command(self, ga: int, turn_on: bool):
        """Dispatches lighting state adjustments directly onto the live network."""
        if not self.is_connected or not self.writer:
            _LOGGER.error("CNI engine currently offline. Dropping packet request.")
            return

        try:
            cmd_byte = "79" if turn_on else "01"
            packet_hex = f"0538{ga:02X}{cmd_byte}00"
            
            self.writer.write(bytes.fromhex(packet_hex))
            await self.writer.drain()
            
            # Optimistically push localized state to avoid laggy visual loop responses
            self.states[ga] = turn_on
            self.async_set_updated_data(self.states)
        except Exception as err:
            _LOGGER.error("Failed handling outgoing physical transmission: %s", err)