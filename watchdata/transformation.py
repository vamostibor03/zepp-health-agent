"""Longitudinal "transformation" metrics: trailing 7/30-day aggregates.

Built for body-transformation tracking rather than generic statistics:

* Data quality is explicit - a 0-sleep day is NOT counted as a valid night, and
  every aggregate reports how many valid nights/days actually fed it.
* Schedule regularity is reported as raw *variability* (bedtime / wake / duration
  standard deviations, in minutes) instead of an invented 0-100 "consistency
  score". A consistency score can be layered on later once a formula is agreed.
* Missing values are never fabricated; they come back as null.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .models import DailyMetrics
from .storage import Storage


# --------------------------------------------------------------------- helpers
def _asleep_min(m: DailyMetrics) -> int:
    return (m.sleep_deep_min or 0) + (m.sleep_light_min or 0) + (m.sleep_rem_min or 0)


def _is_valid_night(m: DailyMetrics) -> bool:
    """A night with any scored sleep (deep+light+rem > 0)."""
    return _asleep_min(m) > 0


def _sleep_bounds(m: DailyMetrics) -> tuple[Optional[datetime], Optional[datetime]]:
    """(bedtime, wake) datetimes from the raw st/ed unix timestamps, if present."""
    summary = (m.raw or {}).get("summary") or {}
    slp = summary.get("slp") or {}
    st, ed = slp.get("st"), slp.get("ed")
    if not (st and ed):
        return None, None
    try:
        tz_raw = summary.get("tz")
        offset = timedelta(seconds=int(tz_raw)) if tz_raw else timedelta(0)
        tzinfo = timezone(offset)
        return (
            datetime.fromtimestamp(int(st), tzinfo),
            datetime.fromtimestamp(int(ed), tzinfo),
        )
    except (ValueError, OverflowError, OSError):
        return None, None


def _bedtime_minute(dt: datetime) -> int:
    """Continuous minute scale so late-evening and after-midnight bedtimes are
    comparable (e.g. 22:00 -> 1320, 00:30 -> 1470)."""
    minute = dt.hour * 60 + dt.minute
    if dt.hour < 12:
        minute += 1440
    return minute


def _wake_minute(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _mean(xs: list[float]) -> Optional[float]:
    return round(statistics.fmean(xs), 1) if xs else None


def _sd_min(xs: list[float]) -> Optional[int]:
    return round(statistics.stdev(xs)) if len(xs) >= 2 else None


def _minute_to_hhmm(minute: float) -> str:
    m = int(round(minute)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _hm(minutes: Optional[float]) -> Optional[str]:
    if minutes is None:
        return None
    total = int(round(minutes))
    h, mm = divmod(total, 60)
    if h and mm:
        return f"{h}h {mm}m"
    return f"{h}h" if h else f"{mm}m"


# --------------------------------------------------------------- aggregations
def _sleep_agg(nights: list[DailyMetrics]) -> dict[str, Any]:
    durations = [m.sleep_total_min for m in nights if m.sleep_total_min]
    deep = [m.sleep_deep_min for m in nights if m.sleep_deep_min is not None]
    light = [m.sleep_light_min for m in nights if m.sleep_light_min is not None]
    rem = [m.sleep_rem_min for m in nights if m.sleep_rem_min is not None]

    efficiency: list[float] = []
    bedtimes: list[int] = []
    waketimes: list[int] = []
    for m in nights:
        b, w = _sleep_bounds(m)
        asleep = _asleep_min(m)
        in_bed: Optional[int] = None
        if b and w and w > b:
            in_bed = round((w - b).total_seconds() / 60)
        elif m.sleep_total_min:
            in_bed = m.sleep_total_min
        if asleep and in_bed:
            efficiency.append(asleep / in_bed * 100)
        if b:
            bedtimes.append(_bedtime_minute(b))
        if w:
            waketimes.append(_wake_minute(w))

    return {
        "avg_sleep_duration_min": _mean(durations),
        "avg_deep_min": _mean(deep),
        "avg_light_min": _mean(light),
        "avg_rem_min": _mean(rem),
        "avg_efficiency_pct": _mean(efficiency),
        "bedtime_mean": _minute_to_hhmm(statistics.fmean(bedtimes)) if bedtimes else None,
        "bedtime_sd_min": _sd_min(bedtimes),
        "waketime_mean": _minute_to_hhmm(statistics.fmean(waketimes)) if waketimes else None,
        "waketime_sd_min": _sd_min(waketimes),
        "sleep_duration_sd_min": _sd_min(durations),
        "nights_with_bedtime": len(bedtimes),
    }


def _activity_agg(days: list[DailyMetrics]) -> dict[str, Any]:
    def present(attr: str) -> list[float]:
        # Treat 0 as "not worn / no reading" for these metrics.
        return [getattr(m, attr) for m in days if getattr(m, attr)]

    steps = present("steps")
    dist = present("distance_m")
    cal = present("calories_kcal")
    rhr = present("resting_hr")
    return {
        "avg_steps": _mean(steps),
        "steps_days": len(steps),
        "avg_distance_m": _mean(dist),
        "avg_active_calories": _mean(cal),
        "avg_resting_hr": _mean(rhr),
        "resting_hr_days": len(rhr),
    }


def compute_transformation(storage: Storage, target: date) -> dict[str, Any]:
    """Trailing 7/30-day transformation metrics ending on ``target`` (inclusive)."""

    def window(size: int) -> list[DailyMetrics]:
        return storage.get_range(target - timedelta(days=size - 1), target)

    result: dict[str, Any] = {"day": target.isoformat(), "windows": {}}
    for label, size in (("7d", 7), ("30d", 30)):
        rows = window(size)
        nights = [m for m in rows if _is_valid_night(m)]
        result["windows"][label] = {
            "window_days": size,
            "days_with_any_data": len(rows),
            "valid_nights": len(nights),
            "missing_nights": size - len(nights),
            "sleep": _sleep_agg(nights),
            "activity": _activity_agg(rows),
        }

    # Resting-HR trend: current 30 days vs the 30 days before that.
    cur = window(30)
    prev = storage.get_range(target - timedelta(days=59), target - timedelta(days=30))
    cur_rhr = [m.resting_hr for m in cur if m.resting_hr]
    prev_rhr = [m.resting_hr for m in prev if m.resting_hr]
    change = None
    if cur_rhr and prev_rhr:
        change = round(statistics.fmean(cur_rhr) - statistics.fmean(prev_rhr), 1)
    result["resting_hr_change_30d_vs_prev30d_bpm"] = change
    result["resting_hr_prev30d_days"] = len(prev_rhr)

    result["notes"] = {
        "valid_night": "a night with >0 minutes of scored sleep (deep+light+rem)",
        "sleep_consistency": (
            "intentionally NOT scored 0-100; use bedtime/wake/duration SD "
            "(minutes) as the raw regularity signal"
        ),
        "variability_units": "standard deviations are in minutes",
        "weight": "not provided by the watch; log manually if you want it here",
    }
    return result


# ------------------------------------------------------------------ rendering
_DASH = "\u2014"


def format_transformation_text(data: dict[str, Any]) -> str:
    lines = [f"TRANSFORMATION METRICS {_DASH} {data['day']}", ""]

    for label in ("7d", "30d"):
        w = data["windows"][label]
        s = w["sleep"]
        a = w["activity"]
        vn = w["valid_nights"]
        lines.append(f"Sleep {_DASH} {label} ({vn}/{w['window_days']} valid nights)")
        lines.append(f"  Avg duration: {_hm(s['avg_sleep_duration_min']) or _DASH}")
        lines.append(f"  Avg deep: {_hm(s['avg_deep_min']) or _DASH}")
        lines.append(f"  Avg REM: {_hm(s['avg_rem_min']) or _DASH}")
        eff = s["avg_efficiency_pct"]
        lines.append(f"  Avg efficiency: {f'{eff}%' if eff is not None else _DASH}")
        bt = s["bedtime_mean"] or _DASH
        bsd = s["bedtime_sd_min"] if s["bedtime_sd_min"] is not None else _DASH
        lines.append(f"  Bedtime: {bt} (SD {bsd} min)")
        wt = s["waketime_mean"] or _DASH
        wsd = s["waketime_sd_min"] if s["waketime_sd_min"] is not None else _DASH
        lines.append(f"  Wake: {wt} (SD {wsd} min)")
        dsd = s["sleep_duration_sd_min"] if s["sleep_duration_sd_min"] is not None else _DASH
        lines.append(f"  Duration SD: {dsd} min")
        lines.append("")
        lines.append(f"Activity {_DASH} {label}")
        st = a["avg_steps"]
        lines.append(f"  Avg steps: {int(st):,} ({a['steps_days']} days)" if st is not None else f"  Avg steps: {_DASH}")
        lines.append(
            f"  Avg distance: {a['avg_distance_m'] / 1000:.2f} km"
            if a["avg_distance_m"] is not None
            else f"  Avg distance: {_DASH}"
        )
        lines.append(
            f"  Avg active calories: {int(a['avg_active_calories'])} kcal"
            if a["avg_active_calories"] is not None
            else f"  Avg active calories: {_DASH}"
        )
        rhr = a["avg_resting_hr"]
        lines.append(
            f"  Avg resting HR: {rhr} bpm ({a['resting_hr_days']} days)"
            if rhr is not None
            else f"  Avg resting HR: {_DASH}"
        )
        lines.append("")

    change = data.get("resting_hr_change_30d_vs_prev30d_bpm")
    if change is not None:
        arrow = "down" if change < 0 else "up"
        lines.append(
            f"Resting HR trend: {change:+.1f} bpm ({arrow}) vs previous 30 days"
        )
    lines.append("")
    q7 = data["windows"]["7d"]
    q30 = data["windows"]["30d"]
    lines.append("Data quality")
    lines.append(f"  Valid sleep nights (7d): {q7['valid_nights']}/7")
    lines.append(f"  Valid sleep nights (30d): {q30['valid_nights']}/30")
    lines.append("  Weight: not tracked by watch (log manually)")
    return "\n".join(lines)
