"""Mandate compiler: natural language -> structured, signed spending policy.

Claude does the compiling when an API key is set. A deterministic parser
handles the common phrasings so Warden works offline and so the LLM output
can be cross-checked: the stricter of the two wins on every numeric limit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from . import llm
from .config import settings
from .models import Mandate, Payee
from .tools import CAPABILITIES

MONEY = r"\$?\s?([\d,]+(?:\.\d{1,2})?)"


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_rules(text: str) -> dict[str, Any]:
    """Best-effort deterministic parse of a mandate written in plain English."""
    t = " ".join(text.split())
    low = t.lower()
    out: dict[str, Any] = {"source_text": text}

    # Budget: "under $2,000 a week", "budget of $500 per month", "max $80 weekly"
    m = re.search(
        r"(?:under|below|max(?:imum)?|budget(?: of)?|up to|no more than|limit(?: of)?)\s*"
        + MONEY
        + r"\s*(?:a|per|each|/|every)?\s*(day|week|month)",
        low,
    ) or re.search(MONEY + r"\s*(daily|weekly|monthly)", low)
    if m:
        out["budget_total"] = _num(m.group(1))
        w = m.group(2)
        out["budget_window"] = {"daily": "day", "weekly": "week", "monthly": "month"}.get(w, w)
    else:
        m = re.search(r"(?:total|overall|budget)[^.$]*?" + MONEY, low)
        if m:
            out["budget_total"] = _num(m.group(1))
            out["budget_window"] = "total"

    # Refund autonomy: "refund ... up to $50 on your own"
    m = re.search(r"refund[^.]*?up to\s*" + MONEY, low)
    if m:
        out["refund_auto_max"] = _num(m.group(1))

    # Approval threshold: "ask me above $100", "anything over $100 needs my approval"
    m = re.search(r"(?:ask me|check with me|need[s]? my (?:ok|approval))[^.$]*?(?:above|over|more than)\s*" + MONEY, low) or re.search(
        r"(?:above|over|more than)\s*" + MONEY + r"[^.]*?(?:ask me|check with me|my (?:ok|approval))", low
    )
    if m:
        out["auto_approve_below"] = _num(m.group(1))

    # Hard per-transaction cap: "no single payment over $500"
    m = re.search(r"(?:no single|never (?:send|pay|spend) more than|single payment)[^.$]*?" + MONEY, low)
    if m:
        out["per_tx_max"] = _num(m.group(1))

    # Trusted payees: "Maya Chen (maya@x.com)" optionally followed by "up to $400 each"
    payees: list[dict[str, Any]] = []
    for pm in re.finditer(r"([A-Z][\w'.-]+(?:\s[A-Z][\w'.-]+)*)\s*\(\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)\s*\)", t):
        payees.append({"name": pm.group(1).strip(), "email": pm.group(2).lower()})
    if payees:
        each = re.search(r"up to\s*" + MONEY + r"\s*each", t[t.find("(") :].lower())
        if each:
            for p in payees:
                p["max_per_payment"] = _num(each.group(1))
    out["trusted_payees"] = payees

    # Capabilities
    tools: list[str] = []
    if re.search(r"\brefund", low):
        tools += CAPABILITIES["refunds"]
    if re.search(r"\b(pay|payout|payables|contractor|supplier|vendor)", low):
        tools += CAPABILITIES["payouts"]
    if re.search(r"\binvoic", low):
        tools += CAPABILITIES["invoicing"]
    if re.search(r"\bdispute", low):
        tools += CAPABILITIES["disputes"]
    if re.search(r"\b(ship|tracking)", low):
        tools += CAPABILITIES["shipping"]
    if re.search(r"\bsupport\b", low):
        tools += CAPABILITIES["refunds"] + CAPABILITIES["shipping"]
    out["allowed_tools"] = sorted(set(tools))

    blocked = re.findall(r"never (?:buy|pay for|purchase)\s+([a-z ,]+?)(?:[.;]|$)", low)
    cats: list[str] = []
    for b in blocked:
        cats += [c.strip() for c in re.split(r",|\bor\b|\band\b", b) if c.strip()]
    out["blocked_categories"] = cats

    out["new_payee_requires_approval"] = True
    first = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    out["purpose"] = first[:200]
    return out


LLM_SYSTEM = """You compile a business owner's plain-English instructions for an AI agent into a strict
spending mandate for PayPal operations. Be conservative: when the text is ambiguous, choose the
stricter limit. Never invent payees. Output JSON with keys:
purpose (string), currency (ISO code), budget_total (number), budget_window ("day"|"week"|"month"|"total"),
per_tx_max (number, 0 if unstated), auto_approve_below (number: money moves at or above this need human approval, 0 if unstated),
refund_auto_max (number or null), capabilities (subset of ["refunds","payouts","invoicing","disputes","shipping"]),
trusted_payees (list of {name, email, max_per_payment|null}), blocked_categories (list of strings),
new_payee_requires_approval (boolean), max_moves_per_hour (integer)."""


def _stricter(a: float | None, b: float | None) -> float | None:
    vals = [v for v in (a, b) if v not in (None, 0)]
    return min(vals) if vals else (a if a is not None else b)


async def compile_mandate(text: str, agent_id: str = "backoffice-agent") -> Mandate:
    rules = parse_rules(text)
    data: dict[str, Any] = dict(rules)
    compiled_by = "rules"

    if llm.enabled():
        j = await llm.complete_json(LLM_SYSTEM, text)
        if isinstance(j, dict):
            compiled_by = "llm+rules"
            caps = [c for c in j.get("capabilities", []) if c in CAPABILITIES]
            llm_tools = sorted({t for c in caps for t in CAPABILITIES[c]})
            # Union of capabilities the owner mentioned; limits take the stricter value.
            data["allowed_tools"] = sorted(set(rules["allowed_tools"]) | set(llm_tools))
            for k in ("budget_total", "per_tx_max", "auto_approve_below", "refund_auto_max"):
                data[k] = _stricter(rules.get(k), j.get(k))
            data["budget_window"] = rules.get("budget_window") or j.get("budget_window") or "week"
            data["purpose"] = j.get("purpose") or rules["purpose"]
            data["currency"] = j.get("currency") or "USD"
            data["blocked_categories"] = sorted(set(rules["blocked_categories"]) | set(j.get("blocked_categories") or []))
            data["max_moves_per_hour"] = int(j.get("max_moves_per_hour") or 20)
            # Payees: only accept emails that literally appear in the owner's text.
            src = text.lower()
            seen = {p["email"] for p in rules["trusted_payees"]}
            for p in j.get("trusted_payees") or []:
                e = (p.get("email") or "").lower()
                if e and e in src and e not in seen:
                    data["trusted_payees"].append({"name": p.get("name", e), "email": e, "max_per_payment": p.get("max_per_payment")})
                    seen.add(e)

    data = {k: v for k, v in data.items() if v is not None}
    m = Mandate(agent_id=agent_id, compiled_by=compiled_by, **data)
    if not m.per_tx_max:
        m.per_tx_max = m.budget_total  # no single move can exceed the whole budget
    m.trusted_payees = [Payee(**p) if isinstance(p, dict) else p for p in m.trusted_payees]
    return sign(m)


def _canonical(m: Mandate) -> bytes:
    d = m.model_dump(exclude={"signature"})
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str).encode()


def sign(m: Mandate) -> Mandate:
    m.signature = hmac.new(settings.signing_key.encode(), _canonical(m), hashlib.sha256).hexdigest()
    return m


def verify(m: Mandate) -> bool:
    expected = hmac.new(settings.signing_key.encode(), _canonical(m), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, m.signature or "")
