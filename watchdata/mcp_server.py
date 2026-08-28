"""MCP server exposing the stored health history as tools.

Two audiences are supported by the same server:

1. Rich, typed tools (``get_day``, ``get_days``, ``latest_days``, ``get_trends``,
   ``overview``) for MCP clients such as Cursor or Claude Desktop. These return
   structured JSON.

2. The read-only ``search`` + ``fetch`` pair that ChatGPT (deep research /
   "connectors") requires. ChatGPT only ever calls those two tool names and
   reads ``content[0].text`` as a JSON envelope, so those two return a JSON
   *string* rather than structured content.

Transports
----------
* stdio (default) - for local clients that launch the server as a subprocess:

      python -m watchdata.mcp_server

* streamable-http - a real HTTP server (needed for ChatGPT, which reaches the
  server over HTTPS). Put it behind an HTTPS reverse proxy / tunnel:

      python -m watchdata.mcp_server --http --host 0.0.0.0 --port 8000

  The MCP endpoint is served at ``/mcp``.

The server only needs read access to the SQLite database (``DATABASE_PATH``,
default ``data/health.db``); it does not require the Zepp / OpenAI / Telegram
credentials the report pipeline uses.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .models import MANUAL_METRICS, METRIC_LABELS, DailyMetrics
from .storage import Storage
from .transformation import (
    compute_summary,
    compute_transformation,
    format_summary_text,
    format_transformation_text,
)
from .trends import compute_trends

_DB_PATH = os.environ.get("DATABASE_PATH", "data/health.db").strip() or "data/health.db"

mcp = FastMCP(
    "WatchData Health",
    instructions=(
        "Read-only access to a personal Amazfit/Zepp daily health history "
        "(steps, distance, calories, sleep stages, heart rate, SpO2, stress, "
        "PAI). For a specific day or metric, prefer the typed tools "
        "(get_day, get_days, latest_days, get_trends, overview). The generic "
        "search/fetch pair exists mainly for ChatGPT deep research."
    ),
    # Stateless so each request is self-contained. Essential on hosts that may
    # recycle the instance between calls (e.g. Render free tier), where a
    # stateful session established on one request would not survive to the next
    # and cause intermittent 404s. json_response returns plain JSON (no SSE
    # stream), which is also more robust through proxies. Ignored for stdio.
    stateless_http=True,
    json_response=True,
)


# --------------------------------------------------------------------- helpers
def _fmt_duration(minutes: Optional[float]) -> Optional[str]:
    if minutes is None:
        return None
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _metrics_to_dict(m: DailyMetrics) -> dict[str, Any]:
    """A clean, JSON-serializable view of one day (drops None + raw payload)."""
    out: dict[str, Any] = {"day": m.day.isoformat()}
    fields = m.numeric_fields()
    for name in METRIC_LABELS:
        if name in fields:
            value = fields[name]
            # Present whole numbers as ints for readability.
            out[name] = int(value) if float(value).is_integer() else value
    # Human-friendly sleep duration if present.
    if fields.get("sleep_total_min"):
        out["sleep_total_hm"] = _fmt_duration(fields["sleep_total_min"])
    return out


def _derived(m: DailyMetrics) -> dict[str, Any]:
    """Metrics we can compute consistently from raw + parsed fields.

    These are NOT stored in the DB; they are derived on demand so downstream
    tools (and health-marker catalogs like Anomalie) get a richer picture:
    sleep efficiency, stage percentages, bedtime/wake time, and wake count.
    """
    out: dict[str, Any] = {}
    summary = (m.raw or {}).get("summary") or {}
    slp = summary.get("slp") or {}

    deep = m.sleep_deep_min or 0
    light = m.sleep_light_min or 0
    rem = m.sleep_rem_min or 0
    asleep = deep + light + rem

    st, ed = slp.get("st"), slp.get("ed")
    in_bed: Optional[int] = None
    if st and ed and int(ed) > int(st):
        in_bed = round((int(ed) - int(st)) / 60)
    elif m.sleep_total_min:
        in_bed = m.sleep_total_min

    if asleep and in_bed:
        out["asleep_min"] = asleep
        out["time_in_bed_min"] = in_bed
        out["sleep_efficiency_pct"] = round(asleep / in_bed * 100, 1)
    if asleep:
        out["sleep_stage_pct"] = {
            "deep": round(deep / asleep * 100, 1),
            "light": round(light / asleep * 100, 1),
            "rem": round(rem / asleep * 100, 1),
        }

    if st and ed:
        try:
            tz_raw = summary.get("tz")
            offset = timedelta(seconds=int(tz_raw)) if tz_raw else timedelta(0)
            tzinfo = timezone(offset)
            out["bedtime"] = datetime.fromtimestamp(int(st), tzinfo).strftime(
                "%Y-%m-%d %H:%M"
            )
            out["wake_time"] = datetime.fromtimestamp(int(ed), tzinfo).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (ValueError, OverflowError, OSError):
            pass

    wc = slp.get("wc")
    if wc is not None:
        out["wake_count"] = int(wc)
    return out


def _anomalie_markers(m: DailyMetrics) -> list[dict[str, Any]]:
    """Translate a day into Anomalie-style health markers, ready to log.

    Each entry: {marker, value, unit, source}. ``source`` is "watch" for a
    direct device value or "derived" for one we computed. Markers that need
    data the watch does not provide (e.g. Sleep Consistency, HRV, respiratory
    rate) are intentionally omitted rather than fabricated.
    """
    d = _metrics_to_dict(m)
    der = _derived(m)
    markers: list[dict[str, Any]] = []

    def add(marker: str, value: Any, unit: str = "", source: str = "watch") -> None:
        if value is None:
            return
        markers.append({"marker": marker, "value": value, "unit": unit, "source": source})

    add("Sleep Score", d.get("sleep_score"))
    if der.get("bedtime"):
        add("Bedtime", der["bedtime"].split(" ")[1], source="derived")
    if der.get("wake_time"):
        add("Wake Time", der["wake_time"].split(" ")[1], source="derived")
    if d.get("sleep_total_min") is not None:
        add("Sleep Duration", _fmt_duration(d["sleep_total_min"]))
    if der.get("sleep_efficiency_pct") is not None:
        add("Sleep Efficiency", der["sleep_efficiency_pct"], "%", "derived")
    add("Deep Sleep", d.get("sleep_deep_min"), "min")
    add("Light Sleep", d.get("sleep_light_min"), "min")
    add("REM Sleep", d.get("sleep_rem_min"), "min")
    add("Awake Time", d.get("sleep_awake_min"), "min")
    if der.get("wake_count") is not None:
        add("Sleep Disturbances", der["wake_count"])
    add("Resting Heart Rate", d.get("resting_hr"), "bpm")
    add("Daily Steps", d.get("steps"))
    if d.get("distance_m") is not None:
        add("Distance", round(d["distance_m"] / 1000, 3), "km")
    add("Active Calories", d.get("calories_kcal"), "kcal")
    return markers


def _day_text(m: DailyMetrics) -> str:
    """A short human-readable summary line for a day."""
    d = _metrics_to_dict(m)
    parts: list[str] = []
    if "steps" in d:
        parts.append(f"{d['steps']:,} steps")
    if "distance_m" in d:
        parts.append(f"{d['distance_m'] / 1000:.2f} km")
    if "calories_kcal" in d:
        parts.append(f"{d['calories_kcal']} kcal")
    if d.get("sleep_total_hm"):
        parts.append(f"sleep {d['sleep_total_hm']}")
    elif "sleep_total_min" in d:
        parts.append("no sleep recorded")
    if "resting_hr" in d:
        parts.append(f"resting HR {d['resting_hr']} bpm")
    return f"{m.day.isoformat()}: " + (", ".join(parts) if parts else "no data")


def _result_obj(m: DailyMetrics) -> dict[str, str]:
    return {
        "id": m.day.isoformat(),
        "title": f"Health metrics \u2014 {m.day.strftime('%A, %d %b %Y')}",
        "url": f"https://watchdata.local/day/{m.day.isoformat()}",
    }


def _parse_date(value: str) -> date:
    return date.fromisoformat(value.strip())


# ----------------------------------------------------------------- typed tools
@mcp.tool()
def overview() -> dict[str, Any]:
    """Summarize what history is available (earliest/latest day and count)."""
    with Storage(_DB_PATH) as s:
        latest = s.latest_day()
        count = s.count()
        earliest = None
        if latest is not None:
            rng = s.get_range(latest - timedelta(days=100000), latest)
            earliest = rng[0].day.isoformat() if rng else None
    return {
        "database": _DB_PATH,
        "day_count": count,
        "earliest_day": earliest,
        "latest_day": latest.isoformat() if latest else None,
    }


@mcp.tool()
def get_day(day: str) -> dict[str, Any]:
    """Return all stored metrics for a single day (format: YYYY-MM-DD)."""
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        m = s.get_day(target)
    if m is None:
        return {"day": target.isoformat(), "found": False}
    result = {"found": True, **_metrics_to_dict(m)}
    derived = _derived(m)
    if derived:
        result["derived"] = derived
    return result


@mcp.tool()
def get_day_raw(day: str) -> dict[str, Any]:
    """Return the raw, decoded Zepp summary blob for a day (all watch fields).

    Use this to discover metrics not yet surfaced by the typed tools (e.g. the
    per-minute sleep hypnogram, sleep SpO2, step goal, hourly activity). Only
    days fetched after raw-capture was enabled will have this.
    """
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        m = s.get_day(target)
    if m is None or not m.raw:
        return {"day": target.isoformat(), "found": False, "raw": None}
    return {"found": True, "day": target.isoformat(), "raw": m.raw.get("summary", m.raw)}


@mcp.tool()
def get_days(start: str, end: str) -> dict[str, Any]:
    """Return metrics for an inclusive date range (YYYY-MM-DD), oldest first."""
    a, b = _parse_date(start), _parse_date(end)
    if a > b:
        a, b = b, a
    with Storage(_DB_PATH) as s:
        rows = s.get_range(a, b)
    return {
        "start": a.isoformat(),
        "end": b.isoformat(),
        "count": len(rows),
        "days": [_metrics_to_dict(m) for m in rows],
    }


@mcp.tool()
def latest_days(n: int = 7) -> dict[str, Any]:
    """Return metrics for the most recent ``n`` recorded days (default 7)."""
    n = max(1, min(n, 365))
    with Storage(_DB_PATH) as s:
        latest = s.latest_day()
        if latest is None:
            return {"count": 0, "days": []}
        rows = s.get_range(latest - timedelta(days=n - 1), latest)
    return {"count": len(rows), "days": [_metrics_to_dict(m) for m in rows]}


@mcp.tool()
def anomalie_markers(day: str) -> dict[str, Any]:
    """Return Anomalie-ready health markers for a day (a translation layer).

    Maps raw Amazfit/Zepp fields and derived metrics onto named markers such as
    Sleep Score, Bedtime, Sleep Efficiency, REM Sleep, Sleep Disturbances,
    Resting Heart Rate, Daily Steps, etc. Markers the watch can't support
    (Sleep Consistency, HRV, respiratory rate) are omitted, not invented.
    Includes a ``text`` block that can be pasted directly.
    """
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        m = s.get_day(target)
    if m is None:
        return {"day": target.isoformat(), "found": False, "markers": []}
    markers = _anomalie_markers(m)
    lines = []
    for mk in markers:
        unit = f" {mk['unit']}" if mk["unit"] else ""
        lines.append(f"{mk['marker']}: {mk['value']}{unit}")
    return {
        "found": True,
        "day": target.isoformat(),
        "markers": markers,
        "text": "\n".join(lines),
        "omitted": {
            "Sleep Consistency": "needs multiple nights; not a single-day value",
            "HRV": "not provided by the band_data endpoint",
            "Respiratory Rate": "not provided by the band_data endpoint",
        },
    }


@mcp.tool()
def body_metrics(day: str) -> dict[str, Any]:
    """Return manually-logged body/nutrition metrics for a day.

    These are entered via the Telegram bot (weight, waist, chest, hips, neck,
    body fat, calories, protein) - the watch does not provide them. Returns the
    values recorded on that exact day.
    """
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        entries = s.get_manual(target)
    out: dict[str, Any] = {"day": target.isoformat(), "found": bool(entries)}
    for key, meta in MANUAL_METRICS.items():
        if key in entries:
            out[key] = {"value": entries[key]["value"], "unit": meta["unit"]}
    return out


@mcp.tool()
def transformation_summary(day: str) -> dict[str, Any]:
    """High-level "am I progressing?" summary combining watch + manual data.

    Weight (baseline/current/7d avg/weekly trend/total change), latest body
    measurements, activity (avg steps, week-over-week change, days over step
    thresholds), sleep (7d avg, % nights 7-9h, bedtime variability), recovery
    (resting HR + change), and nutrition (avg calories/protein). Returns a
    ``text`` block plus structured ``summary`` JSON. No values are fabricated.
    """
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        data = compute_summary(s, target)
    return {
        "found": True,
        "day": target.isoformat(),
        "text": format_summary_text(data),
        "summary": data,
    }


@mcp.tool()
def transformation_metrics(day: str) -> dict[str, Any]:
    """Trailing 7/30-day transformation metrics for longitudinal tracking.

    Returns averages for sleep (duration, deep, REM, efficiency) and activity
    (steps, distance, active calories, resting HR), plus schedule variability
    (bedtime / wake / duration standard deviations in minutes) and a resting-HR
    trend vs the previous 30-day period. Data quality is explicit: 0-sleep days
    are excluded and valid-night counts are reported. No values are fabricated.

    Sleep Consistency is deliberately reported as raw SD, not a 0-100 score.
    Includes a ``text`` block for direct reading and structured ``metrics`` JSON.
    """
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        if s.get_day(target) is None and s.latest_day() is None:
            return {"day": target.isoformat(), "found": False}
        data = compute_transformation(s, target)
    return {
        "found": True,
        "day": target.isoformat(),
        "text": format_transformation_text(data),
        "metrics": data,
    }


@mcp.tool()
def get_trends(day: str) -> dict[str, Any]:
    """Compare a day's metrics against trailing 7/30/90-day averages."""
    target = _parse_date(day)
    with Storage(_DB_PATH) as s:
        m = s.get_day(target)
        if m is None:
            return {"day": target.isoformat(), "found": False}
        report = compute_trends(s, m)
    return {"found": True, **report.as_dict()}


# ---------------------------------------------- ChatGPT-compatible search/fetch
# NOTE: ChatGPT reads content[0].text as JSON for these two, so they return a
# JSON *string* (not structured content). Keep both read-only.
@mcp.tool()
def search(query: str) -> str:
    """Search the health history and return matching day documents.

    Understands explicit dates (YYYY-MM-DD) or months (YYYY-MM) in the query;
    otherwise returns the most recent 30 recorded days. Each result carries an
    id (the ISO date), a title, and a url. Use ``fetch`` with an id for details.
    """
    results: list[dict[str, str]] = []
    with Storage(_DB_PATH) as s:
        exact = re.findall(r"\d{4}-\d{2}-\d{2}", query)
        months = [m for m in re.findall(r"\d{4}-\d{2}(?!-)", query)]
        seen: set[str] = set()

        for ds in exact:
            try:
                m = s.get_day(_parse_date(ds))
            except ValueError:
                continue
            if m and m.day.isoformat() not in seen:
                results.append(_result_obj(m))
                seen.add(m.day.isoformat())

        for ym in months:
            try:
                year, month = (int(x) for x in ym.split("-"))
                first = date(year, month, 1)
                nxt = date(year + (month == 12), (month % 12) + 1, 1)
                for m in s.get_range(first, nxt - timedelta(days=1)):
                    if m.day.isoformat() not in seen:
                        results.append(_result_obj(m))
                        seen.add(m.day.isoformat())
            except ValueError:
                continue

        if not results:
            latest = s.latest_day()
            if latest is not None:
                rows = s.get_range(latest - timedelta(days=29), latest)
                results = [_result_obj(m) for m in reversed(rows)]

    return json.dumps({"results": results})


@mcp.tool()
def fetch(id: str) -> str:
    """Fetch the full health document for a day id (ISO date) as JSON text."""
    try:
        target = _parse_date(id)
    except ValueError:
        return json.dumps(
            {"id": id, "title": "Invalid id", "text": "Expected an ISO date (YYYY-MM-DD).", "url": "", "metadata": {}}
        )

    with Storage(_DB_PATH) as s:
        m = s.get_day(target)
        if m is None:
            return json.dumps(
                {
                    "id": id,
                    "title": f"No data for {id}",
                    "text": f"No health metrics stored for {id}.",
                    "url": _result_obj(DailyMetrics(day=target))["url"],
                    "metadata": {},
                }
            )
        metrics = _metrics_to_dict(m)
        derived = _derived(m)
        report = compute_trends(s, m)

    text_lines = [_day_text(m), "", "Metrics: " + json.dumps(metrics)]
    if derived:
        text_lines.append("Derived: " + json.dumps(derived))
    document = {
        "id": id,
        "title": f"Health metrics \u2014 {target.strftime('%A, %d %b %Y')}",
        "text": "\n".join(text_lines),
        "url": _result_obj(m)["url"],
        "metadata": {
            "metrics": metrics,
            "derived": derived,
            "trends": report.as_dict(),
        },
    }
    return json.dumps(document)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WatchData MCP server")
    p.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable-http instead of stdio (needed for ChatGPT).",
    )
    # Hosting platforms (Render/Fly/Cloud Run) inject the bind address via env.
    p.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="HTTP bind host (env HOST; use 0.0.0.0 in containers).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="HTTP bind port (env PORT).",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "Public hostname to trust in the Host header (e.g. your ngrok/HTTPS "
            "domain). Repeatable. When set, DNS-rebinding protection stays ON "
            "and only these hosts are accepted. When omitted, protection is "
            "DISABLED so any proxy host works (fine behind a trusted tunnel; "
            "combine with auth before exposing publicly)."
        ),
    )
    return p.parse_args(argv)


def serve_http(host: str, port: int, allowed_hosts: Optional[list[str]] = None) -> None:
    """Configure and run the streamable-http transport.

    Shared by the standalone CLI and the combined host runner (watchdata.serve).
    """
    mcp.settings.host = host
    mcp.settings.port = port
    # Allowed hosts may also come from the env (comma-separated), handy on
    # hosting platforms where the public domain is known at deploy time.
    hosts_in = list(allowed_hosts or [])
    env_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "")
    if env_hosts:
        hosts_in += [h.strip() for h in env_hosts.split(",") if h.strip()]
    # The SDK auto-enables DNS-rebinding protection for localhost binds, which
    # rejects requests forwarded by a proxy (ngrok / Fly / Render) with
    # "421 Invalid Host header". Behind an HTTPS proxy we must relax it.
    if hosts_in:
        hosts: list[str] = []
        origins: list[str] = []
        for h in hosts_in:
            hosts.extend([h, f"{h}:*"])
            origins.extend([f"https://{h}", f"https://{h}:*"])
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    mcp.run(transport="streamable-http")


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    if args.http:
        serve_http(args.host, args.port, args.allowed_host)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
