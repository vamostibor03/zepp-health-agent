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
import re
import time
from datetime import date

import requests

from .config import Config, configure_logging
from .main import generate_daily_report
from .models import MANUAL_METRICS, resolve_manual_metric
from .storage import Storage
from .telegram_notifier import TelegramNotifier
from .transformation import compute_summary, format_summary_text

logger = logging.getLogger("watchdata.bot")

_HELP = (
    "\U0001f4cb <b>Commands</b>\n"
    "\n<b>Reports</b>\n"
    "/today \u2014 full report for <b>today</b>\n"
    "/yesterday \u2014 full report for <b>yesterday</b>\n"
    "/summary \u2014 transformation summary (weight, activity, sleep, recovery)\n"
    "\n<b>Log measurements</b> (optionally add a date at the end)\n"
    "/weight 116.8\n"
    "/waist 92  \u00b7  /chest  \u00b7  /hips  \u00b7  /neck  \u00b7  /bodyfat 22\n"
    "/log calories=2450 protein=185 weight=116.8\n"
    "\n/help \u2014 show this message"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_entries(
    cmd: str, args: list[str]
) -> tuple[list[tuple[str, float]], str | None, list[str]]:
    """Return (entries, iso_date_or_None, errors) for a logging command."""
    entries: list[tuple[str, float]] = []
    errors: list[str] = []
    day: str | None = None
    rest: list[str] = []
    for tok in args:
        if _DATE_RE.match(tok):
            day = tok
        else:
            rest.append(tok)

    def num(raw: str) -> float | None:
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            return None

    if cmd == "log":
        for tok in rest:
            if "=" not in tok:
                errors.append(f"'{tok}' (use key=value)")
                continue
            k, v = tok.split("=", 1)
            key = resolve_manual_metric(k)
            if not key:
                errors.append(f"unknown metric '{k}'")
                continue
            value = num(v)
            if value is None:
                errors.append(f"bad number '{v}' for {k}")
                continue
            entries.append((key, value))
    else:
        key = resolve_manual_metric(cmd)
        if not rest:
            errors.append("no value given")
        else:
            value = num(rest[0])
            if value is None:
                errors.append(f"bad number '{rest[0]}'")
            elif key:
                entries.append((key, value))
    return entries, day, errors


def _record(cfg: Config, notifier: TelegramNotifier, entries, iso_day: str | None) -> None:
    target = date.fromisoformat(iso_day) if iso_day else cfg.today()
    logged: list[str] = []
    with Storage(cfg.database_path) as s:
        for key, value in entries:
            meta = MANUAL_METRICS[key]
            s.set_manual(target, key, value, meta["unit"])
            logged.append(f"\u2705 {meta['label']}: {value} {meta['unit']}".rstrip())
    notifier.send(f"Logged for {target.isoformat()}:\n" + "\n".join(logged))


def _send_summary(cfg: Config, notifier: TelegramNotifier) -> None:
    with Storage(cfg.database_path) as s:
        data = compute_summary(s, cfg.today())
    notifier.send("<pre>" + format_summary_text(data) + "</pre>")


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
                    {"command": "summary", "description": "Transformation summary"},
                    {"command": "weight", "description": "Log weight, e.g. /weight 116.8"},
                    {"command": "waist", "description": "Log waist cm, e.g. /waist 92"},
                    {"command": "log", "description": "Log multiple, e.g. /log calories=2450 protein=185"},
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

            # Normalize "/today@MyBot" -> "today"; keep the rest as args.
            parts = text.split()
            cmd = parts[0][1:].split("@")[0].lower()
            args = parts[1:]
            try:
                if cmd == "today":
                    _handle(cfg, notifier, "today")
                elif cmd in ("yesterday", "prev", "previous"):
                    _handle(cfg, notifier, "yesterday")
                elif cmd == "summary":
                    _send_summary(cfg, notifier)
                elif cmd in ("help", "start"):
                    notifier.send(_HELP)
                elif cmd == "log" or resolve_manual_metric(cmd):
                    entries, iso_day, errors = _parse_entries(cmd, args)
                    if entries:
                        _record(cfg, notifier, entries, iso_day)
                    if errors:
                        notifier.send("\u26a0\ufe0f Could not log: " + "; ".join(errors))
                    if not entries and not errors:
                        notifier.send("Nothing to log.\n\n" + _HELP)
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
