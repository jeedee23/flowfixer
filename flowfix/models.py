# File: flowfix/models.py
# Purpose: Shared settings, validation, and settings comparisons.
# Version: 0.2.0 | Created: 2026-10-25 | Modified: 2026-10-25
# Changelog: 0.2.0 - Centralize validation outside Tkinter.

from dataclasses import asdict, dataclass, field, fields
import math
from typing import Any


@dataclass(frozen=True)
class BetweenRange:
    between_layer: int
    until_layer: int
    flow_between: int


@dataclass
class FlowFixSettings:
    low_flow_percent: int = 50
    threshold_y: float = 0.5
    restore_flow_percent: int = 100
    number_first_layers: int = 2
    first_layers_restore_flow_percent: int = 92
    number_last_layers: int = 3
    last_layers_restore_flow_percent: int = 97
    between_ranges: list[BetweenRange] = field(default_factory=list)
    output_suffix: str = "_flowfix"

    def validate(self) -> None:
        for name in (
            "low_flow_percent", "restore_flow_percent",
            "first_layers_restore_flow_percent", "last_layers_restore_flow_percent",
        ):
            validate_flow(getattr(self, name), name)
        for name in ("number_first_layers", "number_last_layers"):
            validate_layer(getattr(self, name), name)
        if (
            isinstance(self.threshold_y, bool)
            or not isinstance(self.threshold_y, (int, float))
            or not math.isfinite(self.threshold_y)
        ):
            raise ValueError("Threshold Y must be a finite number.")
        if not isinstance(self.output_suffix, str) or not self.output_suffix.strip():
            raise ValueError("Output suffix cannot be empty.")
        if any(c in self.output_suffix for c in '<>:"/\\|?*\0') or any(
            ord(c) < 32 for c in self.output_suffix
        ):
            raise ValueError("Output suffix must be a portable filename fragment.")
        if not isinstance(self.between_ranges, list):
            raise ValueError("Between ranges must be a list.")
        for item in self.between_ranges:
            if not isinstance(item, BetweenRange):
                raise ValueError("Invalid between range.")
            validate_range(item)
        ordered = sorted(self.between_ranges, key=lambda r: r.between_layer)
        for previous, current in zip(ordered, ordered[1:]):
            if current.between_layer <= previous.until_layer:
                raise ValueError(
                    f"Ranges overlap: {previous.between_layer}-{previous.until_layer} "
                    f"and {current.between_layer}-{current.until_layer} (inclusive)."
                )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FlowFixSettings":
        if not isinstance(value, dict):
            raise ValueError("Settings must be a JSON object.")
        names = {item.name for item in fields(cls)}
        if set(value) != names:
            raise ValueError("Settings fields do not match schema version 1.")
        ranges = value["between_ranges"]
        if not isinstance(ranges, list):
            raise ValueError("Between ranges must be a list.")
        parsed = []
        for item in ranges:
            if not isinstance(item, dict) or set(item) != {
                "between_layer", "until_layer", "flow_between"
            }:
                raise ValueError("Invalid range fields.")
            parsed.append(BetweenRange(**item))
        settings = cls(**{**value, "between_ranges": parsed})
        settings.validate()
        return settings


def validate_layer(value: int, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be an integer of 0 or greater.")


def validate_flow(value: int, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer percentage.")


def validate_range(item: BetweenRange) -> None:
    validate_layer(item.between_layer, "From layer")
    validate_layer(item.until_layer, "Until layer")
    validate_flow(item.flow_between, "Range flow")
    if item.until_layer < item.between_layer:
        raise ValueError("Until layer must be greater than or equal to From layer.")


def settings_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    }
