"""SQLite persistence for historical daily metrics.

The database is intended to be committed back to the repository by the GitHub
Actions workflow so history accumulates across runs.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, timedelta
from typing import Optional

from .models import DailyMetrics

logger = logging.getLogger("watchdata.storage")

# Ordered list of numeric columns (also drives table creation & upserts).
_METRIC_COLUMNS = [
    "steps",
    "distance_m",
    "calories_kcal",
    "active_minutes",
    "sleep_total_min",
    "sleep_deep_min",
    "sleep_light_min",
    "sleep_rem_min",
    "sleep_awake_min",
    "sleep_score",
    "resting_hr",
    "avg_hr",
    "max_hr",
    "min_hr",
    "spo2_avg",
    "stress_avg",
    "pai",
]


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # WAL + a busy timeout let the MCP server (reads) and the bot / fetch
        # (writes) share one file concurrently without "database is locked".
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=10000")
        except sqlite3.DatabaseError:
            logger.warning("Could not set WAL/busy_timeout pragmas", exc_info=True)
        self._init_schema()

    def _init_schema(self) -> None:
        columns_sql = ",\n".join(f"    {col} REAL" for col in _METRIC_COLUMNS)
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS daily_metrics (
                day TEXT PRIMARY KEY,
{columns_sql},
                raw TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        # Manually-logged metrics (weight, waist, calories, ...) keyed by
        # (day, metric) so each can be updated independently.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_log (
                day TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL,
                unit TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (day, metric)
            )
            """
        )
        self.conn.commit()

    # --------------------------------------------------------- manual metrics
    def set_manual(
        self, day: date, metric: str, value: float, unit: Optional[str] = None
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO manual_log (day, metric, value, unit, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(day, metric) DO UPDATE SET
                value=excluded.value, unit=excluded.unit,
                updated_at=datetime('now')
            """,
            (day.isoformat(), metric, float(value), unit),
        )
        self.conn.commit()

    def delete_manual(self, day: date, metric: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM manual_log WHERE day = ? AND metric = ?",
            (day.isoformat(), metric),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_manual(self, day: date) -> dict[str, dict[str, object]]:
        cur = self.conn.execute(
            "SELECT metric, value, unit FROM manual_log WHERE day = ?",
            (day.isoformat(),),
        )
        return {
            r["metric"]: {"value": r["value"], "unit": r["unit"]}
            for r in cur.fetchall()
        }

    def get_manual_range(
        self, metric: str, start: date, end: date
    ) -> list[tuple[date, float]]:
        """(day, value) pairs for a metric in an inclusive range, oldest first."""
        cur = self.conn.execute(
            """
            SELECT day, value FROM manual_log
            WHERE metric = ? AND day BETWEEN ? AND ? AND value IS NOT NULL
            ORDER BY day
            """,
            (metric, start.isoformat(), end.isoformat()),
        )
        return [(date.fromisoformat(r["day"]), r["value"]) for r in cur.fetchall()]

    def latest_manual(
        self, metric: str, on_or_before: Optional[date] = None
    ) -> Optional[tuple[date, float]]:
        if on_or_before is not None:
            cur = self.conn.execute(
                """
                SELECT day, value FROM manual_log
                WHERE metric = ? AND value IS NOT NULL AND day <= ?
                ORDER BY day DESC LIMIT 1
                """,
                (metric, on_or_before.isoformat()),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT day, value FROM manual_log
                WHERE metric = ? AND value IS NOT NULL
                ORDER BY day DESC LIMIT 1
                """,
                (metric,),
            )
        row = cur.fetchone()
        return (date.fromisoformat(row["day"]), row["value"]) if row else None

    def earliest_manual(self, metric: str) -> Optional[tuple[date, float]]:
        cur = self.conn.execute(
            """
            SELECT day, value FROM manual_log
            WHERE metric = ? AND value IS NOT NULL
            ORDER BY day ASC LIMIT 1
            """,
            (metric,),
        )
        row = cur.fetchone()
        return (date.fromisoformat(row["day"]), row["value"]) if row else None

    # ------------------------------------------------------------------ write
    def upsert(self, metrics: DailyMetrics) -> None:
        row = metrics.to_row()
        cols = ["day", *_METRIC_COLUMNS, "raw"]
        values = [row["day"]]
        values += [row.get(col) for col in _METRIC_COLUMNS]
        values.append(json.dumps(metrics.raw, ensure_ascii=False))

        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{col}=excluded.{col}" for col in cols if col != "day"
        )
        self.conn.execute(
            f"""
            INSERT INTO daily_metrics ({", ".join(cols)}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(day) DO UPDATE SET {updates},
                updated_at=datetime('now')
            """,
            values,
        )
        self.conn.commit()

    def upsert_many(self, metrics_by_day: dict[date, DailyMetrics]) -> int:
        for metrics in metrics_by_day.values():
            self.upsert(metrics)
        return len(metrics_by_day)

    # ------------------------------------------------------------------- read
    def get_day(self, day: date) -> Optional[DailyMetrics]:
        cur = self.conn.execute(
            "SELECT * FROM daily_metrics WHERE day = ?", (day.isoformat(),)
        )
        row = cur.fetchone()
        return _row_to_metrics(row) if row else None

    def get_range(self, start: date, end: date) -> list[DailyMetrics]:
        """Inclusive range, ordered oldest -> newest."""
        cur = self.conn.execute(
            "SELECT * FROM daily_metrics WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        )
        return [_row_to_metrics(r) for r in cur.fetchall()]

    def get_previous_n_days(
        self, reference: date, n: int, inclusive: bool = False
    ) -> list[DailyMetrics]:
        """Return metrics for the ``n`` days before ``reference``.

        If ``inclusive`` is False (default), ``reference`` itself is excluded,
        i.e. the comparison window is [reference-n, reference-1].
        """
        end = reference if inclusive else reference - timedelta(days=1)
        start = end - timedelta(days=n - 1)
        return self.get_range(start, end)

    def latest_day(self) -> Optional[date]:
        cur = self.conn.execute("SELECT MAX(day) AS d FROM daily_metrics")
        row = cur.fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS c FROM daily_metrics")
        return int(cur.fetchone()["c"])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _row_to_metrics(row: sqlite3.Row) -> DailyMetrics:
    raw = {}
    if row["raw"]:
        try:
            raw = json.loads(row["raw"])
        except json.JSONDecodeError:
            raw = {}

    def _int(col: str) -> Optional[int]:
        value = row[col]
        return int(value) if value is not None else None

    return DailyMetrics(
        day=date.fromisoformat(row["day"]),
        steps=_int("steps"),
        distance_m=_int("distance_m"),
        calories_kcal=_int("calories_kcal"),
        active_minutes=_int("active_minutes"),
        sleep_total_min=_int("sleep_total_min"),
        sleep_deep_min=_int("sleep_deep_min"),
        sleep_light_min=_int("sleep_light_min"),
        sleep_rem_min=_int("sleep_rem_min"),
        sleep_awake_min=_int("sleep_awake_min"),
        sleep_score=_int("sleep_score"),
        resting_hr=_int("resting_hr"),
        avg_hr=_int("avg_hr"),
        max_hr=_int("max_hr"),
        min_hr=_int("min_hr"),
        spo2_avg=_int("spo2_avg"),
        stress_avg=_int("stress_avg"),
        pai=float(row["pai"]) if row["pai"] is not None else None,
        raw=raw,
    )
