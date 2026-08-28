"""Normalized data models shared across the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional


@dataclass
class DailyMetrics:
    """A single day's worth of normalized health metrics.

    Every field is optional because devices/accounts differ in what they
    report. ``None`` means "not available" and is handled gracefully by the
    storage and trends layers.
    """

    day: date

    # Activity
    steps: Optional[int] = None
    distance_m: Optional[int] = None
    calories_kcal: Optional[int] = None
    active_minutes: Optional[int] = None

    # Sleep (minutes)
    sleep_total_min: Optional[int] = None
    sleep_deep_min: Optional[int] = None
    sleep_light_min: Optional[int] = None
    sleep_rem_min: Optional[int] = None
    sleep_awake_min: Optional[int] = None
    sleep_score: Optional[int] = None

    # Heart
    resting_hr: Optional[int] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    min_hr: Optional[int] = None

    # Other
    spo2_avg: Optional[int] = None
    stress_avg: Optional[int] = None
    pai: Optional[float] = None

    # Raw payload kept for debugging / future re-parsing.
    raw: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Serializable dict for storage (raw is JSON-encoded separately)."""
        row = asdict(self)
        row["day"] = self.day.isoformat()
        row.pop("raw", None)
        return row

    def numeric_fields(self) -> dict[str, float]:
        """Return only the populated numeric metrics (used for trends)."""
        out: dict[str, float] = {}
        for key, value in asdict(self).items():
            if key in ("day", "raw"):
                continue
            if isinstance(value, (int, float)) and value is not None:
                out[key] = float(value)
        return out


# Manually-logged metrics (not provided by the watch). Recorded via the
# Telegram bot and surfaced through the MCP + transformation summary.
MANUAL_METRICS: dict[str, dict[str, str]] = {
    "weight": {"label": "Weight", "unit": "kg"},
    "waist": {"label": "Waist", "unit": "cm"},
    "chest": {"label": "Chest", "unit": "cm"},
    "hips": {"label": "Hips", "unit": "cm"},
    "neck": {"label": "Neck", "unit": "cm"},
    "bodyfat": {"label": "Body fat", "unit": "%"},
    "calories": {"label": "Calories", "unit": "kcal"},
    "protein": {"label": "Protein", "unit": "g"},
}

# Convenience aliases accepted from the user (mapped to canonical keys above).
MANUAL_ALIASES: dict[str, str] = {
    "wt": "weight",
    "kg": "weight",
    "bf": "bodyfat",
    "bodyfat_pct": "bodyfat",
    "fat": "bodyfat",
    "cal": "calories",
    "cals": "calories",
    "kcal": "calories",
    "calories_kcal": "calories",
    "prot": "protein",
    "protein_g": "protein",
}


def resolve_manual_metric(name: str) -> Optional[str]:
    """Map a user-typed metric name/alias to a canonical manual metric key."""
    key = name.strip().lower()
    if key in MANUAL_METRICS:
        return key
    return MANUAL_ALIASES.get(key)


# Human-friendly labels + formatting metadata for reports.
METRIC_LABELS: dict[str, dict[str, str]] = {
    "steps": {"label": "Steps", "unit": ""},
    "distance_m": {"label": "Distance", "unit": "m"},
    "calories_kcal": {"label": "Calories", "unit": "kcal"},
    "active_minutes": {"label": "Active time", "unit": "min"},
    "sleep_total_min": {"label": "Sleep", "unit": "min"},
    "sleep_deep_min": {"label": "Deep sleep", "unit": "min"},
    "sleep_light_min": {"label": "Light sleep", "unit": "min"},
    "sleep_rem_min": {"label": "REM sleep", "unit": "min"},
    "sleep_awake_min": {"label": "Awake", "unit": "min"},
    "sleep_score": {"label": "Sleep score", "unit": ""},
    "resting_hr": {"label": "Resting HR", "unit": "bpm"},
    "avg_hr": {"label": "Avg HR", "unit": "bpm"},
    "max_hr": {"label": "Max HR", "unit": "bpm"},
    "min_hr": {"label": "Min HR", "unit": "bpm"},
    "spo2_avg": {"label": "SpO2", "unit": "%"},
    "stress_avg": {"label": "Stress", "unit": ""},
    "pai": {"label": "PAI", "unit": ""},
}
