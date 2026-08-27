"""LLM analysis of health metrics + trends.

Uses the OpenAI SDK, which also works against any OpenAI-compatible endpoint
via ``OPENAI_BASE_URL``. If the LLM call fails for any reason we fall back to a
deterministic, locally-generated summary so the pipeline still delivers a
report.
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import DailyMetrics
from .trends import TrendReport

logger = logging.getLogger("watchdata.llm")

DAILY_SYSTEM_PROMPT = (
    "You are a concise, encouraging personal health analyst. You receive one "
    "day of wearable (Amazfit/Zepp) metrics plus how they compare to the "
    "user's trailing 7/30/90-day averages. Write a short daily report.\n\n"
    "Guidelines:\n"
    "- Open with a one-line headline summarizing the day.\n"
    "- Call out 2-4 notable changes vs the baselines (improvements AND "
    "regressions), citing concrete numbers and the % change.\n"
    "- Comment on sleep, activity, and heart/recovery if data is present.\n"
    "- End with 1-2 specific, actionable suggestions for today.\n"
    "- Be honest but supportive. Never invent data that isn't provided.\n"
    "- Do NOT give medical diagnoses; suggest seeing a professional only if a "
    "metric looks genuinely alarming.\n"
    "- Keep it under ~180 words. Plain text, no markdown headers."
)

PREV_DAY_SYSTEM_PROMPT = (
    "You are a concise personal health analyst. You receive a single day of "
    "wearable (Amazfit/Zepp) metrics with no historical comparison. Write a "
    "brief, factual recap of that day: highlight sleep, activity and heart "
    "metrics that are present, note anything that stands out, and give one "
    "actionable tip. Under ~120 words, plain text, no markdown headers, no "
    "invented data."
)


class LLMAnalyzer:
    def __init__(
        self, api_key: str, model: str = "gpt-4o-mini", base_url: str = ""
    ) -> None:
        self.model = model
        self._client = None
        try:
            from openai import OpenAI

            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("Could not initialize OpenAI client; will use fallback")

    def analyze_daily(
        self, target: DailyMetrics, trends: TrendReport
    ) -> str:
        prompt = _build_daily_prompt(target, trends)
        return self._chat(DAILY_SYSTEM_PROMPT, prompt) or _fallback_daily(
            target, trends
        )

    def analyze_prev_day(self, target: DailyMetrics) -> str:
        prompt = _build_prev_day_prompt(target)
        return self._chat(PREV_DAY_SYSTEM_PROMPT, prompt) or _fallback_prev_day(
            target
        )

    def _chat(self, system: str, user: str) -> Optional[str]:
        if self._client is None:
            return None
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.5,
                max_tokens=500,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception:  # noqa: BLE001
            logger.exception("LLM request failed; using deterministic fallback")
            return None


# --------------------------------------------------------------------- prompts
def _fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "n/a"
    if abs(value - round(value)) < 1e-9:
        num = f"{int(round(value))}"
    else:
        num = f"{value:.1f}"
    return f"{num}{(' ' + unit) if unit else ''}".strip()


def _build_daily_prompt(target: DailyMetrics, trends: TrendReport) -> str:
    lines = [f"Date analyzed: {target.day.isoformat()}", "", "Metrics vs baselines:"]
    for name, t in trends.metrics.items():
        if t.value is None and all(
            t.averages.get(w) is None for w in trends.as_dict()["windows"]
        ):
            continue
        parts = [f"- {t.label}: today={_fmt(t.value, t.unit)}"]
        for window in (7, 30, 90):
            avg = t.averages.get(window)
            pct = t.pct_change.get(window)
            n = t.sample_sizes.get(window, 0)
            if avg is None:
                parts.append(f"{window}d avg=n/a")
            else:
                pct_str = f"{pct:+.0f}%" if pct is not None else "n/a"
                parts.append(f"{window}d avg={_fmt(avg, t.unit)} ({pct_str}, n={n})")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def _build_prev_day_prompt(target: DailyMetrics) -> str:
    from .models import METRIC_LABELS

    lines = [f"Date: {target.day.isoformat()}", "", "Metrics:"]
    values = target.numeric_fields()
    for name, meta in METRIC_LABELS.items():
        if name in values:
            lines.append(f"- {meta['label']}: {_fmt(values[name], meta['unit'])}")
    if len(lines) == 3:
        lines.append("- (no metrics recorded)")
    return "\n".join(lines)


# ------------------------------------------------------------------- fallbacks
def _fallback_daily(target: DailyMetrics, trends: TrendReport) -> str:
    highlights: list[str] = []
    for name, t in trends.metrics.items():
        pct = t.pct_change.get(7)
        if t.value is not None and pct is not None and abs(pct) >= 10:
            direction = "up" if pct > 0 else "down"
            highlights.append(
                f"{t.label} {direction} {abs(pct):.0f}% vs 7-day avg "
                f"({_fmt(t.value, t.unit)} vs {_fmt(t.averages.get(7), t.unit)})"
            )
    body = "; ".join(highlights[:4]) if highlights else "steady vs recent baselines"
    return (
        f"Daily health recap for {target.day.isoformat()} (auto-generated):\n"
        f"Notable changes: {body}.\n"
        "Keep an eye on sleep consistency and daily movement."
    )


def _fallback_prev_day(target: DailyMetrics) -> str:
    values = target.numeric_fields()
    from .models import METRIC_LABELS

    parts = [
        f"{METRIC_LABELS[n]['label']} {_fmt(v, METRIC_LABELS[n]['unit'])}"
        for n, v in values.items()
        if n in METRIC_LABELS
    ]
    summary = ", ".join(parts[:8]) if parts else "no metrics recorded"
    return f"Recap for {target.day.isoformat()} (auto-generated): {summary}."
