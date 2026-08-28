"""On-demand Telegram command bot.

Run this as a long-lived process (locally, on a small always-on host, in Docker,
or a systemd service):

    python -m watchdata.bot

It long-polls Telegram's ``getUpdates`` API and responds to:

    /today      Full daily report for TODAY (handy to run at the end of a day).
    /yesterday  Full daily report for YESTERDAY (same as the scheduled job).
    /help       Show the available commands.

Security: only messages coming from the configured ``TELEGRAM_CHAT_ID`` are
honored; anything else is ignored.

Note: this is separate from the scheduled GitHub Action. GitHub Actions cannot
be triggered by a chat message, so the daily 10:30 report still runs from the
workflow while this bot handles interactive commands.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import Config, configure_logging
from .main import generate_daily_report
from .telegram_notifier import TelegramNotifier

logger = logging.getLogger("watchdata.bot")

_HELP = (
    "\U0001f4cb <b>Commands</b>\n"
    "/today \u2014 full report for <b>today</b> (run at the end of the day)\n"
    "/yesterday \u2014 full report for <b>yesterday</b>\n"
    "/help \u2014 show this message"
)


def _handle(cfg: Config, notifier: TelegramNotifier, which: str) -> None:
    target = cfg.today() if which == "today" else cfg.yesterday()
    notifier.send(
        f"\u23f3 Building your <b>{which}</b> report for "
        f"{target.strftime('%A, %d %b %Y')}\u2026"
    )
    report = generate_daily_report(cfg, target)
    notifier.send(report)


def _register_command_menu(base_url: str) -> None:
    """Populate the blue command menu in the Telegram client (best effort)."""
    try:
        requests.post(
            f"{base_url}/setMyCommands",
            json={
                "commands": [
                    {"command": "today", "description": "Full report for today"},
                    {"command": "yesterday", "description": "Full report for yesterday"},
                    {"command": "help", "description": "Show commands"},
                ]
            },
            timeout=30,
        )
    except requests.RequestException:
        logger.warning("Could not set command menu", exc_info=True)


def main() -> int:
    configure_logging()
    try:
        cfg = Config.from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    base_url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    _register_command_menu(base_url)

    offset: int | None = None
    logger.info(
        "Command bot started; long-polling for /today and /yesterday "
        "(authorized chat_id=%s).",
        cfg.telegram_chat_id,
    )

    while True:
        try:
            resp = requests.get(
                f"{base_url}/getUpdates",
                params={
                    "timeout": 50,
                    "offset": offset,
                    "allowed_updates": '["message"]',
                },
                timeout=70,
            )
            payload = resp.json()
        except (requests.RequestException, ValueError):
            logger.warning("getUpdates failed; retrying in 5s", exc_info=True)
            time.sleep(5)
            continue

        if not payload.get("ok", False):
            logger.warning("getUpdates returned not-ok: %s", payload)
            time.sleep(5)
            continue

        for update in payload.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = (message.get("text") or "").strip()

            if chat_id != str(cfg.telegram_chat_id):
                logger.info("Ignoring message from unauthorized chat %s", chat_id)
                continue
            if not text.startswith("/"):
                continue

            # Normalize "/today@MyBot" -> "today".
            cmd = text[1:].split()[0].split("@")[0].lower()
            try:
                if cmd == "today":
                    _handle(cfg, notifier, "today")
                elif cmd in ("yesterday", "prev", "previous"):
                    _handle(cfg, notifier, "yesterday")
                elif cmd in ("help", "start"):
                    notifier.send(_HELP)
                else:
                    notifier.send("Unknown command.\n\n" + _HELP)
            except Exception:  # noqa: BLE001 - keep the bot alive on any failure
                logger.exception("Failed handling command %r", text)
                try:
                    notifier.send(
                        "\u26a0\ufe0f Sorry, that report failed to build. "
                        "Check the bot logs."
                    )
                except requests.RequestException:
                    pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
