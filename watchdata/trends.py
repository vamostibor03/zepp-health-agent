"""Trend analysis: compare a target day against trailing 7/30/90-day windows."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import METRIC_LABELS, DailyMetrics
from .storage import Storage

WINDOWS = (7, 30, 90)


@dataclass
class MetricTrend:
    metric: str
    label: str
    unit: str
    value: Optional[float]
    # Per-window statistics keyed by window size (7/30/90).
    averages: dict[int, Optional[float]] = field(default_factory=dict)
    deltas: dict[int, Optional[float]] = field(default_factory=dict)
    pct_change: dict[int, Optional[float]] = field(default_factory=dict)
    sample_sizes: dict[int, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "label": self.label,
            "unit": self.unit,
            "value": self.value,
            "averages": self.averages,
            "deltas": self.deltas,
            "pct_change": self.pct_change,
            "sample_sizes": self.sample_sizes,
        }


@dataclass
class TrendReport:
    day: date
    metrics: dict[str, MetricTrend]

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "windows": list(WINDOWS),
            "metrics": {k: v.as_dict() for k, v in self.metrics.items()},
        }


def _mean(values: list[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def compute_trends(
    storage: Storage, target: DailyMetrics, windows: tuple[int, ...] = WINDOWS
) -> TrendReport:
    """Compare ``target``'s metrics against trailing-window averages.

    Windows exclude the target day itself so we compare "today vs the recent
    baseline". History is pulled once for the largest window and sliced down.
    """
    max_window = max(windows)
    history = storage.get_previous_n_days(target.day, max_window, inclusive=False)

    # Pre-index history newest-first for easy window slicing.
    history_sorted = sorted(history, key=lambda m: m.day, reverse=True)

    target_values = target.numeric_fields()
    metrics: dict[str, MetricTrend] = {}

    # Consider every metric the target has, plus any seen in history.
    metric_names = set(target_values)
    for m in history_sorted:
        metric_names.update(m.numeric_fields())

    for name in metric_names:
        meta = METRIC_LABELS.get(name, {"label": name, "unit": ""})
        value = target_values.get(name)
        trend = MetricTrend(
            metric=name,
            label=meta["label"],
            unit=meta["unit"],
            value=value,
        )
        for window in windows:
            window_days = history_sorted[:window]
            samples = [
                d.numeric_fields()[name]
                for d in window_days
                if name in d.numeric_fields()
            ]
            avg = _mean(samples)
            trend.averages[window] = avg
            trend.sample_sizes[window] = len(samples)
            if value is not None and avg is not None:
                delta = value - avg
                trend.deltas[window] = delta
                trend.pct_change[window] = (
                    (delta / avg * 100.0) if avg != 0 else None
                )
            else:
                trend.deltas[window] = None
                trend.pct_change[window] = None
        metrics[name] = trend

    # Stable, human-sensible ordering following METRIC_LABELS.
    ordered = {
        name: metrics[name] for name in METRIC_LABELS if name in metrics
    }
    for name in metrics:  # append any extras not in the label map
        ordered.setdefault(name, metrics[name])

    return TrendReport(day=target.day, metrics=ordered)
