"""Thin wrapper around Gemini. Every caller has a deterministic fallback, so
Warden still enforces policy when no API key is configured, and the model can
only ever make a decision stricter."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("warden.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
SLOW_TIMEOUT_S = 25.0  # mandate compiling and agent turns, which keep the model's normal thinking
RETRY_503_DELAY_S = 0.5
COOLDOWN_429_S = 60.0  # after a quota error, skip Gemini (deterministic path only) for this long

_http: httpx.AsyncClient | None = None
_limited_until = 0.0
_last_429_log = 0.0


class RateLimited(RuntimeError):
    pass


def enabled() -> bool:
    return settings.llm_enabled


def limited() -> bool:
    """True while Gemini is cooling down after a quota (429) error."""
    return time.monotonic() < _limited_until


def _note_429(body: str) -> None:
    global _limited_until, _last_429_log
    now = time.monotonic()
    _limited_until = now + COOLDOWN_429_S
    if now - _last_429_log >= 60:
        _last_429_log = now
        log.warning("Gemini quota exceeded (429); using Warden's deterministic path for %ds. %s",
                    int(COOLDOWN_429_S), body[:200])


def quick_thinking(model: str) -> dict[str, Any] | None:
    """thinkingConfig for low-latency calls. Gemini 3+ takes thinkingLevel (an error on
    older models). LOW is the one level every Gemini 3+ model accepts; MINIMAL is
    rejected by some (e.g. gemini-3.8-flash). 2.5 takes thinkingBudget, where Pro
    cannot go below 128. Models without thinking reject thinkingConfig entirely."""
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", model)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    pro = "-pro" in model
    if major >= 3:
        return {"thinkingLevel": "LOW"}
    if (major, minor) >= (2, 5):
        return {"thinkingBudget": 128 if pro else 0}
    return None


async def gemini_generate(body: dict[str, Any], timeout: float = SLOW_TIMEOUT_S) -> dict[str, Any]:
    """POST a generateContent request to Gemini and return the raw response.
    Raises RateLimited during a quota cooldown, RuntimeError on other HTTP
    errors, and asyncio.TimeoutError past `timeout` seconds."""
    global _http
    if limited():
        raise RateLimited("Gemini is cooling down after a quota error")
    if _http is None:
        _http = httpx.AsyncClient(timeout=SLOW_TIMEOUT_S + 5)

    async def post() -> httpx.Response:
        url = f"{GEMINI_BASE}/models/{settings.llm_model}:generateContent"
        headers = {"x-goog-api-key": settings.gemini_api_key}
        r = await _http.post(url, headers=headers, json=body)
        if r.status_code == 503:  # "high demand" blip: one retry, still inside the hard timeout
            await asyncio.sleep(RETRY_503_DELAY_S)
            r = await _http.post(url, headers=headers, json=body)
        return r

    r = await asyncio.wait_for(post(), timeout)
    if r.status_code == 429:  # quota: never retry; fall back now and cool down
        _note_429(r.text)
        raise RateLimited("Gemini quota exceeded")
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    return r.json()


def gemini_text(resp: dict[str, Any]) -> str:
    parts = ((resp.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start > 0:
        text = text[start:]
    return json.loads(text)


async def _complete(system: str, user: str, cfg: dict[str, Any], quick: bool) -> str | None:
    if not enabled():
        return None
    # No maxOutputTokens: on thinking models it also caps the thinking, which can leave no answer.
    if quick and (thinking := quick_thinking(settings.llm_model)):
        cfg = {**cfg, "thinkingConfig": thinking}
    resp = await gemini_generate({"systemInstruction": {"parts": [{"text": system}]},
                                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                                  "generationConfig": cfg},
                                 timeout=settings.llm_timeout_s if quick else SLOW_TIMEOUT_S)
    return gemini_text(resp)


async def complete_json(system: str, user: str, schema: dict[str, Any] | None = None, quick: bool = True) -> Any | None:
    """Ask the model for a JSON object (JSON mode, optionally constrained by a JSON Schema).
    quick=True uses minimal thinking and the short timeout. Returns None on any failure."""
    cfg: dict[str, Any] = {"responseMimeType": "application/json"}
    if schema:
        cfg["responseJsonSchema"] = schema
    try:
        text = await _complete(system, user, cfg, quick)
        return _extract_json(text) if text else None
    except RateLimited:
        return None  # already logged, at most once a minute
    except Exception as exc:  # network, auth, timeout, parse
        log.warning("LLM call failed: %r", exc)
        return None


async def complete_text(system: str, user: str, quick: bool = True) -> str | None:
    try:
        text = await _complete(system, user, {}, quick)
        return text.strip() if text else None
    except RateLimited:
        return None
    except Exception as exc:
        log.warning("LLM call failed: %r", exc)
        return None
