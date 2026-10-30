# File: tests/test_core.py
# Purpose: Regression tests for flow priorities and lossless streaming.
# Version: 0.2.2 | Created: 2026-10-25 | Modified: 2026-10-29
# Changelog: 0.2.2 - Cover post-homing plunge, mode guards and old settings.
# 0.2.0 - Cover baseline semantics and byte-preservation fixes.

import io
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from flowfix.core import binary_lines, flowfix_file, scan_file, transform_file
from flowfix.models import BetweenRange, FlowFixSettings


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "source.gcode"

    def transform(self, data, settings=None):
        self.source.write_bytes(data)
        output = io.BytesIO()
        result = transform_file(self.source, output, settings or FlowFixSettings())
        self.assertEqual(self.source.read_bytes(), data)
        return output.getvalue(), result

    def test_inclusive_ranges_threshold_and_low_priority(self):
        settings = FlowFixSettings(between_ranges=[
            BetweenRange(1, 20, 70), BetweenRange(234, 300, 80),
        ])
        data = b";LAYER_COUNT:320\n"
        for layer in (0, 1, 20, 21, 234, 300, 301, 319):
            data += f";LAYER:{layer}\nG1 Y0.1\nG1 Y0.5\n".encode()
        output, result = self.transform(data, settings)
        for layer, flow in ((0, 92), (1, 70), (20, 70), (21, 100),
                            (234, 80), (300, 80), (301, 100), (319, 97)):
            self.assertIn(
                f";LAYER:{layer}\nM221 S50\nG1 Y0.1\nM221 S{flow}\nG1 Y0.5\n".encode(),
                output,
            )
        self.assertEqual(result["inserted_low_m221"], 8)
        self.assertEqual(result["inserted_restore_m221"], 8)

    def test_lossless_newlines_and_non_utf8(self):
        for ending in (b"\n", b"\r\n", b"\r"):
            with self.subTest(ending=ending):
                data = ending.join((b";LAYER_COUNT:10", b";LAYER:5", b";\xff", b"G1 Y1", b""))
                output, _ = self.transform(data)
                self.assertEqual(output, data)
                output, _ = self.transform(data.replace(b"Y1", b"Y0"))
                self.assertIn(b"M221 S50" + ending, output)
                self.assertIn(b";\xff" + ending, output)
                self.assertTrue(output.endswith(b"M221 S100" + ending))

    def test_missing_trailing_newline_is_separated(self):
        output, _ = self.transform(b";LAYER_COUNT:10\n;LAYER:5\nG1 Y0.1")
        self.assertTrue(output.endswith(b"G1 Y0.1\nM221 S100\n"))

    def test_mixed_endings_preserved(self):
        data = b";LAYER_COUNT:10\r\n;LAYER:5\n;\xff\rG1 Y1"
        output, _ = self.transform(data)
        self.assertEqual(data, output)

    def test_no_markers_warning_and_explicit_y_only(self):
        data = b"G1 Y0.1\nG1 X2\nG0 Y1\nG1 X3 ;Y1\nG1 Y1\n"
        output, result = self.transform(data)
        self.assertEqual(output, b"M221 S50\n" + data[:-6] + b"M221 S100\nG1 Y1\n")
        self.assertIsNone(result["total_layers_detected"])
        self.assertTrue(result["warnings"])

    def test_existing_m221_policy_is_baseline_not_modeled(self):
        data = b";LAYER_COUNT:10\n;LAYER:5\nM221 S80\nG1 Y1\n"
        output, result = self.transform(data)
        self.assertEqual(output, data)
        self.assertEqual(result["existing_m221"], 1)
        self.assertTrue(result["warnings"])

    def test_first_overrides_last_when_they_overlap(self):
        output, _ = self.transform(b";LAYER_COUNT:1\n;LAYER:0\nG1 Y1\n")
        self.assertIn(b"M221 S92\nG1", output)

    def test_inferred_count_and_last_active_layer(self):
        output, result = self.transform(b";LAYER:8\nG1 Y0\n;end\n")
        self.assertEqual(result["total_layers_detected"], 9)
        self.assertTrue(output.endswith(b";end\nM221 S97\n"))

    def test_empty_and_noneligible_files_unchanged(self):
        for data in (b"", b"; comment \xff", b"G0 Y0\nG1 X3\n"):
            self.assertEqual(self.transform(data)[0], data)

    def test_scanner_and_binary_chunk_boundaries(self):
        for data in (b"x" * 65535 + b"\r\nz\r", b"\r\n", b"\r", b"x" * 150000):
            lines = list(binary_lines(io.BytesIO(data)))
            self.assertEqual(b"".join(lines), data)
            if b"\r\n" in data:
                self.assertTrue(lines[0].endswith(b"\r\n"))

    def test_compatibility_output_never_overwrites(self):
        self.source.write_bytes(b"G1 Y0\n")
        settings = FlowFixSettings()
        result = flowfix_file(self.source, settings)
        output = Path(result["output_path"])
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            flowfix_file(self.source, settings)
        self.assertEqual(output.read_bytes(), before)

    def test_validation_is_shared_and_strict(self):
        for settings in (
            FlowFixSettings(threshold_y=float("nan")),
            FlowFixSettings(threshold_y=float("inf")),
            FlowFixSettings(low_flow_percent=0),
            FlowFixSettings(number_first_layers=-1),
            FlowFixSettings(low_flow_percent=True),
            FlowFixSettings(output_suffix="../source"),
            FlowFixSettings(output_suffix=""),
            FlowFixSettings(between_ranges=[BetweenRange(1, 20, 70), BetweenRange(20, 30, 80)]),
            FlowFixSettings(between_ranges=[BetweenRange(-1, 3, 70)]),
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()
        settings = FlowFixSettings(between_ranges=[
            BetweenRange(1, 20, 70), BetweenRange(21, 30, 80),
        ])
        self.assertEqual(FlowFixSettings.from_dict(settings.to_dict()), settings)
        with self.assertRaises(ValueError):
            FlowFixSettings.from_dict({})

    def test_large_file_peak_processing_memory_below_two_mib(self):
        with self.source.open("wb") as stream:
            stream.write(b";LAYER_COUNT:10\n;LAYER:5\n")
            chunk = b"G1 X1 Y0.1 E0.01\nG1 X2 Y1 E0.01\n" * 1000
            for _ in range(100):
                stream.write(chunk)
        target = self.source.with_suffix(".output")
        tracemalloc.start()
        try:
            with target.open("wb") as output:
                transform_file(self.source, output, FlowFixSettings())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_default_plunge_immediately_after_homing(self):
        data = b"G90\nG21\nG28 ;home\nG92 E0\n;LAYER:0\nG1 Y1\n"
        output, result = self.transform(data)
        self.assertIn(b"G28 ;home\nG0 Y-0.8\nG92 Y0\nG92 E0\n", output)
        self.assertEqual(result["inserted_belt_plunges"], 1)

    def test_custom_plunge_newlines_and_homing_at_eof(self):
        for ending in (b"\n", b"\r\n", b"\r"):
            for trailing in (ending, b""):
                with self.subTest(ending=ending, trailing=trailing):
                    output, result = self.transform(
                        b"G21" + ending + b"G90" + ending + b"g28 y0" + trailing,
                        FlowFixSettings(plunge_into_belt_mm=1.25),
                    )
                    self.assertTrue(output.endswith(
                        b"g28 y0" + ending + b"G0 Y-1.25" + ending + b"G92 Y0" + ending
                    ))
                    self.assertEqual(result["inserted_belt_plunges"], 1)

    def test_axis_specific_homing_and_one_plunge_only(self):
        output, result = self.transform(b"G28 X\nG28 Z\nG28 Y\nG28\n;LAYER:0\nG28\n")
        self.assertTrue(output.startswith(b"G28 X\nG28 Z\nG28 Y\nG0 Y-0.8\nG92 Y0\nG28\n"))
        self.assertEqual(output.count(b"G0 Y-0.8"), 1)
        self.assertEqual(result["inserted_belt_plunges"], 1)

    def test_plunge_disabled_leaves_homing_unchanged(self):
        data = b"G28\nG0 Y-0.7\nG92 Y0\n"
        output, result = self.transform(data, FlowFixSettings(plunge_into_belt_mm=0))
        self.assertEqual(output, data)
        self.assertEqual(result["inserted_belt_plunges"], 0)
        self.assertFalse(any("Plunge not inserted" in w for w in result["warnings"]))

    def test_existing_startup_plunge_is_rejected_not_doubled(self):
        for movement in (b"G0", b"G1"):
            with self.subTest(movement=movement), self.assertRaisesRegex(ValueError, "already contains"):
                self.transform(b"G28\n" + movement + b" Y-0.7\n; offset\nG92 Y0\n;LAYER:0\n")

    def test_plunge_rejects_relative_or_inch_modes(self):
        for mode in (b"G91", b"G20"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "requires G90"):
                self.transform(mode + b"\nG28\n")
        output, _ = self.transform(b"G91\nG20\nG90\nG21\nG28\n")
        self.assertIn(b"G28\nG0 Y-0.8\nG92 Y0\n", output)

    def test_missing_startup_y_homing_warns_and_does_not_invent_plunge(self):
        for data in (
            b";G28\n;LAYER:0\nG28\n", b"G28 X Z\n", b"G280\n", b"G28.1\n",
        ):
            with self.subTest(data=data):
                baseline, _ = self.transform(data, FlowFixSettings(plunge_into_belt_mm=0))
                output, result = self.transform(data)
                self.assertEqual(output, baseline)
                self.assertEqual(result["inserted_belt_plunges"], 0)
                self.assertTrue(any("Plunge not inserted" in w for w in result["warnings"]))

    def test_homing_comments_do_not_change_axis_detection(self):
        output, _ = self.transform(b"G28 X (Y not homed)\nG28 (home all)\n")
        self.assertEqual(output, b"G28 X (Y not homed)\nG28 (home all)\nG0 Y-0.8\nG92 Y0\n")

    def test_legacy_settings_restore_without_enabling_plunge(self):
        legacy = FlowFixSettings().to_dict()
        del legacy["plunge_into_belt_mm"]
        loaded = FlowFixSettings.from_dict(legacy)
        self.assertEqual(loaded.plunge_into_belt_mm, 0)
        output, result = self.transform(b"G28\n", loaded)
        self.assertEqual(output, b"G28\n")
        self.assertEqual(result["inserted_belt_plunges"], 0)

    def test_plunge_validation(self):
        for value in (-0.8, float("nan"), float("inf"), True, "0.8"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Plunge into belt"):
                FlowFixSettings(plunge_into_belt_mm=value).validate()
        for value in (0, 0.8, 1):
            settings = FlowFixSettings(plunge_into_belt_mm=value)
            self.assertEqual(FlowFixSettings.from_dict(settings.to_dict()), settings)


if __name__ == "__main__":
    unittest.main()
