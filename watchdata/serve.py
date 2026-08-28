"""Combined single-host runner: MCP server + Telegram bot (+ optional daily job).

Designed for a host with a persistent volume (e.g. Fly.io) so that manually
logged data (via the bot) and the data served to ChatGPT (via the MCP) live in
ONE database - no more diverging copies.

Processes started (in one container):

* MCP server  - streamable-http on $HOST:$PORT (main thread).
* Telegram bot - long-polls for /today, /weight, /log, /summary, ... (thread).
* Daily job   - optional; when RUN_DAILY_UTC="HH:MM" is set, fetches the watch
                data and sends the daily report at that UTC time (thread). This
                keeps the shared DB fresh on the host. If you use this, disable
                the GitHub Actions schedule to avoid duplicate reports.

The database lives at $DATABASE_PATH (e.g. /data/health.db on a Fly volume).
On first boot, if that file is missing, it is seeded from the history bundled
in the image (data/health.db) so you start with existing days.

Required env (same as the pipeline): ZEPP_*, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, plus HOST/PORT/DATABASE_PATH and optional MCP_ALLOWED_HOSTS,
RUN_DAILY_UTC.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import Config, configure_logging

logger = logging.getLogger("watchdata.serve")

# Path to the history bundled in the image (relative to the repo root).
_SEED_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "health.db")


def _seed_database(db_path: str) -> None:
    """Copy the bundled history to the volume on first boot, if needed."""
    if os.path.exists(db_path):
        return
    if os.path.abspath(db_path) == os.path.abspath(_SEED_DB):
        return
    if os.path.exists(_SEED_DB):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        shutil.copy2(_SEED_DB, db_path)
        logger.info("Seeded database at %s from bundled history", db_path)
    else:
        logger.info("No bundled history to seed; starting with an empty DB at %s", db_path)


def _run_bot() -> None:
    from .bot import main as bot_main

    try:
        bot_main()
    except Exception:  # noqa: BLE001 - never let the bot thread kill the host
        logger.exception("Telegram bot thread crashed")


def _run_daily_scheduler(cfg: Config, hhmm: str) -> None:
    from .main import generate_daily_report
    from .telegram_notifier import TelegramNotifier

    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except ValueError:
        logger.error("Invalid RUN_DAILY_UTC=%r (expected HH:MM); scheduler off", hhmm)
        return

    logger.info("Daily scheduler on: will run at %02d:%02d UTC", hour, minute)
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        try:
            report = generate_daily_report(cfg, cfg.yesterday())
            TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id).send(report)
            logger.info("Daily report sent for %s", cfg.yesterday().isoformat())
        except Exception:  # noqa: BLE001
            logger.exception("Daily scheduled report failed")


def main() -> int:
    configure_logging()
    try:
        cfg = Config.from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    _seed_database(cfg.database_path)

    threading.Thread(target=_run_bot, name="telegram-bot", daemon=True).start()

    hhmm = os.environ.get("RUN_DAILY_UTC", "").strip()
    if hhmm:
        threading.Thread(
            target=_run_daily_scheduler, args=(cfg, hhmm), name="daily", daemon=True
        ).start()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting MCP server on %s:%s (bot running alongside)", host, port)

    from .mcp_server import serve_http

    serve_http(host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
