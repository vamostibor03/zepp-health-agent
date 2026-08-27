"""Central configuration loaded from environment variables / .env file."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime (CI passes real env vars)
    pass


logger = logging.getLogger("watchdata")


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"See .env.example for the full list."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    """Runtime configuration for the pipeline."""

    zepp_email: str
    zepp_password: str
    zepp_region: str
    zepp_user_id: str
    zepp_app_token: str

    openai_api_key: str
    openai_base_url: str
    llm_model: str

    telegram_bot_token: str
    telegram_chat_id: str

    database_path: str
    timezone: str = "UTC"

    tzinfo: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        try:
            self.tzinfo = ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001 - fall back gracefully to UTC
            logger.warning("Unknown timezone %r, falling back to UTC", self.timezone)
            self.tzinfo = ZoneInfo("UTC")

    # -- Derived helpers -------------------------------------------------------
    def today(self) -> date:
        return datetime.now(self.tzinfo).date()

    def yesterday(self) -> date:
        return self.today() - timedelta(days=1)

    @classmethod
    def from_env(cls) -> "Config":
        # The OpenAI SDK reads OPENAI_BASE_URL straight from the environment.
        # CI often sets it to an empty string (from an undefined secret), which
        # produces an invalid, scheme-less base URL and a connection error.
        # Drop it entirely when blank so the SDK uses its default endpoint.
        if not os.environ.get("OPENAI_BASE_URL", "").strip():
            os.environ.pop("OPENAI_BASE_URL", None)

        # Two supported auth modes:
        #   1. Token mode (preferred, avoids login rate limits): supply
        #      ZEPP_APP_TOKEN + ZEPP_USER_ID extracted from user.huami.com.
        #   2. Credential mode: supply ZEPP_EMAIL + ZEPP_PASSWORD.
        app_token = _optional("ZEPP_APP_TOKEN")
        user_id = _optional("ZEPP_USER_ID")
        email = _optional("ZEPP_EMAIL")
        password = _optional("ZEPP_PASSWORD")

        has_token = bool(app_token and user_id)
        has_creds = bool(email and password)
        if not (has_token or has_creds):
            raise RuntimeError(
                "Zepp auth not configured. Provide either "
                "ZEPP_APP_TOKEN + ZEPP_USER_ID (recommended) or "
                "ZEPP_EMAIL + ZEPP_PASSWORD. See .env.example."
            )

        return cls(
            zepp_email=email,
            zepp_password=password,
            zepp_region=_optional("ZEPP_REGION", "de").lower(),
            zepp_user_id=user_id,
            zepp_app_token=app_token,
            openai_api_key=_require("OPENAI_API_KEY"),
            openai_base_url=_optional("OPENAI_BASE_URL"),
            llm_model=_optional("LLM_MODEL", "gpt-4o-mini"),
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
            database_path=_optional("DATABASE_PATH", "data/health.db"),
            timezone=_optional("TIMEZONE", "UTC"),
        )


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
