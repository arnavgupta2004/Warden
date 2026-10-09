"""Thin wrapper around Claude. Every caller has a deterministic fallback, so
Warden still enforces policy when no API key is configured."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import settings

log = logging.getLogger("warden.llm")

_client = None


def client():
    global _client
    if _client is None and settings.llm_enabled:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def enabled() -> bool:
    return settings.llm_enabled


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start > 0:
        text = text[start:]
    return json.loads(text)


async def complete_json(system: str, user: str, max_tokens: int = 1500) -> Any | None:
    """Ask Claude for a JSON object. Returns None on any failure."""
    c = client()
    if c is None:
        return None
    try:
        msg = await c.messages.create(
            model=settings.llm_model,
            max_tokens=max_tokens,
            system=system + "\nRespond with a single JSON object and nothing else.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _extract_json(text)
    except Exception as exc:  # network, auth, parse
        log.warning("LLM call failed: %s", exc)
        return None


async def complete_text(system: str, user: str, max_tokens: int = 400) -> str | None:
    c = client()
    if c is None:
        return None
    try:
        msg = await c.messages.create(
            model=settings.llm_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None
