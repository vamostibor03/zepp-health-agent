"""Build the human-readable report sent to Telegram.

The message uses Telegram's HTML parse mode (see ``TelegramNotifier``) so we can
use <b>bold</b> section headers. Any free-text coming from the LLM is HTML-escaped
before insertion.
"""

from __future__ import annotations

import html
from typing import Callable, Optional

from .models import METRIC_LABELS, DailyMetrics
from .trends import TrendReport


def _fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "\u2014"
    if abs(value - round(value)) < 1e-9:
        num = f"{int(round(value)):,}"
    else:
        num = f"{value:.1f}"
    return f"{num}{(' ' + unit) if unit else ''}".strip()


def _fmt_duration(minutes: Optional[float]) -> str:
    """523 -> '8h 43m', 78 -> '1h 18m', 45 -> '45m'."""
    if minutes is None:
        return "\u2014"
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _fmt_km(meters: Optional[float]) -> str:
    if meters is None:
        return "\u2014"
    return f"{meters / 1000:.2f} km"


def _trend_vs_week(t) -> str:
    """A short, plain-language comparison against the trailing 7-day average.

    e.g. '\U0001f7e2 +12% vs last week' / '\U0001f534 -77% vs last week'.
    Direction is colored neutrally by change (up=green, down=red) rather than
    guessing whether a change is 'good'.
    """
    pct = t.pct_change.get(7) if t else None
    if pct is None:
        return ""
    if pct > 2:
        icon = "\U0001f7e2"  # green circle
    elif pct < -2:
        icon = "\U0001f534"  # red circle
    else:
        icon = "\u26aa"  # white circle (flat)
    return f"{icon} {pct:+.0f}% vs last week"


def _line(
    trends: TrendReport,
    name: str,
    formatter: Optional[Callable[[Optional[float]], str]] = None,
) -> Optional[str]:
    """Render one metric bullet, or None if the metric is absent."""
    t = trends.metrics.get(name)
    if t is None or t.value is None:
        return None
    value = formatter(t.value) if formatter else _fmt(t.value, t.unit)
    trend = _trend_vs_week(t)
    bullet = f"\u2022 {t.label}: <b>{value}</b>"
    return f"{bullet}   {trend}" if trend else bullet


def _has_sleep(trends: TrendReport) -> bool:
    t = trends.metrics.get("sleep_total_min")
    return bool(t and t.value)


def build_daily_report(
    target: DailyMetrics, trends: TrendReport, analysis: str
) -> str:
    weekday = target.day.strftime("%A")
    pretty_date = target.day.strftime("%d %b %Y")
    lines: list[str] = [
        f"\U0001f4ca <b>Daily Health Report</b>",
        f"\U0001f4c5 {weekday}, {pretty_date}",
        "",
    ]

    # --- Sleep --------------------------------------------------------------
    lines.append("\U0001f634 <b>Sleep</b>")
    if _has_sleep(trends):
        total = trends.metrics["sleep_total_min"]
        lines.append(
            f"\u2022 Total: <b>{_fmt_duration(total.value)}</b>   "
            f"{_trend_vs_week(total)}"
        )
        stages = []
        for key, label in (
            ("sleep_deep_min", "Deep"),
            ("sleep_light_min", "Light"),
            ("sleep_rem_min", "REM"),
            ("sleep_awake_min", "Awake"),
        ):
            t = trends.metrics.get(key)
            if t and t.value:
                stages.append(f"{label} {_fmt_duration(t.value)}")
        if stages:
            lines.append("   " + "  \u00b7  ".join(stages))
        score = _line(trends, "sleep_score")
        if score:
            lines.append(score)
    else:
        lines.append("\u2022 <i>No sleep recorded for this day.</i>")
    lines.append("")

    # --- Activity -----------------------------------------------------------
    lines.append("\U0001f463 <b>Activity</b>")
    for entry in (
        _line(trends, "steps"),
        _line(trends, "distance_m", _fmt_km),
        _line(trends, "calories_kcal"),
        _line(trends, "active_minutes", _fmt_duration),
    ):
        if entry:
            lines.append(entry)
    lines.append("")

    # --- Heart & recovery ---------------------------------------------------
    heart_lines = [
        _line(trends, "resting_hr"),
        _line(trends, "avg_hr"),
        _line(trends, "max_hr"),
        _line(trends, "spo2_avg"),
        _line(trends, "stress_avg"),
        _line(trends, "pai"),
    ]
    heart_lines = [h for h in heart_lines if h]
    if heart_lines:
        lines.append("\u2764\ufe0f <b>Heart &amp; Recovery</b>")
        lines.extend(heart_lines)
        lines.append("")

    # --- Coach's notes (LLM analysis) --------------------------------------
    if analysis and analysis.strip():
        lines.append("\U0001f4ac <b>Coach's notes</b>")
        lines.append(html.escape(analysis.strip()))

    return "\n".join(lines).rstrip()


def build_prev_day_report(target: DailyMetrics, analysis: str) -> str:
    pretty_date = target.day.strftime("%A, %d %b %Y")
    lines = [
        f"\U0001f4c5 <b>Previous Day Recap</b>",
        f"{pretty_date}",
        "",
    ]

    values = target.numeric_fields()

    def val(name: str, formatter: Optional[Callable[[float], str]] = None) -> Optional[str]:
        if name not in values:
            return None
        meta = METRIC_LABELS.get(name, {"label": name, "unit": ""})
        rendered = formatter(values[name]) if formatter else _fmt(values[name], meta["unit"])
        return f"\u2022 {meta['label']}: <b>{rendered}</b>"

    lines.append("\U0001f634 <b>Sleep</b>")
    if values.get("sleep_total_min"):
        lines.append(f"\u2022 Total: <b>{_fmt_duration(values['sleep_total_min'])}</b>")
        stages = []
        for key, label in (
            ("sleep_deep_min", "Deep"),
            ("sleep_light_min", "Light"),
            ("sleep_rem_min", "REM"),
            ("sleep_awake_min", "Awake"),
        ):
            if values.get(key):
                stages.append(f"{label} {_fmt_duration(values[key])}")
        if stages:
            lines.append("   " + "  \u00b7  ".join(stages))
    else:
        lines.append("\u2022 <i>No sleep recorded for this day.</i>")
    lines.append("")

    lines.append("\U0001f463 <b>Activity</b>")
    for entry in (
        val("steps"),
        val("distance_m", _fmt_km),
        val("calories_kcal"),
    ):
        if entry:
            lines.append(entry)
    lines.append("")

    heart = [val("resting_hr"), val("avg_hr"), val("spo2_avg"), val("stress_avg")]
    heart = [h for h in heart if h]
    if heart:
        lines.append("\u2764\ufe0f <b>Heart &amp; Recovery</b>")
        lines.extend(heart)
        lines.append("")

    if analysis and analysis.strip():
        lines.append("\U0001f4ac <b>Coach's notes</b>")
        lines.append(html.escape(analysis.strip()))

    return "\n".join(lines).rstrip()
