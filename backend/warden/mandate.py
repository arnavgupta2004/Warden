"""Mandate compiler: natural language -> structured, signed spending policy.

Gemini does the compiling when an API key is set. A deterministic parser
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


DEFAULT_MANDATE = (
    "Run customer support and payables for Kestrel & Co. Refund customers up to $50 on your own; anything bigger, ask me. "
    "Pay our contractors Maya Chen (maya@chenstudio.example) and Diego Alvarez (diego.alvarez@lumenfreight.example) "
    "up to $400 each. Chase unpaid invoices and answer disputes with tracking info. "
    "Never pay anyone new without asking me. No single payment over $500. Keep total spend under $1,500 a week."
)

# Public-demo ceilings (WARDEN_PUBLIC_DEMO=1): no mandate can go past these, whatever its text says.
PUBLIC_BUDGET_WEEK = 1500.0
PUBLIC_PER_TX_MAX = 500.0
PUBLIC_EXTRA_PAYEES = 2


def apply_public_demo_ceilings(data: dict[str, Any]) -> list[str]:
    """Clamp a compiled mandate (in place) to the public-demo ceilings. Returns the clamped field names."""
    caps: list[str] = []
    if data.get("budget_window", "week") != "week" or not data.get("budget_total") or data["budget_total"] > PUBLIC_BUDGET_WEEK:
        data["budget_window"] = "week"
        data["budget_total"] = min(data.get("budget_total") or PUBLIC_BUDGET_WEEK, PUBLIC_BUDGET_WEEK)
        caps.append("budget_total")
    if not data.get("per_tx_max") or data["per_tx_max"] > PUBLIC_PER_TX_MAX:
        data["per_tx_max"] = PUBLIC_PER_TX_MAX
        caps.append("per_tx_max")
    defaults = {p["email"] for p in parse_rules(DEFAULT_MANDATE)["trusted_payees"]}
    payees = [p if isinstance(p, dict) else p.model_dump() for p in data.get("trusted_payees", [])]
    extra = [p for p in payees if p["email"] not in defaults]
    if len(extra) > PUBLIC_EXTRA_PAYEES:
        keep = {p["email"] for p in extra[:PUBLIC_EXTRA_PAYEES]}
        data["trusted_payees"] = [p for p in payees if p["email"] in defaults or p["email"] in keep]
        caps.append("trusted_payees")
    return caps


LLM_SYSTEM = """You compile a business owner's plain-English instructions for an AI agent into a strict
spending mandate for PayPal operations. Be conservative: when the text is ambiguous, choose the
stricter limit. Never invent payees. Output JSON with keys:
purpose (string), currency (ISO code), budget_total (number), budget_window ("day"|"week"|"month"|"total"),
per_tx_max (number: a cap the owner states for every single payment of any kind; 0 if unstated. A limit for one
named payee is that payee's max_per_payment, not per_tx_max),
auto_approve_below (number: a threshold the owner states for ALL money moves, at or above which a human must approve;
0 if unstated. A refund-only limit is refund_auto_max, not this),
refund_auto_max (number or null: refunds above this need the owner's approval), capabilities (subset of ["refunds","payouts","invoicing","disputes","shipping"]),
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
        j = await llm.complete_json(LLM_SYSTEM, text, quick=False)  # cached by llm on the exact text
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
    if not data.get("per_tx_max"):
        data["per_tx_max"] = data.get("budget_total")  # no single move can exceed the whole budget
    caps = apply_public_demo_ceilings(data) if settings.public_demo else []
    m = Mandate(agent_id=agent_id, compiled_by=compiled_by, public_demo_caps=caps, **data)
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
