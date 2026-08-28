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

from .models import MANUAL_METRICS, DailyMetrics
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


# =============================================================== SUMMARY ===
# A single "am I progressing?" view combining watch data + manually-logged
# weight / measurements / nutrition.


def _pct_change(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def _weight_block(storage: Storage, target: date) -> dict[str, Any]:
    baseline = storage.earliest_manual("weight")
    current = storage.latest_manual("weight", on_or_before=target)
    last7 = [v for _, v in storage.get_manual_range("weight", target - timedelta(days=6), target)]
    prior7 = [
        v
        for _, v in storage.get_manual_range(
            "weight", target - timedelta(days=13), target - timedelta(days=7)
        )
    ]
    avg7 = _mean(last7)
    trend = None
    if last7 and prior7:
        trend = round(statistics.fmean(last7) - statistics.fmean(prior7), 2)
    total_change = None
    if baseline and current:
        total_change = round(current[1] - baseline[1], 2)
    return {
        "baseline_kg": baseline[1] if baseline else None,
        "baseline_day": baseline[0].isoformat() if baseline else None,
        "current_kg": current[1] if current else None,
        "current_day": current[0].isoformat() if current else None,
        "avg_7d_kg": avg7,
        "trend_kg_per_week": trend,
        "total_change_kg": total_change,
    }


def _activity_block(storage: Storage, target: date) -> dict[str, Any]:
    last30 = storage.get_range(target - timedelta(days=29), target)
    last7 = storage.get_range(target - timedelta(days=6), target)
    prior_last7 = storage.get_range(target - timedelta(days=13), target - timedelta(days=7))

    def steps(days: list[DailyMetrics]) -> list[int]:
        return [m.steps for m in days if m.steps]

    avg7 = _mean(steps(last7))
    prev7 = _mean(steps(prior_last7))
    s30 = steps(last30)
    return {
        "avg_steps_7d": avg7,
        "steps_change_vs_prev_week_pct": _pct_change(avg7, prev7),
        "days_ge_5k_30d": sum(1 for v in s30 if v >= 5000),
        "days_ge_7_5k_30d": sum(1 for v in s30 if v >= 7500),
        "days_ge_10k_30d": sum(1 for v in s30 if v >= 10000),
        "active_days_30d": len(s30),
    }


def _sleep_block(storage: Storage, target: date) -> dict[str, Any]:
    last7 = storage.get_range(target - timedelta(days=6), target)
    last30 = storage.get_range(target - timedelta(days=29), target)
    nights7 = [m for m in last7 if _is_valid_night(m)]
    nights30 = [m for m in last30 if _is_valid_night(m)]
    agg7 = _sleep_agg(nights7)
    in_target = [m for m in nights30 if m.sleep_total_min and 420 <= m.sleep_total_min <= 540]
    return {
        "avg_duration_7d_min": agg7["avg_sleep_duration_min"],
        "pct_nights_7_9h_30d": (
            round(len(in_target) / len(nights30) * 100, 1) if nights30 else None
        ),
        "bedtime_sd_30d_min": _sleep_agg(nights30)["bedtime_sd_min"],
        "valid_nights_30d": len(nights30),
    }


def _nutrition_block(storage: Storage, target: date) -> dict[str, Any]:
    cals = [v for _, v in storage.get_manual_range("calories", target - timedelta(days=6), target)]
    prot = [v for _, v in storage.get_manual_range("protein", target - timedelta(days=6), target)]
    return {
        "avg_calories_7d": _mean(cals),
        "avg_protein_7d": _mean(prot),
        "days_logged_7d": len(cals),
    }


def compute_summary(storage: Storage, target: date) -> dict[str, Any]:
    """A high-level transformation summary combining watch + manual data."""
    weight = _weight_block(storage, target)
    activity = _activity_block(storage, target)
    sleep = _sleep_block(storage, target)
    nutrition = _nutrition_block(storage, target)

    # Recovery: resting HR now (7d avg) + change vs previous 30 days.
    last7 = storage.get_range(target - timedelta(days=6), target)
    cur30 = storage.get_range(target - timedelta(days=29), target)
    prev30 = storage.get_range(target - timedelta(days=59), target - timedelta(days=30))
    rhr7 = _mean([m.resting_hr for m in last7 if m.resting_hr])
    cur_rhr = [m.resting_hr for m in cur30 if m.resting_hr]
    prev_rhr = [m.resting_hr for m in prev30 if m.resting_hr]
    rhr_change = (
        round(statistics.fmean(cur_rhr) - statistics.fmean(prev_rhr), 1)
        if cur_rhr and prev_rhr
        else None
    )

    week = None
    if weight["baseline_day"]:
        baseline_day = date.fromisoformat(weight["baseline_day"])
        week = (target - baseline_day).days // 7 + 1

    # Latest body measurements (most recent on/before target).
    measurements: dict[str, Any] = {}
    for key in ("waist", "chest", "hips", "neck", "bodyfat"):
        latest = storage.latest_manual(key, on_or_before=target)
        if latest:
            measurements[key] = {"value": latest[1], "day": latest[0].isoformat()}

    return {
        "day": target.isoformat(),
        "week": week,
        "weight": weight,
        "measurements": measurements,
        "activity": activity,
        "sleep": sleep,
        "recovery": {"resting_hr_7d": rhr7, "rhr_change_vs_prev30_bpm": rhr_change},
        "nutrition": nutrition,
        "notes": {
            "deficit": (
                "energy deficit not computed: needs a maintenance/TDEE estimate "
                "the watch does not provide"
            ),
        },
    }


def format_summary_text(data: dict[str, Any]) -> str:
    w = data["weight"]
    a = data["activity"]
    s = data["sleep"]
    r = data["recovery"]
    n = data["nutrition"]
    header = f"TRANSFORMATION SUMMARY {_DASH} {data['day']}"
    if data.get("week"):
        header += f" (week {data['week']})"
    lines = [header, ""]

    lines.append("Weight")
    if w["current_kg"] is not None:
        lines.append(f"  Baseline: {w['baseline_kg']} kg" if w["baseline_kg"] is not None else "  Baseline: \u2014")
        lines.append(f"  Current: {w['current_kg']} kg")
        lines.append(
            f"  Change: {w['total_change_kg']:+.2f} kg" if w["total_change_kg"] is not None else "  Change: \u2014"
        )
        lines.append(f"  7d average: {w['avg_7d_kg']} kg" if w["avg_7d_kg"] is not None else "  7d average: \u2014")
        lines.append(
            f"  Trend: {w['trend_kg_per_week']:+.2f} kg/week"
            if w["trend_kg_per_week"] is not None
            else "  Trend: \u2014 (need 2 weeks of entries)"
        )
    else:
        lines.append("  No weight logged yet (use /weight 116.8)")
    lines.append("")

    if data["measurements"]:
        lines.append("Measurements")
        for key, meta in MANUAL_METRICS.items():
            if key in data["measurements"]:
                mv = data["measurements"][key]
                lines.append(f"  {meta['label']}: {mv['value']} {meta['unit']}".rstrip())
        lines.append("")

    lines.append("Activity")
    lines.append(
        f"  Avg steps (7d): {int(a['avg_steps_7d']):,}" if a["avg_steps_7d"] is not None else "  Avg steps (7d): \u2014"
    )
    if a["steps_change_vs_prev_week_pct"] is not None:
        lines.append(f"  Change vs previous week: {a['steps_change_vs_prev_week_pct']:+.1f}%")
    lines.append(
        f"  Days >=5k/7.5k/10k (30d): {a['days_ge_5k_30d']}/{a['days_ge_7_5k_30d']}/{a['days_ge_10k_30d']}"
    )
    lines.append("")

    lines.append("Sleep")
    lines.append(f"  Avg (7d): {_hm(s['avg_duration_7d_min']) or _DASH}")
    if s["pct_nights_7_9h_30d"] is not None:
        lines.append(f"  Nights 7-9h (30d): {s['pct_nights_7_9h_30d']}%")
    bsd = s["bedtime_sd_30d_min"]
    lines.append(f"  Bedtime variability (30d): {bsd if bsd is not None else _DASH} min")
    lines.append("")

    lines.append("Recovery")
    lines.append(f"  Resting HR (7d): {r['resting_hr_7d'] or _DASH} bpm")
    if r["rhr_change_vs_prev30_bpm"] is not None:
        lines.append(f"  Change vs prev 30d: {r['rhr_change_vs_prev30_bpm']:+.1f} bpm")
    lines.append("")

    lines.append("Nutrition")
    if n["avg_calories_7d"] is not None:
        lines.append(f"  Avg calories (7d): {int(n['avg_calories_7d']):,} kcal")
        if n["avg_protein_7d"] is not None:
            lines.append(f"  Avg protein (7d): {int(n['avg_protein_7d'])} g")
        lines.append(f"  ({n['days_logged_7d']} day(s) logged)")
    else:
        lines.append("  No nutrition logged (use /log calories=2450 protein=185)")
    return "\n".join(lines)
