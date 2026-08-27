"""Standalone setup checker for WatchData.

Verifies each external dependency independently and prints a clear OK/FAIL
line for each, so you can confirm your .env is correct before relying on the
scheduled job.

Usage (from the project root, with your virtualenv active):

    # Windows PowerShell needs UTF-8 output for the emoji in reports:
    $env:PYTHONIOENCODING = "utf-8"
    python check_setup.py

Exit code is 0 only if all three checks pass.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from watchdata.config import Config


def check_zepp(c: Config) -> bool:
    from watchdata.zepp_client import ZeppClient

    # A single login attempt: if Huami is rate-limiting your IP (HTTP 429),
    # retrying immediately only extends the block. Wait and rerun instead.
    z = ZeppClient(
        c.zepp_email,
        c.zepp_password,
        c.zepp_region,
        c.zepp_user_id,
        max_retries=1,
        app_token=c.zepp_app_token,
    )
    z.login()
    data = z.fetch_band_data(date.today() - timedelta(days=7), date.today())
    days = sorted(d.isoformat() for d in data)
    print(f"[OK] Zepp: logged in (user_id={z.user_id}); fetched {len(data)} day(s): {days}")
    for d in sorted(data)[-3:]:
        m = data[d]
        print(
            f"      {m.day}: steps={m.steps} sleep_min={m.sleep_total_min} "
            f"deep={m.sleep_deep_min} rhr={m.resting_hr} spo2={m.spo2_avg}"
        )
    return True


def check_llm(c: Config) -> bool:
    from watchdata.llm import LLMAnalyzer

    a = LLMAnalyzer(c.openai_api_key, c.llm_model, c.openai_base_url)
    if a._client is None:
        print("[FAIL] LLM: client did not initialize")
        return False
    reply = a._chat("You are a test.", "Reply with exactly: PONG")
    if not reply:
        print("[FAIL] LLM: no response (pipeline would use deterministic fallback)")
        return False
    print(f"[OK] LLM: model={c.llm_model} responded: {reply!r}")
    return True


def check_telegram(c: Config) -> bool:
    import requests

    me = requests.get(
        f"https://api.telegram.org/bot{c.telegram_bot_token}/getMe", timeout=20
    ).json()
    if not me.get("ok"):
        print(f"[FAIL] Telegram: token invalid -> {me}")
        return False
    from watchdata.telegram_notifier import TelegramNotifier

    TelegramNotifier(c.telegram_bot_token, c.telegram_chat_id).send(
        "WatchData setup check OK"
    )
    print(
        f"[OK] Telegram: bot @{me['result'].get('username')} sent a message "
        f"to chat {c.telegram_chat_id}"
    )
    return True


def main() -> int:
    c = Config.from_env()
    results = {}
    for name, fn in (("Zepp", check_zepp), ("LLM", check_llm), ("Telegram", check_telegram)):
        try:
            results[name] = fn(c)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            results[name] = False
    print("\nSummary:", ", ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
