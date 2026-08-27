"""Client for the (unofficial) Amazfit / Zepp "Huami" cloud API.

There is no official public API for Zepp. This module reproduces the login and
data-download flow the Zepp mobile app uses:

    1. Exchange email + password for a short-lived ``access`` token.
    2. Exchange that token for a long-lived ``app_token`` + ``user_id``.
    3. Call the ``band_data`` endpoint to download per-day summaries.

The per-day "summary" field is a base64-encoded JSON blob whose keys are terse
(``stp`` = steps, ``slp`` = sleep, ...). We decode and normalize it into
:class:`~watchdata.models.DailyMetrics`.

Because this is an unofficial API, endpoints/regions occasionally change. All
parsing is defensive: unknown/missing fields simply become ``None``.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
import uuid
from datetime import date, timedelta
from typing import Any, Optional

import requests

from .models import DailyMetrics

logger = logging.getLogger("watchdata.zepp")

# Regional data hosts for the band_data endpoint (newer *.zepp.com hosts).
_DATA_HOSTS = {
    "us": "api-mifit-us2.zepp.com",
    "de": "api-mifit-de2.zepp.com",
    "eu": "api-mifit-de2.zepp.com",
    "global": "api-mifit.huami.com",
    "cn": "api-mifit.huami.com",
}


class ZeppAuthError(RuntimeError):
    """Raised when authentication with the Huami cloud fails."""


class ZeppClient:
    def __init__(
        self,
        email: str,
        password: str,
        region: str = "de",
        user_id: str = "",
        timeout: int = 30,
        max_retries: int = 4,
        app_token: str = "",
    ) -> None:
        self.email = email
        self.password = password
        self.region = (region or "de").lower()
        self._forced_user_id = user_id
        self.timeout = timeout
        self.max_retries = max_retries

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MiFit/5.9.2 (Android 11)"})

        # If an app_token is supplied we use it directly and never touch the
        # login endpoint (which is aggressively rate limited). Tokens are
        # extracted from user.huami.com and last several weeks.
        self.app_token: Optional[str] = app_token or None
        self.login_token: Optional[str] = None
        self.user_id: Optional[str] = user_id or None
        # A stable pseudo device id; format matches what the app sends.
        self._device_id = "02:00:00:%02x:%02x:%02x" % tuple(
            uuid.uuid4().bytes[:3]
        )

    # ------------------------------------------------------------------ auth
    def login(self) -> None:
        # Token mode: nothing to do, we already have credentials.
        if self.app_token and self.user_id:
            logger.info(
                "Using supplied Zepp app_token (user_id=%s); skipping login",
                self.user_id,
            )
            return
        if self.app_token and not self.user_id:
            raise ZeppAuthError(
                "ZEPP_APP_TOKEN was provided without ZEPP_USER_ID; both are "
                "required for token auth."
            )
        access_token, country_code = self._get_access_token()
        self._login_with_token(access_token, country_code)
        logger.info("Authenticated with Zepp cloud (user_id=%s)", self.user_id)

    def _post_with_retry(self, url: str, **kwargs: Any) -> requests.Response:
        """POST with exponential backoff on HTTP 429 / transient 5xx.

        Respects a ``Retry-After`` header when present.
        """
        delay = 5.0
        last: Optional[requests.Response] = None
        for attempt in range(1, self.max_retries + 1):
            resp = self.session.post(url, timeout=self.timeout, **kwargs)
            last = resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
            if attempt == self.max_retries:
                break
            wait = delay
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = max(wait, float(retry_after))
            logger.warning(
                "Huami returned %s (attempt %d/%d); retrying in %.0fs",
                resp.status_code,
                attempt,
                self.max_retries,
                wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
        return last  # type: ignore[return-value]

    def _get_access_token(self) -> tuple[str, str]:
        """Step 1: email/password -> access token (via 302 redirect)."""
        url = (
            "https://api-user.huami.com/registrations/"
            f"{urllib.parse.quote(self.email)}/tokens"
        )
        data = {
            "state": "REDIRECTION",
            "client_id": "HuaMi",
            "password": self.password,
            "redirect_uri": (
                "https://s3-us-west-2.amazonaws.com/hm-registration/"
                "successSignIn.html"
            ),
            "region": "us-west-2",
            "token": "access",
            "country_code": "US",
        }
        resp = self._post_with_retry(url, data=data, allow_redirects=False)
        location = resp.headers.get("Location", "")
        if not location:
            hint = (
                "Rate limited by Huami (HTTP 429) - wait a few minutes and "
                "retry."
                if resp.status_code == 429
                else "Check email/password."
            )
            raise ZeppAuthError(
                "No redirect returned from token endpoint "
                f"(status={resp.status_code}). {hint}"
            )

        parsed = urllib.parse.urlparse(location)
        params = urllib.parse.parse_qs(parsed.query)
        if "access" not in params:
            error = params.get("error", ["unknown"])[0]
            raise ZeppAuthError(
                f"Login failed (error={error}). "
                "Double-check ZEPP_EMAIL / ZEPP_PASSWORD."
            )
        access_token = params["access"][0]
        country_code = params.get("country_code", ["US"])[0]
        return access_token, country_code

    def _login_with_token(self, access_token: str, country_code: str) -> None:
        """Step 2: access token -> long-lived app_token + user_id."""
        url = "https://account.huami.com/v2/client/login"
        data = {
            "app_name": "com.huami.midong",
            "app_version": "6.3.3",
            "code": access_token,
            "country_code": country_code,
            "device_id": self._device_id,
            "device_model": "android_phone",
            "grant_type": "access_token",
            "third_name": "huami",
            "lang": "en",
            "os_version": "11",
            "source": "com.huami.midong",
        }
        resp = self.session.post(url, data=data, timeout=self.timeout)
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ZeppAuthError(
                f"Login response was not JSON (status={resp.status_code})."
            ) from exc

        token_info = payload.get("token_info")
        if not token_info or "app_token" not in token_info:
            raise ZeppAuthError(f"Login did not return an app_token: {payload}")

        self.app_token = token_info["app_token"]
        self.login_token = token_info.get("login_token")
        if not self._forced_user_id:
            self.user_id = str(token_info.get("user_id", "")) or None

    # ------------------------------------------------------------------ data
    def _data_host(self) -> str:
        return _DATA_HOSTS.get(self.region, _DATA_HOSTS["de"])

    def fetch_band_data(
        self, from_date: date, to_date: date
    ) -> dict[date, DailyMetrics]:
        """Download and normalize daily summaries in the inclusive range."""
        if not self.app_token or not self.user_id:
            raise ZeppAuthError("Not logged in; call login() first.")

        url = f"https://{self._data_host()}/v1/data/band_data.json"
        params = {
            "query_type": "summary",
            "device_type": "android_phone",
            "userid": self.user_id,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
        }
        headers = {"apptoken": self.app_token}
        resp = self.session.get(
            url, params=params, headers=headers, timeout=self.timeout
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"band_data request failed (status={resp.status_code}): "
                f"{resp.text[:200]}"
            )

        payload = resp.json()
        items = payload.get("data", []) or []
        logger.info(
            "Fetched %d day(s) of band data (%s -> %s)",
            len(items),
            from_date,
            to_date,
        )

        result: dict[date, DailyMetrics] = {}
        for item in items:
            try:
                metrics = self._parse_day(item)
            except Exception:  # noqa: BLE001 - never let one bad day kill the run
                logger.exception("Failed to parse day item: %s", item.get("date"))
                continue
            if metrics is not None:
                result[metrics.day] = metrics
        return result

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _decode_summary(summary_b64: str) -> dict[str, Any]:
        if not summary_b64:
            return {}
        raw = base64.b64decode(summary_b64)
        return json.loads(raw.decode("utf-8"))

    def _parse_day(self, item: dict[str, Any]) -> Optional[DailyMetrics]:
        day_str = item.get("date")
        if not day_str:
            return None
        day = date.fromisoformat(day_str)

        summary = self._decode_summary(item.get("summary", ""))
        metrics = DailyMetrics(day=day, raw={"item": item, "summary": summary})

        # --- Activity (stp block) -------------------------------------------
        stp = summary.get("stp") or {}
        metrics.steps = _as_int(stp.get("ttl"))
        metrics.distance_m = _as_int(stp.get("dis"))
        metrics.calories_kcal = _as_int(stp.get("cal"))
        # runDist/walkDist etc. exist but total distance/cal above is enough.

        # --- Sleep (slp block) ----------------------------------------------
        slp = summary.get("slp") or {}
        deep = _as_int(slp.get("dp"))
        light = _as_int(slp.get("lt"))
        rem = _as_int(slp.get("rem"))
        awake = _as_int(slp.get("wk"))
        metrics.sleep_deep_min = deep
        metrics.sleep_light_min = light
        metrics.sleep_rem_min = rem
        metrics.sleep_awake_min = awake
        # Total: prefer explicit start/end span, else sum of stages.
        if slp.get("st") and slp.get("ed"):
            span = (_as_int(slp["ed"]) or 0) - (_as_int(slp["st"]) or 0)
            metrics.sleep_total_min = max(0, round(span / 60)) if span else None
        if metrics.sleep_total_min is None:
            parts = [p for p in (deep, light, rem) if p is not None]
            metrics.sleep_total_min = sum(parts) if parts else None
        metrics.sleep_score = _as_int(slp.get("scnt")) or _as_int(slp.get("score"))

        # --- Heart rate -----------------------------------------------------
        # Resting/avg HR may live in a couple of places depending on firmware.
        metrics.resting_hr = _as_int(_first(summary, ["rhr", "restHr"])) or _as_int(
            (summary.get("slp") or {}).get("rhr")
        )
        metrics.avg_hr = _as_int(_first(summary, ["avgHr", "hr_avg"]))
        metrics.max_hr = _as_int(_first(summary, ["maxHr", "hr_max"]))
        metrics.min_hr = _as_int(_first(summary, ["minHr", "hr_min"]))

        # --- Other biometrics ----------------------------------------------
        metrics.spo2_avg = _as_int(_first(summary, ["spo2_avg", "spo2"]))
        metrics.stress_avg = _as_int(_first(summary, ["stress_avg", "stress"]))
        pai_val = _first(summary, ["pai", "totalPai"])
        metrics.pai = float(pai_val) if pai_val is not None else None

        return metrics


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None
