"""Tests for pure C-Bus Lighting Application protocol handling."""

from __future__ import annotations

import unittest

from protocol import (
    LIGHT_OFF,
    LIGHT_ON,
    LEVEL_STATUS_NIBBLE_CODES,
    TERMINATE_RAMP,
    build_level_status_request,
    build_lighting_command,
    calculate_cbus_checksum,
    interpolate_ramp_level,
    parse_level_status_response,
    parse_lighting_event,
    ramp_command_for_transition,
    ramp_duration_seconds,
)


def _received_frame(*data: int) -> str:
    checksum = (-sum(data)) & 0xFF
    return "".join(f"{byte:02X}" for byte in (*data, checksum))


def _level_status_frame(start_group: int, levels: list[int]) -> str:
    data = [0x86, 0xFE, 0xFE, 0x00, 0xF7, 0x07, 0x38, start_group]
    for level in levels:
        data.extend(
            (
                LEVEL_STATUS_NIBBLE_CODES[level & 0x0F],
                LEVEL_STATUS_NIBBLE_CODES[level >> 4],
            )
        )
    return _received_frame(*data)


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

    def test_build_level_status_request(self) -> None:
        self.assertEqual(
            build_level_status_request(0), "\\05FF00730738004Ag\r"
        )
        self.assertEqual(
            build_level_status_request(0x20), "\\05FF00730738202Ag\r"
        )
        with self.assertRaises(ValueError):
            build_level_status_request(1)

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

    def test_parse_exact_level_status_block(self) -> None:
        expected = [0, 1, 17, 64, 127, 128, 200, 254, 255]
        block = parse_level_status_response(_level_status_frame(0x20, expected))

        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.application, 0x38)
        self.assertEqual(block.start_group, 0x20)
        self.assertEqual(
            block.levels,
            {0x20 + index: level for index, level in enumerate(expected)},
        )

    def test_parse_level_status_after_same_line_acknowledgement(self) -> None:
        frame = _level_status_frame(0, [0, 42, 137, 255])

        block = parse_level_status_response(f"g.\\{frame}\r")

        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.levels, {0: 0, 1: 42, 2: 137, 3: 255})

    def test_level_status_skips_invalid_pairs_and_bad_checksum(self) -> None:
        frame = _level_status_frame(0, [10, 20, 30])
        invalid_pair = frame[:20] + "00" + frame[22:]
        checksum = calculate_cbus_checksum(invalid_pair[:-2])
        invalid_pair = invalid_pair[:-2] + checksum

        block = parse_level_status_response(invalid_pair)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertNotIn(1, block.levels)
        self.assertEqual(block.levels[0], 10)
        self.assertEqual(block.levels[2], 30)

        self.assertIsNone(parse_level_status_response(frame[:-2] + "00"))


if __name__ == "__main__":
    unittest.main()
