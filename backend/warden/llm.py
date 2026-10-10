"""Thin wrapper around Gemini. Every caller has a deterministic fallback, so
Warden still enforces policy when no API key is configured, and the model can
only ever make a decision stricter."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("warden.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
SLOW_TIMEOUT_S = 25.0  # mandate compiling and agent turns, which keep the model's normal thinking
RETRY_503_DELAY_S = 0.5
COOLDOWN_429_S = 60.0  # after a quota error, skip Gemini (deterministic path only) for this long
CACHE_MAX = 2000  # cached answers kept on disk; the oldest is evicted first

_http: httpx.AsyncClient | None = None
_limited_until = 0.0
_last_429_log = 0.0
_cache: OrderedDict[str, Any] | None = None
_last_quota: dict[str, Any] = {}  # what Gemini's last 429 said: which quota, its limit, retry delay


class RateLimited(RuntimeError):
    pass


def enabled() -> bool:
    return settings.llm_enabled


def limited() -> bool:
    """True while Gemini is cooling down after a quota (429) error."""
    return time.monotonic() < _limited_until


def parse_quota(body: str) -> dict[str, Any]:
    """Pull the quota name, its limit and the retry delay out of a Gemini 429 body
    (google.rpc.QuotaFailure / RetryInfo details). Missing parts are left out."""
    out: dict[str, Any] = {}
    try:
        details = json.loads(body).get("error", {}).get("details", [])
    except (ValueError, AttributeError):
        return out
    for d in details:
        kind = d.get("@type", "")
        if kind.endswith("QuotaFailure") and d.get("violations"):
            v = d["violations"][0]
            out["quota"] = v.get("quotaId") or v.get("quotaMetric")
            if v.get("quotaValue") is not None:
                out["limit"] = int(v["quotaValue"])
        elif kind.endswith("RetryInfo") and (m := re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))):
            out["retry_after_s"] = float(m.group(1))
    return out


def quota_info() -> dict[str, Any]:
    return dict(_last_quota)


def _note_429(body: str) -> None:
    global _limited_until, _last_429_log, _last_quota
    now = time.monotonic()
    _last_quota = parse_quota(body)
    # Gemini says how long to wait; a per-day quota can mean hours, so don't retry every minute.
    cooldown = min(max(_last_quota.get("retry_after_s", COOLDOWN_429_S), COOLDOWN_429_S), 3600.0)
    _limited_until = now + cooldown
    if now - _last_429_log >= 60:
        _last_429_log = now
        log.warning("Gemini quota exceeded (429): %s. Using Warden's deterministic path for %ds.",
                    _last_quota or body[:200], int(cooldown))


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


# -- response cache ------------------------------------------------------
# Identical prompts get identical answers, so repeated demo runs and restarts
# reuse earlier Gemini answers instead of spending free-tier quota. Failures
# and empty answers are never stored. Agent turns don't use this.

def cache_key(kind: str, system: str, user: str, schema: dict[str, Any] | None) -> str:
    blob = json.dumps([settings.llm_model, kind, system, user, schema], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_load() -> OrderedDict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = OrderedDict(json.loads(Path(settings.llm_cache_path).read_text()))
        except (OSError, ValueError):
            _cache = OrderedDict()
    return _cache


def cache_get(key: str) -> Any | None:
    return _cache_load().get(key)


def cache_put(key: str, value: Any) -> None:
    if value in (None, "", {}, []):
        return
    cache = _cache_load()
    cache[key] = value
    while len(cache) > CACHE_MAX:
        cache.popitem(last=False)
    path = Path(settings.llm_cache_path)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only disk only costs repeat calls


def cache_reset() -> None:
    """Forget the in-memory copy (tests, or after changing WARDEN_LLM_CACHE)."""
    global _cache
    _cache = None


async def complete_json(system: str, user: str, schema: dict[str, Any] | None = None, quick: bool = True) -> Any | None:
    """Ask the model for a JSON object (JSON mode, optionally constrained by a JSON Schema).
    quick=True uses minimal thinking and the short timeout. Cached; returns None on any failure."""
    if not enabled():
        return None
    key = cache_key("json", system, user, schema)
    if (hit := cache_get(key)) is not None:
        return hit
    cfg: dict[str, Any] = {"responseMimeType": "application/json"}
    if schema:
        cfg["responseJsonSchema"] = schema
    try:
        text = await _complete(system, user, cfg, quick)
        j = _extract_json(text) if text else None
        cache_put(key, j)
        return j
    except RateLimited:
        return None  # already logged, at most once a minute
    except Exception as exc:  # network, auth, timeout, parse
        log.warning("LLM call failed: %r", exc)
        return None


async def complete_text(system: str, user: str, quick: bool = True) -> str | None:
    if not enabled():
        return None
    key = cache_key("text", system, user, None)
    if (hit := cache_get(key)) is not None:
        return hit
    try:
        text = await _complete(system, user, {}, quick)
        text = text.strip() if text else None
        cache_put(key, text)
        return text
    except RateLimited:
        return None
    except Exception as exc:
        log.warning("LLM call failed: %r", exc)
        return None
