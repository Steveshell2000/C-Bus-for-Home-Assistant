"""Tests for pure C-Bus Lighting Application protocol handling."""

from __future__ import annotations

import unittest

from protocol import (
    LIGHT_OFF,
    LIGHT_ON,
    TERMINATE_RAMP,
    build_lighting_command,
    calculate_cbus_checksum,
    interpolate_ramp_level,
    parse_lighting_event,
    ramp_command_for_transition,
    ramp_duration_seconds,
)


def _received_frame(*data: int) -> str:
    checksum = (-sum(data)) & 0xFF
    return "".join(f"{byte:02X}" for byte in (*data, checksum))


class ProtocolTests(unittest.TestCase):
    def test_checksum_matches_clipsal_documented_vector(self) -> None:
        self.assertEqual(calculate_cbus_checksum("0538007988"), "C2")

    def test_build_lighting_command(self) -> None:
        vectors = [
            (LIGHT_ON, None, "\\0538007988C2g\r"),
            (LIGHT_OFF, None, "\\05380001883Ag\r"),
            (0x12, 0x80, "\\053800128880A9g\r"),
            (TERMINATE_RAMP, None, "\\053800098832g\r"),
        ]
        for command, target, expected in vectors:
            with self.subTest(command=command, target=target):
                self.assertEqual(
                    build_lighting_command(0x88, command, target), expected
                )

    def test_transition_is_scaled_for_actual_brightness_delta(self) -> None:
        self.assertEqual(ramp_command_for_transition(4, 0, 255), 0x0A)
        self.assertEqual(ramp_command_for_transition(4, 127, 255), 0x12)
        self.assertEqual(ramp_command_for_transition(0, 0, 255), 0x02)
        self.assertEqual(ramp_command_for_transition(30, 100, 100), 0x02)

    def test_ramp_duration_uses_full_scale_rate(self) -> None:
        self.assertEqual(ramp_duration_seconds(0x0A, 0, 255), 4)
        self.assertAlmostEqual(
            ramp_duration_seconds(0x12, 127, 255), 4.0157, places=4
        )

    def test_interpolated_level_moves_smoothly_and_clamps(self) -> None:
        levels = [
            interpolate_ramp_level(0, 255, elapsed, 4) for elapsed in range(5)
        ]
        self.assertEqual(levels, [0, 64, 128, 191, 255])

        descending = [
            interpolate_ramp_level(255, 0, elapsed, 4) for elapsed in range(5)
        ]
        self.assertEqual(descending, [255, 191, 128, 64, 0])

    def test_parse_on_and_off_events(self) -> None:
        on = parse_lighting_event(
            _received_frame(0x05, 0x23, 0x38, 0x00, 0x79, 0x88)
        )
        off = parse_lighting_event(
            _received_frame(0x05, 0x23, 0x38, 0x00, 0x01, 0x88)
        )

        self.assertIsNotNone(on)
        assert on is not None
        self.assertEqual(
            (on.command, on.group_address, on.target_level), ("on", 0x88, 255)
        )
        self.assertIsNotNone(off)
        assert off is not None
        self.assertEqual(
            (off.command, off.group_address, off.target_level), ("off", 0x88, 0)
        )

    def test_parse_ramp_in_both_documented_receive_forms(self) -> None:
        standard = parse_lighting_event(
            _received_frame(0x05, 0x23, 0x38, 0x00, 0x12, 0x88, 0x80)
        )
        alternate = parse_lighting_event(
            _received_frame(0x05, 0x23, 0x38, 0x01, 0x00, 0x0A, 0x88, 0x40)
        )

        self.assertIsNotNone(standard)
        assert standard is not None
        self.assertEqual(standard.command, "ramp")
        self.assertEqual(standard.ramp_command, 0x12)
        self.assertEqual(standard.ramp_rate_seconds, 8)
        self.assertEqual(standard.target_level, 0x80)

        self.assertIsNotNone(alternate)
        assert alternate is not None
        self.assertEqual(alternate.command, "ramp")
        self.assertEqual(alternate.ramp_command, 0x0A)
        self.assertEqual(alternate.target_level, 0x40)

    def test_parse_terminate_ramp_event(self) -> None:
        event = parse_lighting_event(
            _received_frame(0x05, 0x23, 0x38, 0x00, 0x09, 0x88)
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.command, "terminate")
        self.assertEqual(event.group_address, 0x88)
        self.assertIsNone(event.target_level)

    def test_parse_rejects_bad_checksum_and_unknown_command(self) -> None:
        self.assertIsNone(parse_lighting_event("0523380012888000"))
        self.assertIsNone(
            parse_lighting_event(
                _received_frame(0x05, 0x23, 0x38, 0x00, 0x7B, 0x88)
            )
        )


if __name__ == "__main__":
    unittest.main()
