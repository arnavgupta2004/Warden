"""Thin wrapper around Gemini. Every caller has a deterministic fallback, so
Warden still enforces policy when no API key is configured, and the model can
only ever make a decision stricter."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("warden.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
SLOW_TIMEOUT_S = 25.0  # mandate compiling and agent turns, which keep the model's normal thinking

_http: httpx.AsyncClient | None = None


def enabled() -> bool:
    return settings.llm_enabled


def quick_thinking(model: str) -> dict[str, Any] | None:
    """thinkingConfig for low-latency calls. Gemini 3+ takes thinkingLevel (an error on
    older models); 2.5 takes thinkingBudget, where Pro cannot go below 128. Models
    without thinking reject thinkingConfig entirely, so they get none."""
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", model)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    pro = "-pro" in model
    if major >= 3:
        return {"thinkingLevel": "LOW" if pro else "MINIMAL"}
    if (major, minor) >= (2, 5):
        return {"thinkingBudget": 128 if pro else 0}
    return None


async def gemini_generate(body: dict[str, Any], timeout: float = SLOW_TIMEOUT_S) -> dict[str, Any]:
    """POST a generateContent request to Gemini and return the raw response.
    Raises on HTTP errors and on asyncio.TimeoutError past `timeout` seconds."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=SLOW_TIMEOUT_S + 5)
    r = await asyncio.wait_for(
        _http.post(f"{GEMINI_BASE}/models/{settings.llm_model}:generateContent",
                   headers={"x-goog-api-key": settings.gemini_api_key}, json=body),
        timeout)
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
    except Exception as exc:  # network, auth, timeout, parse
        log.warning("LLM call failed: %r", exc)
        return None


async def complete_text(system: str, user: str, quick: bool = True) -> str | None:
    try:
        text = await _complete(system, user, {}, quick)
        return text.strip() if text else None
    except Exception as exc:
        log.warning("LLM call failed: %r", exc)
        return None
