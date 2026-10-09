"""Core data models for Warden."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Trust(str, Enum):
    """Where a piece of context came from."""

    PRINCIPAL = "principal"  # the human owner (mandate, direct instructions)
    VERIFIED = "verified"  # data returned by PayPal APIs or the trusted registry
    UNTRUSTED = "untrusted"  # web pages, inbound email, documents, other agents


class Verdict(str, Enum):
    ALLOW = "allow"
    STEP_UP = "step_up"  # needs a human approval
    DENY = "deny"


class Payee(BaseModel):
    name: str
    email: str
    max_per_payment: float | None = None


class Mandate(BaseModel):
    """A signed, structured spending policy compiled from natural language."""

    id: str = Field(default_factory=lambda: new_id("mdt"))
    principal: str = "owner"
    agent_id: str = "backoffice-agent"
    source_text: str = ""
    purpose: str = ""
    currency: str = "USD"
    budget_total: float = 0.0
    budget_window: Literal["day", "week", "month", "total"] = "week"
    per_tx_max: float = 0.0
    auto_approve_below: float = 0.0  # money moves >= this need a human
    refund_auto_max: float | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    trusted_payees: list[Payee] = Field(default_factory=list)
    blocked_categories: list[str] = Field(default_factory=list)
    new_payee_requires_approval: bool = True
    max_moves_per_hour: int = 20
    expires_at: float | None = None
    created_at: float = Field(default_factory=time.time)
    compiled_by: str = "rules"  # "llm" or "rules"
    signature: str = ""

    def payee_by_email(self, email: str) -> Payee | None:
        email = (email or "").strip().lower()
        for p in self.trusted_payees:
            if p.email.lower() == email:
                return p
        return None


class ContextSpan(BaseModel):
    """A piece of content the agent has seen, labelled with its provenance."""

    id: str = Field(default_factory=lambda: new_id("ctx"))
    session_id: str
    trust: Trust
    source: str  # url, "mandate", "paypal:get_order", "email:3", ...
    text: str
    injection_signals: list[str] = Field(default_factory=list)
    at: float = Field(default_factory=time.time)


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("call"))
    session_id: str
    agent_id: str = "backoffice-agent"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    stated_reason: str = ""  # the agent's own justification, if given
    at: float = Field(default_factory=time.time)


class Finding(BaseModel):
    rule: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    effect: Verdict = Verdict.ALLOW  # the strongest verdict this finding forces
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    call_id: str
    verdict: Verdict
    risk: int  # 0-100
    findings: list[Finding] = Field(default_factory=list)
    explanation: str = ""
    money: float = 0.0
    counterparty: str = ""


class LedgerEntry(BaseModel):
    seq: int = 0
    id: str = Field(default_factory=lambda: new_id("led"))
    at: float = Field(default_factory=time.time)
    session_id: str
    agent_id: str
    tool: str
    args: dict[str, Any]
    verdict: Verdict
    risk: int
    findings: list[Finding]
    explanation: str
    money: float
    currency: str = "USD"
    counterparty: str = ""
    status: str = "decided"  # decided | pending_approval | executed | failed | denied | settled
    approved_by: str | None = None
    paypal_ref: str | None = None
    result: dict[str, Any] | None = None
    prev_hash: str = ""
    hash: str = ""


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: new_id("apv"))
    ledger_id: str
    call: ToolCall
    decision: Decision
    status: Literal["pending", "approved", "denied", "expired"] = "pending"
    decided_by: str | None = None
    created_at: float = Field(default_factory=time.time)
    decided_at: float | None = None
