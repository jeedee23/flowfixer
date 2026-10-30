# File: flowfix/core.py
# Purpose: Two-pass, byte-preserving G-code flow processing.
# Version: 0.2.2 | Created: 2026-10-25 | Modified: 2026-10-29
# Changelog: 0.2.2 - Insert a configurable post-homing belt plunge with guards.
# 0.2.0 - Stream lines, preserve bytes/newlines, separate EOF restore.

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import io
from pathlib import Path
import re
from typing import BinaryIO, Iterator

from .models import FlowFixSettings

g1_re = re.compile(rb"^\s*G1\b", re.IGNORECASE)
y_re = re.compile(rb"(?<![A-Z])Y([-+]?\d*\.?\d+)", re.IGNORECASE)
layer_count_re = re.compile(rb";\s*LAYER_COUNT\s*:\s*(\d+)", re.IGNORECASE)
layer_mark_re = re.compile(rb";\s*LAYER\s*:\s*(\d+)", re.IGNORECASE)
m221_re = re.compile(rb"^\s*M221\b", re.IGNORECASE)
command_re = re.compile(rb"^\s*G0*(\d+)(?![\d.])", re.IGNORECASE)


@dataclass(frozen=True)
class ScanResult:
    total_layers: int | None
    marker_count: int
    existing_m221: int
    newline: bytes
    existing_belt_plunge: bool = False

    @property
    def warnings(self) -> list[str]:
        result = []
        if not self.marker_count:
            result.append("No compatible ;LAYER:n markers: layer overrides are unavailable.")
        if self.existing_m221:
            result.append(
                f"Found {self.existing_m221} existing M221 command(s). They are copied "
                "unchanged and do not update FlowFix's tracked flow (baseline policy)."
            )
        return result


def binary_lines(stream: BinaryIO) -> Iterator[bytes]:
    # Latin-1 is a byte-for-byte mapping; newline="" recognizes but never rewrites endings.
    reader = io.TextIOWrapper(stream, encoding="latin-1", newline="")
    try:
        for line in reader:
            yield line.encode("latin-1")
    finally:
        reader.detach()

def startup_code(line: bytes) -> bytes:
    return re.sub(rb"\([^)]*\)", b"", line.split(b";", 1)[0]).strip()


def scan_file(path: Path) -> ScanResult:
    total = maximum = None
    markers = existing = 0
    newline = None
    startup = True
    previous_negative_y_move = False
    existing_belt_plunge = False
    with path.open("rb") as stream:
        for line in binary_lines(stream):
            if newline is None:
                end = re.search(rb"\r\n|\r|\n", line)
                if end:
                    newline = end.group()
            if b";" in line:
                count = layer_count_re.search(line)
                if count:
                    total = int(count.group(1))
                layer = layer_mark_re.search(line)
                if layer:
                    value = int(layer.group(1))
                    maximum = value if maximum is None else max(maximum, value)
                    markers += 1
                    startup = False
            if startup:
                code = startup_code(line)
                command = command_re.match(code)
                if code:
                    number = int(command.group(1)) if command else None
                    y = extract_y_value(code)
                    if previous_negative_y_move and number == 92 and y == 0:
                        existing_belt_plunge = True
                    previous_negative_y_move = number in (0, 1) and y is not None and y < 0
            existing += bool(m221_re.match(line))
    if total is None and maximum is not None:
        total = maximum + 1
    return ScanResult(total, markers, existing, newline or b"\n", existing_belt_plunge)


def extract_y_value(line: bytes | str) -> float | None:
    raw = line.encode("utf-8") if isinstance(line, str) else line
    match = y_re.search(raw.split(b";", 1)[0])
    return float(match.group(1)) if match else None


def matching_between_flow(layer_index: int | None, settings: FlowFixSettings) -> int | None:
    if layer_index is not None:
        for item in settings.between_ranges:
            if item.between_layer <= layer_index <= item.until_layer:
                return item.flow_between
    return None


def layer_restore_flow(
    layer_index: int | None, total_layers: int | None, settings: FlowFixSettings
) -> int:
    between = matching_between_flow(layer_index, settings)
    if between is not None:
        return between
    if layer_index is None or total_layers is None:
        return settings.restore_flow_percent
    if settings.number_first_layers > 0 and layer_index < settings.number_first_layers:
        return settings.first_layers_restore_flow_percent
    if settings.number_last_layers > 0 and layer_index >= max(
        0, total_layers - settings.number_last_layers
    ):
        return settings.last_layers_restore_flow_percent
    return settings.restore_flow_percent


def transform_file(
    input_path: Path, output: BinaryIO, settings: FlowFixSettings
) -> dict:
    settings.validate()
    scan = scan_file(input_path)
    if settings.plunge_into_belt_mm > 0 and scan.existing_belt_plunge:
        raise ValueError(
            "Startup already contains a negative-Y move followed by G92 Y0. "
            "Remove that plunge from the slicer's start code before enabling "
            "Plunge into belt, or set Plunge into belt to 0 to preserve it."
        )
    current_flow = settings.restore_flow_percent
    layer = None
    restore = layer_restore_flow(None, scan.total_layers, settings)
    low_inserts = restore_inserts = 0
    terminated = True
    plunge_inserted = False
    startup = True
    absolute = metric = True
    with input_path.open("rb") as source:
        for line in binary_lines(source):
            marker = layer_mark_re.search(line) if b";" in line else None
            if marker:
                startup = False
                new_layer = int(marker.group(1))
                if new_layer != layer:
                    layer = new_layer
                    restore = layer_restore_flow(layer, scan.total_layers, settings)
            if g1_re.match(line):
                y = extract_y_value(line)
                if y is not None:
                    low = y < settings.threshold_y
                    target = settings.low_flow_percent if low else restore
                    if target != current_flow:
                        output.write(f"M221 S{target}".encode("ascii") + scan.newline)
                        current_flow = target
                        low_inserts += int(low)
                        restore_inserts += int(not low)
            output.write(line)
            terminated = line.endswith((b"\n", b"\r"))
            if startup and settings.plunge_into_belt_mm > 0 and not plunge_inserted:
                code = startup_code(line)
                command = command_re.match(code)
                number = int(command.group(1)) if command else None
                if number in (90, 91):
                    absolute = number == 90
                elif number in (20, 21):
                    metric = number == 21
                elif number == 28:
                    axes = set(re.findall(rb"[XYZ]", code[command.end():].upper()))
                    if not axes or b"Y" in axes:
                        if not absolute or not metric:
                            raise ValueError(
                                "Plunge into belt requires G90 absolute positioning "
                                "and G21 millimetres at Y homing. Correct the start "
                                "code or set plunge depth to 0."
                            )
                        if not terminated:
                            output.write(scan.newline)
                        depth = format(Decimal(str(settings.plunge_into_belt_mm)), "f")
                        output.write(
                            f"G0 Y-{depth}".encode("ascii") + scan.newline
                            + b"G92 Y0" + scan.newline
                        )
                        plunge_inserted = True
                        terminated = True
    if current_flow != restore:
        if not terminated:
            output.write(scan.newline)
        output.write(f"M221 S{restore}".encode("ascii") + scan.newline)
        restore_inserts += 1
    warnings = scan.warnings
    if settings.plunge_into_belt_mm > 0 and not plunge_inserted:
        warnings.append(
            "Plunge not inserted: no startup G28 homing Y was found before the first layer."
        )
    return {
        "total_layers_detected": scan.total_layers,
        "layer_marker_count": scan.marker_count,
        "existing_m221": scan.existing_m221,
        "inserted_low_m221": low_inserts,
        "inserted_restore_m221": restore_inserts,
        "inserted_belt_plunges": int(plunge_inserted),
        "warnings": warnings,
    }


def flowfix_file(input_path: Path, settings: FlowFixSettings) -> dict:
    """Compatibility helper; exclusive output creation prevents silent overwrites."""
    settings.validate()
    output_path = input_path.with_name(
        input_path.stem + settings.output_suffix + input_path.suffix
    )
    with output_path.open("xb") as output:
        try:
            result = transform_file(input_path, output, settings)
        except (OSError, ValueError):
            output.close()
            output_path.unlink()
            raise
    return {
        **result,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "settings": asdict(settings),
    }
