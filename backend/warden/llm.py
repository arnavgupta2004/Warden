"""Thin wrapper around the language model (Claude or Gemini, whichever has a
key). Every caller has a deterministic fallback, so Warden still enforces
policy when no API key is configured."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("warden.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
LABELS = {"anthropic": "Claude", "gemini": "Gemini"}

_client = None
_http: httpx.AsyncClient | None = None


def provider() -> str | None:
    return settings.llm_provider


def label() -> str:
    """Name shown to the owner, e.g. "Gemini intent check"."""
    return LABELS.get(provider() or "", "AI")


def client():
    """Anthropic client, or None when Claude isn't the active provider."""
    global _client
    if _client is None and provider() == "anthropic":
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client if provider() == "anthropic" else None


def enabled() -> bool:
    return settings.llm_enabled


async def gemini_generate(body: dict[str, Any]) -> dict[str, Any]:
    """POST a generateContent request to Gemini and return the raw response. Raises on HTTP errors."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=60)
    r = await _http.post(f"{GEMINI_BASE}/models/{settings.gemini_model}:generateContent",
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


async def _complete(system: str, user: str, max_tokens: int, want_json: bool) -> str | None:
    p = provider()
    if p == "anthropic":
        msg = await client().messages.create(
            model=settings.llm_model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    if p == "gemini":
        # No maxOutputTokens: on thinking models it also caps the thinking, which can leave no answer.
        cfg: dict[str, Any] = {"responseMimeType": "application/json"} if want_json else {}
        resp = await gemini_generate({"systemInstruction": {"parts": [{"text": system}]},
                                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                                      "generationConfig": cfg})
        return gemini_text(resp)
    return None


async def complete_json(system: str, user: str, max_tokens: int = 1500) -> Any | None:
    """Ask the model for a JSON object. Returns None on any failure."""
    try:
        text = await _complete(system + "\nRespond with a single JSON object and nothing else.", user, max_tokens, True)
        return _extract_json(text) if text else None
    except Exception as exc:  # network, auth, parse
        log.warning("LLM call failed: %s", exc)
        return None


async def complete_text(system: str, user: str, max_tokens: int = 400) -> str | None:
    try:
        text = await _complete(system, user, max_tokens, False)
        return text.strip() if text else None
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None
