"""Provenance tracking.

Every piece of content an agent reads through Warden is stored as a span
labelled with its trust level. When the agent later tries to move money,
Warden checks where the critical arguments (who gets paid, how much) first
appeared. A payee that only ever appeared inside an inbound email or a web
page, and never in anything the owner said or PayPal returned, is tainted.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict

from .models import ContextSpan, Trust

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Phrases typical of instructions smuggled into data.
INJECTION_PHRASES = [
    r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)",
    r"disregard (?:the |your )?(?:previous|prior|system|above)",
    r"\b(?:system|assistant|admin)\s*(?:prompt|override|message|note)\s*:",
    r"you are now",
    r"new instructions?",
    r"do not (?:tell|inform|notify|mention)",
    r"without (?:asking|approval|confirmation|telling)",
    r"(?:delete|cancel|remove) (?:this|the) (?:invoice|record|email|log)",
    r"(?:as an? )?(?:ai|agent|assistant),? (?:you must|please)",
    r"urgent(?:ly)?[^.]{0,40}(?:payout|transfer|wire|send)",
]

HIDDEN_CSS = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(?:\.0+)?\b|"
    r"color\s*:\s*(?:#fff(?:fff)?|white|rgba?\(\s*255\s*,\s*255\s*,\s*255)|left\s*:\s*-\d{3,}px)",
    re.I,
)
ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")


def html_to_text(raw: str) -> str:
    """Flatten HTML the way an LLM agent's fetch tool usually does: hidden
    elements and comments are kept, because that is what the model sees."""
    s = re.sub(r"<!--(.*?)-->", r" \1 ", raw, flags=re.S)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def scan_injection(raw: str, text: str) -> list[str]:
    signals: list[str] = []
    low = text.lower()
    for pat in INJECTION_PHRASES:
        m = re.search(pat, low)
        if m:
            signals.append(f"instruction-like phrase: \"{m.group(0)}\"")
    if HIDDEN_CSS.search(raw):
        signals.append("text hidden with CSS (invisible to a human reader)")
    if re.search(r"<!--.*?(?:pay|refund|send|transfer|instruction).*?-->", raw, re.S | re.I):
        signals.append("instructions inside an HTML comment")
    if ZERO_WIDTH.search(raw):
        signals.append("zero-width characters")
    return signals


def _norm_amount(v: float) -> list[str]:
    whole = int(v) if float(v).is_integer() else None
    forms = [f"{v:.2f}", f"{v:,.2f}"]
    if whole is not None:
        forms += [str(whole), f"{whole:,}"]
    return forms


class ProvenanceStore:
    def __init__(self) -> None:
        self._spans: dict[str, list[ContextSpan]] = defaultdict(list)

    def record(self, session_id: str, trust: Trust, source: str, text: str, raw: str | None = None) -> ContextSpan:
        signals = scan_injection(raw if raw is not None else text, text) if trust == Trust.UNTRUSTED else []
        span = ContextSpan(session_id=session_id, trust=trust, source=source, text=text, injection_signals=signals)
        self._spans[session_id].append(span)
        return span

    def spans(self, session_id: str) -> list[ContextSpan]:
        return list(self._spans.get(session_id, []))

    def suspicious_spans(self, session_id: str) -> list[ContextSpan]:
        return [s for s in self.spans(session_id) if s.injection_signals]

    def origins(self, session_id: str, value: str) -> dict[Trust, list[ContextSpan]]:
        """Which spans contain this value, grouped by trust."""
        out: dict[Trust, list[ContextSpan]] = defaultdict(list)
        if not value:
            return out
        needle = value.strip().lower()
        for s in self.spans(session_id):
            if needle in s.text.lower():
                out[s.trust].append(s)
        return out

    def amount_origins(self, session_id: str, amount: float) -> dict[Trust, list[ContextSpan]]:
        out: dict[Trust, list[ContextSpan]] = defaultdict(list)
        forms = _norm_amount(amount)
        for s in self.spans(session_id):
            for f in forms:
                if re.search(r"(?<![\d.,])\$?" + re.escape(f) + r"(?![\d])", s.text):
                    out[s.trust].append(s)
                    break
        return out

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._spans.clear()
        else:
            self._spans.pop(session_id, None)


def snippet(text: str, needle: str, width: int = 90) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return text[:width]
    a = max(0, i - width // 2)
    b = min(len(text), i + len(needle) + width // 2)
    return ("…" if a else "") + text[a:b].replace("\n", " ") + ("…" if b < len(text) else "")
