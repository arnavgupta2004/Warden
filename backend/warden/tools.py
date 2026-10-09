"""The tool surface Warden exposes to agents.

Names follow PayPal's Agent Toolkit / MCP server where an equivalent exists,
so an agent already wired to PayPal's MCP server can point at Warden instead.
Each tool carries risk metadata the policy engine uses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["context", "read", "money_out", "money_in", "destructive", "ops"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: Kind
    description: str
    params: dict[str, Any]
    required: list[str] = field(default_factory=list)
    counterparty_field: str | None = None  # arg naming who receives money
    amount_field: str | None = None

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.params, "required": self.required}


S = {"type": "string"}
N = {"type": "number"}

TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        # Context (provenance-tracked)
        ToolSpec(
            "fetch_url",
            "context",
            "Fetch a web page, email or document. Content is returned as text and recorded by Warden "
            "with its provenance so later payment calls can be checked against it.",
            {"url": S},
            ["url"],
        ),
        ToolSpec(
            "warden_check_approval",
            "context",
            "Check whether a payment that was held for human approval has been approved, and run it if so.",
            {"approval_id": S},
            ["approval_id"],
        ),
        # Read-only PayPal
        ToolSpec("get_order", "read", "Get a PayPal order by id.", {"order_id": S}, ["order_id"]),
        ToolSpec("get_capture", "read", "Get a captured payment by capture id.", {"capture_id": S}, ["capture_id"]),
        ToolSpec("get_refund", "read", "Get a refund by id.", {"refund_id": S}, ["refund_id"]),
        ToolSpec(
            "list_transactions",
            "read",
            "List recent transactions on the merchant account.",
            {"days": N},
        ),
        ToolSpec("list_invoices", "read", "List invoices.", {}),
        ToolSpec("get_invoice", "read", "Get an invoice by id.", {"invoice_id": S}, ["invoice_id"]),
        ToolSpec("list_disputes", "read", "List open disputes.", {}),
        ToolSpec("get_dispute", "read", "Get a dispute by id.", {"dispute_id": S}, ["dispute_id"]),
        ToolSpec("get_payout", "read", "Get a payout batch by id.", {"payout_batch_id": S}, ["payout_batch_id"]),
        # Money out
        ToolSpec(
            "create_refund",
            "money_out",
            "Refund a captured payment, fully or partially. Money returns to the original payer.",
            {"capture_id": S, "amount": N, "currency": S, "note": S},
            ["capture_id"],
            amount_field="amount",
        ),
        ToolSpec(
            "create_payout",
            "money_out",
            "Send money to a PayPal account by email (PayPal Payouts). Used to pay contractors and suppliers.",
            {"receiver_email": S, "amount": N, "currency": S, "note": S},
            ["receiver_email", "amount"],
            counterparty_field="receiver_email",
            amount_field="amount",
        ),
        ToolSpec(
            "accept_dispute_claim",
            "money_out",
            "Accept a buyer's dispute claim, refunding the buyer.",
            {"dispute_id": S, "note": S},
            ["dispute_id"],
        ),
        # Money in
        ToolSpec(
            "create_invoice",
            "money_in",
            "Create and send an invoice to a customer.",
            {"recipient_email": S, "amount": N, "currency": S, "description": S},
            ["recipient_email", "amount", "description"],
            counterparty_field="recipient_email",
            amount_field="amount",
        ),
        ToolSpec(
            "send_invoice_reminder",
            "money_in",
            "Send a payment reminder for an unpaid invoice.",
            {"invoice_id": S, "note": S},
            ["invoice_id"],
        ),
        # Destructive
        ToolSpec(
            "cancel_sent_invoice",
            "destructive",
            "Cancel an invoice that was already sent.",
            {"invoice_id": S, "note": S},
            ["invoice_id"],
        ),
        # Ops
        ToolSpec(
            "provide_dispute_evidence",
            "ops",
            "Submit evidence (e.g. tracking details) to answer a dispute.",
            {"dispute_id": S, "notes": S, "tracking_number": S, "carrier": S},
            ["dispute_id", "notes"],
        ),
        ToolSpec(
            "create_shipment_tracking",
            "ops",
            "Attach shipment tracking to a captured payment.",
            {"capture_id": S, "tracking_number": S, "carrier": S},
            ["capture_id", "tracking_number", "carrier"],
        ),
    ]
}

ALWAYS_ALLOWED_KINDS = {"context", "read"}

# Capability groups the mandate compiler maps natural language onto.
CAPABILITIES: dict[str, list[str]] = {
    "refunds": ["create_refund"],
    "payouts": ["create_payout"],
    "invoicing": ["create_invoice", "send_invoice_reminder", "cancel_sent_invoice"],
    "disputes": ["provide_dispute_evidence", "accept_dispute_claim"],
    "shipping": ["create_shipment_tracking"],
}


def spec(name: str) -> ToolSpec | None:
    return TOOLS.get(name)
