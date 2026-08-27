"""Build the human-readable report text sent to Telegram."""

from __future__ import annotations

from typing import Optional

from .models import METRIC_LABELS, DailyMetrics
from .trends import TrendReport


def _fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    if abs(value - round(value)) < 1e-9:
        num = f"{int(round(value)):,}"
    else:
        num = f"{value:.1f}"
    return f"{num}{(' ' + unit) if unit else ''}".strip()


def _arrow(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    if pct > 2:
        return "\u2191"  # up
    if pct < -2:
        return "\u2193"  # down
    return "\u2192"  # flat


def build_daily_report(
    target: DailyMetrics, trends: TrendReport, analysis: str
) -> str:
    weekday = target.day.strftime("%A")
    lines = [
        f"\U0001f4ca Daily Health Report \u2014 {weekday}, {target.day.isoformat()}",
        "",
        analysis.strip(),
        "",
        "\u2014" * 12,
        f"Metrics for {target.day.isoformat()} "
        "(value | vs 7d avg | 30d avg | 90d avg):",
    ]
    for name, t in trends.metrics.items():
        if t.value is None:
            continue
        seg = [f"\u2022 {t.label}: {_fmt(t.value, t.unit)}"]
        window_bits = []
        for window in (7, 30, 90):
            avg = t.averages.get(window)
            pct = t.pct_change.get(window)
            if avg is None:
                window_bits.append("-")
            else:
                window_bits.append(f"{_fmt(avg, t.unit)}{_arrow(pct)}")
        seg.append("  |  ".join(window_bits))
        lines.append("   ".join(seg))
    return "\n".join(lines)


def build_prev_day_report(target: DailyMetrics, analysis: str) -> str:
    lines = [
        f"\U0001f4c5 Previous Day Recap \u2014 {target.day.isoformat()}",
        "",
        analysis.strip(),
        "",
        "\u2014" * 12,
        "Metrics:",
    ]
    values = target.numeric_fields()
    for name, meta in METRIC_LABELS.items():
        if name in values:
            lines.append(f"\u2022 {meta['label']}: {_fmt(values[name], meta['unit'])}")
    return "\n".join(lines)
