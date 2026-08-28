"""Send reports to a Telegram chat via the Bot API."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("watchdata.telegram")

_MAX_LEN = 4096  # Telegram hard limit per message.


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 30) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, text: str) -> None:
        """Send ``text``, splitting into multiple messages if needed."""
        for chunk in _split(text, _MAX_LEN):
            self._send_chunk(chunk)

    def _send_chunk(self, text: str) -> None:
        resp = requests.post(
            self._url,
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Telegram sendMessage failed (status={resp.status_code}): "
                f"{resp.text[:200]}"
            )
        logger.info("Sent Telegram message (%d chars)", len(text))


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            # A single line longer than the limit: hard-split it.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks
