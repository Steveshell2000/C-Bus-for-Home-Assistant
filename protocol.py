"""Pure C-Bus Lighting Application protocol helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Final, Literal

LIGHT_ON: Final = 0x79
LIGHT_OFF: Final = 0x01
TERMINATE_RAMP: Final = 0x09

# C-Bus encodes the full-scale (0 to 255) ramp time in the command byte.
RAMP_RATE_SECONDS: Final[dict[int, float]] = {
    0x02: 0.0,
    0x0A: 4.0,
    0x12: 8.0,
    0x1A: 12.0,
    0x22: 20.0,
    0x2A: 30.0,
    0x32: 40.0,
    0x3A: 60.0,
    0x42: 90.0,
    0x4A: 120.0,
    0x52: 180.0,
    0x5A: 300.0,
    0x62: 420.0,
    0x6A: 600.0,
    0x72: 900.0,
    0x7A: 1020.0,
}


@dataclass(frozen=True, slots=True)
class LightingEvent:
    """A supported C-Bus Lighting Application event."""

    command: Literal["on", "off", "ramp", "terminate"]
    group_address: int
    target_level: int | None = None
    ramp_command: int | None = None
    ramp_rate_seconds: float | None = None


def calculate_cbus_checksum(hex_string: str) -> str:
    """Return the two's-complement checksum for an even-length hex string."""
    if len(hex_string) % 2:
        raise ValueError("C-Bus checksum input must contain complete bytes")

    try:
        total = sum(
            int(hex_string[index : index + 2], 16)
            for index in range(0, len(hex_string), 2)
        )
    except ValueError as err:
        raise ValueError("C-Bus checksum input must be hexadecimal") from err

    return f"{(-total) & 0xFF:02X}"


def build_lighting_command(
    group_address: int,
    command: int,
    target_level: int | None = None,
    *,
    tag: str = "g",
) -> str:
    """Build one ASCII-framed C-Bus Lighting Application command."""
    if not 0 <= group_address <= 0xFE:
        raise ValueError("C-Bus group address must be in the range 0..254")
    if command not in {LIGHT_ON, LIGHT_OFF, TERMINATE_RAMP, *RAMP_RATE_SECONDS}:
        raise ValueError(f"Unsupported C-Bus lighting command: 0x{command:02X}")
    if len(tag) != 1 or tag < "g" or tag > "z":
        raise ValueError("C-Bus confirmation tag must be one lowercase letter g..z")

    if command in RAMP_RATE_SECONDS:
        if target_level is None or not 0 <= target_level <= 0xFF:
            raise ValueError("Ramp commands require a target level in the range 0..255")
        parameters = f"{group_address:02X}{target_level:02X}"
    else:
        if target_level is not None:
            raise ValueError("Only ramp commands accept a target level")
        parameters = f"{group_address:02X}"

    payload = f"053800{command:02X}{parameters}"
    return f"\\{payload}{calculate_cbus_checksum(payload)}{tag}\r"


def ramp_command_for_transition(
    transition_seconds: float | int | None,
    start_level: int,
    target_level: int,
) -> int:
    """Choose the closest C-Bus ramp rate for a requested HA transition.

    Home Assistant expresses the desired time from the current level to the target.
    C-Bus expresses the time for a full-scale 0-to-255 ramp, so the requested time
    is scaled by the actual brightness delta before selecting a protocol rate.
    """
    _validate_level(start_level)
    _validate_level(target_level)

    if transition_seconds is None:
        return 0x02

    transition = float(transition_seconds)
    if not math.isfinite(transition) or transition <= 0:
        return 0x02

    delta = abs(target_level - start_level)
    if delta == 0:
        return 0x02

    requested_full_scale = transition * 255.0 / delta
    return min(
        RAMP_RATE_SECONDS,
        key=lambda command: (
            abs(RAMP_RATE_SECONDS[command] - requested_full_scale),
            RAMP_RATE_SECONDS[command],
        ),
    )


def ramp_duration_seconds(command: int, start_level: int, target_level: int) -> float:
    """Return the actual duration for a C-Bus ramp across the given level delta."""
    _validate_level(start_level)
    _validate_level(target_level)
    try:
        full_scale_seconds = RAMP_RATE_SECONDS[command]
    except KeyError as err:
        raise ValueError(f"Not a C-Bus ramp command: 0x{command:02X}") from err
    return full_scale_seconds * abs(target_level - start_level) / 255.0


def interpolate_ramp_level(
    start_level: int,
    target_level: int,
    elapsed_seconds: float,
    duration_seconds: float,
) -> int:
    """Interpolate a ramp level for smooth Home Assistant state updates."""
    _validate_level(start_level)
    _validate_level(target_level)

    if duration_seconds <= 0 or elapsed_seconds >= duration_seconds:
        return target_level
    if elapsed_seconds <= 0:
        return start_level

    progress = elapsed_seconds / duration_seconds
    return round(start_level + ((target_level - start_level) * progress))


def parse_lighting_event(line: str) -> LightingEvent | None:
    """Parse a supported Lighting Application event forwarded by a PCI/CNI.

    Both documented receive forms are accepted: ``...3800...`` and
    ``...380100...``. Frames with an invalid checksum or an unsupported command
    are ignored.
    """
    match = re.match(r"[0-9A-F]+", line.strip().upper().lstrip("\\"))
    if not match:
        return None

    frame_hex = match.group(0)
    if len(frame_hex) % 2:
        return None

    try:
        frame = bytes.fromhex(frame_hex)
    except ValueError:
        return None

    if len(frame) < 6 or frame[0] != 0x05 or sum(frame) & 0xFF:
        return None

    # Monitored messages include an originating unit byte. Accept the local
    # transmit form as well so recorded frames remain easy to test and replay.
    if len(frame) > 3 and frame[2] == 0x38:
        header_index = 3
    elif frame[1] == 0x38:
        header_index = 2
    else:
        return None

    if frame[header_index] == 0x00:
        command_index = header_index + 1
    elif (
        len(frame) > header_index + 1
        and frame[header_index] == 0x01
        and frame[header_index + 1] == 0x00
    ):
        command_index = header_index + 2
    else:
        return None

    if command_index + 2 >= len(frame):
        return None

    command = frame[command_index]
    group_address = frame[command_index + 1]
    if group_address > 0xFE:
        return None

    if command == LIGHT_ON:
        return LightingEvent("on", group_address, target_level=0xFF)
    if command == LIGHT_OFF:
        return LightingEvent("off", group_address, target_level=0x00)
    if command == TERMINATE_RAMP:
        return LightingEvent("terminate", group_address)
    if command in RAMP_RATE_SECONDS:
        if command_index + 3 >= len(frame):
            return None
        return LightingEvent(
            "ramp",
            group_address,
            target_level=frame[command_index + 2],
            ramp_command=command,
            ramp_rate_seconds=RAMP_RATE_SECONDS[command],
        )
    return None


def _validate_level(level: int) -> None:
    if not 0 <= level <= 0xFF:
        raise ValueError("C-Bus level must be in the range 0..255")
