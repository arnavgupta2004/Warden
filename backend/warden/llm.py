"""Thin wrapper around Gemini. Every caller has a deterministic fallback, so
Warden still enforces policy when no API key is configured, and the model can
only ever make a decision stricter."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("warden.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

_http: httpx.AsyncClient | None = None


def enabled() -> bool:
    return settings.llm_enabled


async def gemini_generate(body: dict[str, Any]) -> dict[str, Any]:
    """POST a generateContent request to Gemini and return the raw response. Raises on HTTP errors."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=60)
    r = await _http.post(f"{GEMINI_BASE}/models/{settings.llm_model}:generateContent",
                         headers={"x-goog-api-key": settings.gemini_api_key}, json=body)
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


async def _complete(system: str, user: str, want_json: bool) -> str | None:
    if not enabled():
        return None
    # No maxOutputTokens: on thinking models it also caps the thinking, which can leave no answer.
    cfg: dict[str, Any] = {"responseMimeType": "application/json"} if want_json else {}
    resp = await gemini_generate({"systemInstruction": {"parts": [{"text": system}]},
                                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                                  "generationConfig": cfg})
    return gemini_text(resp)


async def complete_json(system: str, user: str) -> Any | None:
    """Ask the model for a JSON object. Returns None on any failure."""
    try:
        text = await _complete(system + "\nRespond with a single JSON object and nothing else.", user, True)
        return _extract_json(text) if text else None
    except Exception as exc:  # network, auth, parse
        log.warning("LLM call failed: %s", exc)
        return None


async def complete_text(system: str, user: str) -> str | None:
    try:
        text = await _complete(system, user, False)
        return text.strip() if text else None
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None
