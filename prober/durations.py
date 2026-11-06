"""Duration parsing utilities supporting human-readable strings (e.g. '200ms', '10s', '4m')."""

from __future__ import annotations

import re

_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?\s*$",
    re.IGNORECASE,
)

_UNIT_MULTIPLIERS_MS: dict[str, float] = {
    "ms": 1.0,
    "s": 1000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
}


def parse_duration_ms(value: str | int | float) -> float:
    """Parse a duration specification into milliseconds.

    Numeric inputs are treated as milliseconds.
    String inputs without a unit default to milliseconds; supported units: ms, s, m, h.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0:
            raise ValueError(f"Duration must be non-negative, got {value}")
        return float(value)

    if not isinstance(value, str):
        raise ValueError(f"Unsupported duration type: {type(value).__name__}")

    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(
            f"Invalid duration format {value!r}. Expected formats like '200ms', '10s', '4m'."
        )

    amount = float(match.group("value"))
    unit = (match.group("unit") or "ms").lower()
    return amount * _UNIT_MULTIPLIERS_MS[unit]


def parse_duration_seconds(value: str | int | float) -> float:
    """Parse a duration specification into seconds."""
    return parse_duration_ms(value) / 1000.0
