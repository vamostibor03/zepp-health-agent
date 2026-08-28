"""Entry point / orchestration for the WatchData pipeline.

Modes
-----
daily     Fetch recent data, store it, compute 7/30/90-day trends for the
          target day (default: yesterday), get an LLM analysis, and send the
          full report to Telegram. This is what the scheduled job runs.

prev-day  Standalone recap of a single day (default: yesterday) with no trend
          comparison. Useful for a quick "how was yesterday" message or to
          backfill without touching the trend logic.

Common flags
------------
--date YYYY-MM-DD   Override the target day (defaults to "yesterday" in the
                    configured timezone).
--backfill N        Fetch/store the last N days of history (great for the very
                    first run so trends have data to work with).
--no-telegram       Print the report instead of sending it (dry run).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from .config import Config, configure_logging
from .llm import LLMAnalyzer
from .models import DailyMetrics
from .report import build_daily_report, build_prev_day_report
from .storage import Storage
from .telegram_notifier import TelegramNotifier
from .trends import compute_trends
from .zepp_client import ZeppClient

logger = logging.getLogger("watchdata.main")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Amazfit/Zepp daily health report")
    parser.add_argument(
        "--mode",
        choices=["daily", "prev-day"],
        default="daily",
        help="Report mode (default: daily).",
    )
    parser.add_argument(
        "--date",
        help="Target day YYYY-MM-DD (default: yesterday in configured tz).",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="Also fetch/store the last N days of history before reporting.",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Print the report instead of sending it to Telegram.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser.parse_args(argv)


def _resolve_target(cfg: Config, arg_date: str | None) -> date:
    if arg_date:
        return date.fromisoformat(arg_date)
    return cfg.yesterday()


def _sync_data(
    cfg: Config, storage: Storage, target: date, backfill: int
) -> dict[date, DailyMetrics]:
    """Fetch a window that covers the target + enough history for trends."""
    client = ZeppClient(
        email=cfg.zepp_email,
        password=cfg.zepp_password,
        region=cfg.zepp_region,
        user_id=cfg.zepp_user_id,
        app_token=cfg.zepp_app_token,
    )
    client.login()

    # Always fetch enough to (a) cover the requested backfill and (b) refresh
    # the 90-day trend window ending at the target day.
    history_span = max(backfill, 92)
    from_date = target - timedelta(days=history_span)
    fetched = client.fetch_band_data(from_date=from_date, to_date=target)
    stored = storage.upsert_many(fetched)
    logger.info("Stored/updated %d day(s); DB now holds %d day(s)", stored, storage.count())
    return fetched


def _get_target_metrics(
    storage: Storage, fetched: dict[date, DailyMetrics], target: date
) -> DailyMetrics:
    if target in fetched:
        return fetched[target]
    stored = storage.get_day(target)
    if stored is not None:
        return stored
    logger.warning("No data available for %s; emitting empty metrics", target)
    return DailyMetrics(day=target)


def generate_daily_report(
    cfg: Config, target: date, backfill: int = 0
) -> str:
    """Build (but do NOT deliver) the full daily report for ``target``.

    Shared by the CLI and the on-demand Telegram command bot.
    """
    logger.info("Building DAILY report for %s", target)
    with Storage(cfg.database_path) as storage:
        fetched = _sync_data(cfg, storage, target, backfill)
        metrics = _get_target_metrics(storage, fetched, target)
        trends = compute_trends(storage, metrics)

        analyzer = LLMAnalyzer(
            api_key=cfg.openai_api_key,
            model=cfg.llm_model,
            base_url=cfg.openai_base_url,
        )
        analysis = analyzer.analyze_daily(metrics, trends)
        return build_daily_report(metrics, trends, analysis)


def generate_prev_day_report(
    cfg: Config, target: date, backfill: int = 0
) -> str:
    """Build (but do NOT deliver) the previous-day recap for ``target``."""
    logger.info("Building PREV-DAY recap for %s", target)
    with Storage(cfg.database_path) as storage:
        fetched = _sync_data(cfg, storage, target, backfill)
        metrics = _get_target_metrics(storage, fetched, target)

        analyzer = LLMAnalyzer(
            api_key=cfg.openai_api_key,
            model=cfg.llm_model,
            base_url=cfg.openai_base_url,
        )
        analysis = analyzer.analyze_prev_day(metrics)
        return build_prev_day_report(metrics, analysis)


def run_daily(cfg: Config, args: argparse.Namespace) -> str:
    target = _resolve_target(cfg, args.date)
    report = generate_daily_report(cfg, target, args.backfill)
    _deliver(cfg, args, report)
    return report


def run_prev_day(cfg: Config, args: argparse.Namespace) -> str:
    target = _resolve_target(cfg, args.date)
    report = generate_prev_day_report(cfg, target, args.backfill)
    _deliver(cfg, args, report)
    return report


def _deliver(cfg: Config, args: argparse.Namespace, report: str) -> None:
    if args.no_telegram:
        print("\n" + report + "\n")
        return
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    notifier.send(report)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    try:
        cfg = Config.from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    try:
        if args.mode == "daily":
            run_daily(cfg, args)
        else:
            run_prev_day(cfg, args)
    except Exception:  # noqa: BLE001
        logger.exception("Pipeline failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
