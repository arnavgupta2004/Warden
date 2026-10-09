"""Offline PayPal simulator.

Returns the same response shapes as the PayPal REST API for the endpoints
Warden uses, seeded with a small fictional merchant ("Kestrel & Co.").
Lets the project run, be tested, and be judged without credentials. Set
PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET to use the real PayPal sandbox.
"""
from __future__ import annotations

import copy
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import PayPalError


def _pid(seed: str, n: int = 17) -> str:
    h = hashlib.sha1(seed.encode()).hexdigest().upper()
    alphabet = "0123456789ABCDEFGHJKLMNPRSTUVWXY"
    return "".join(alphabet[int(h[i : i + 2], 16) % len(alphabet)] for i in range(0, n * 2, 2))[:n]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _money(v: float, cur: str = "USD") -> dict[str, str]:
    return {"currency_code": cur, "value": f"{v:.2f}"}


NOW = datetime.now(timezone.utc)

SEED_CAPTURES = [
    # capture_id, order_id, payer email, payer name, amount, item, days ago
    ("4RT88014HN112233K", "7XK21934UB556120D", "jamie.rivera@example.com", "Jamie Rivera", 42.00, "Stoneware mug set (4)", 3),
    ("9LM20577QW341870P", "2HB90321KE118845M", "priya.nair@example.com", "Priya Nair", 180.00, "Walnut desk lamp", 5),
    ("1VC66309TR775024A", "5NF44208GX903317J", "sam.okafor@example.com", "Sam Okafor", 220.00, "Linen throw + cushions", 2),
    ("6DW31852LP480916E", "8QT75510MC227094R", "lee.tanaka@example.com", "Lee Tanaka", 64.00, "Ceramic planter", 12),
]


class Simulator:
    mode = "simulator"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.captures: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        for cap, order, email, name, amt, item, ago in SEED_CAPTURES:
            t = _iso(NOW - timedelta(days=ago))
            self.captures[cap] = {
                "id": cap,
                "status": "COMPLETED",
                "amount": _money(amt),
                "final_capture": True,
                "create_time": t,
                "supplementary_data": {"related_ids": {"order_id": order}},
                "_refunded": 0.0,
                "_payer_email": email,
                "_item": item,
            }
            self.orders[order] = {
                "id": order,
                "status": "COMPLETED",
                "intent": "CAPTURE",
                "create_time": t,
                "payer": {"email_address": email, "name": {"given_name": name.split()[0], "surname": name.split()[-1]}},
                "purchase_units": [
                    {
                        "reference_id": "default",
                        "description": item,
                        "amount": _money(amt),
                        "payments": {"captures": [{"id": cap, "status": "COMPLETED", "amount": _money(amt)}]},
                    }
                ],
            }
        self.refunds: dict[str, dict[str, Any]] = {}
        self.payouts: dict[str, dict[str, Any]] = {}
        self.invoices: dict[str, dict[str, Any]] = {
            "INV2-KST7-4Q2M-8ZP3-HC41": self._invoice("INV2-KST7-4Q2M-8ZP3-HC41", "0041", "billing@harborline-studio.example", 1250.0, "Wholesale order: 50 mug sets", "SENT", 9),
            "INV2-KST7-9R5D-2LW8-JB66": self._invoice("INV2-KST7-9R5D-2LW8-JB66", "0042", "accounts@finchandfern.example", 480.0, "Custom planters (12)", "PAID", 20),
        }
        self.disputes: dict[str, dict[str, Any]] = {
            "PP-D-55210": {
                "dispute_id": "PP-D-55210",
                "create_time": _iso(NOW - timedelta(days=4)),
                "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                "status": "WAITING_FOR_SELLER_RESPONSE",
                "dispute_amount": _money(64.00),
                "disputed_transactions": [{"seller_transaction_id": "6DW31852LP480916E", "buyer": {"email": "lee.tanaka@example.com"}}],
                "seller_response_due_date": _iso(NOW + timedelta(days=6)),
            }
        }
        self.trackers: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _invoice(self, iid: str, num: str, email: str, amt: float, desc: str, status: str, ago: int) -> dict[str, Any]:
        return {
            "id": iid,
            "status": status,
            "detail": {"invoice_number": num, "currency_code": "USD", "invoice_date": _iso(NOW - timedelta(days=ago))[:10], "note": desc},
            "primary_recipients": [{"billing_info": {"email_address": email}}],
            "items": [{"name": desc, "quantity": "1", "unit_amount": _money(amt)}],
            "amount": _money(amt),
        }

    # -- helpers used by the policy engine --------------------------------
    def _public(self, d: dict[str, Any]) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in d.items() if not k.startswith("_")}

    async def capture_facts(self, capture_id: str) -> dict[str, Any] | None:
        c = self.captures.get(capture_id)
        if not c:
            return None
        amt = float(c["amount"]["value"])
        return {
            "capture_id": capture_id,
            "amount": amt,
            "currency": c["amount"]["currency_code"],
            "refunded": c["_refunded"],
            "refundable": round(amt - c["_refunded"], 2),
            "payer_email": c["_payer_email"],
            "status": c["status"],
            "open_dispute": any(
                t.get("seller_transaction_id") == capture_id
                for d in self.disputes.values()
                if d["status"].startswith("WAITING") or d["status"] == "OPEN"
                for t in d["disputed_transactions"]
            ),
        }

    async def dispute_facts(self, dispute_id: str) -> dict[str, Any] | None:
        d = self.disputes.get(dispute_id)
        if not d:
            return None
        return {"dispute_id": dispute_id, "amount": float(d["dispute_amount"]["value"]), "status": d["status"],
                "buyer_email": d["disputed_transactions"][0]["buyer"]["email"]}

    # -- tool implementations -------------------------------------------
    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, copy.deepcopy(args)))
        fn = getattr(self, f"t_{tool}", None)
        if fn is None:
            raise PayPalError(400, "UNSUPPORTED_TOOL", f"{tool} is not supported")
        return await fn(**args)

    async def t_get_order(self, order_id: str) -> dict[str, Any]:
        o = self.orders.get(order_id)
        if not o:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Order {order_id} not found")
        return copy.deepcopy(o)

    async def t_get_capture(self, capture_id: str) -> dict[str, Any]:
        c = self.captures.get(capture_id)
        if not c:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Capture {capture_id} not found")
        return self._public(c)

    async def t_get_refund(self, refund_id: str) -> dict[str, Any]:
        r = self.refunds.get(refund_id)
        if not r:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Refund {refund_id} not found")
        return copy.deepcopy(r)

    async def t_list_transactions(self, days: float = 30) -> dict[str, Any]:
        rows = []
        for c in self.captures.values():
            rows.append({
                "transaction_info": {
                    "transaction_id": c["id"],
                    "transaction_event_code": "T0006",
                    "transaction_initiation_date": c["create_time"],
                    "transaction_amount": {"currency_code": "USD", "value": c["amount"]["value"]},
                    "transaction_status": "S",
                },
                "payer_info": {"email_address": c["_payer_email"]},
                "cart_info": {"item_details": [{"item_name": c["_item"]}]},
            })
        return {"transaction_details": rows, "total_items": len(rows)}

    async def t_list_invoices(self) -> dict[str, Any]:
        return {"items": [copy.deepcopy(i) for i in self.invoices.values()], "total_items": len(self.invoices)}

    async def t_get_invoice(self, invoice_id: str) -> dict[str, Any]:
        i = self.invoices.get(invoice_id)
        if not i:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Invoice {invoice_id} not found")
        return copy.deepcopy(i)

    async def t_list_disputes(self) -> dict[str, Any]:
        return {"items": [copy.deepcopy(d) for d in self.disputes.values()]}

    async def t_get_dispute(self, dispute_id: str) -> dict[str, Any]:
        d = self.disputes.get(dispute_id)
        if not d:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Dispute {dispute_id} not found")
        return copy.deepcopy(d)

    async def t_get_payout(self, payout_batch_id: str) -> dict[str, Any]:
        p = self.payouts.get(payout_batch_id)
        if not p:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Payout {payout_batch_id} not found")
        return copy.deepcopy(p)

    async def t_create_refund(self, capture_id: str, amount: float | None = None, currency: str = "USD", note: str = "") -> dict[str, Any]:
        c = self.captures.get(capture_id)
        if not c:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Capture {capture_id} not found")
        total = float(c["amount"]["value"])
        amt = round(float(amount) if amount is not None else total - c["_refunded"], 2)
        if amt <= 0 or c["_refunded"] + amt > total + 1e-9:
            raise PayPalError(422, "REFUND_AMOUNT_EXCEEDED", "The refund amount must be less than or equal to the capture amount that has not yet been refunded.")
        c["_refunded"] = round(c["_refunded"] + amt, 2)
        c["status"] = "REFUNDED" if abs(c["_refunded"] - total) < 1e-9 else "PARTIALLY_REFUNDED"
        rid = _pid(f"ref{capture_id}{len(self.refunds)}{time.time()}")
        r = {
            "id": rid,
            "status": "COMPLETED",
            "amount": _money(amt, currency),
            "note_to_payer": note,
            "create_time": _iso(datetime.now(timezone.utc)),
            "links": [{"href": f"https://api-m.sandbox.paypal.com/v2/payments/refunds/{rid}", "rel": "self", "method": "GET"}],
        }
        self.refunds[rid] = r
        return copy.deepcopy(r)

    async def t_create_payout(self, receiver_email: str, amount: float, currency: str = "USD", note: str = "") -> dict[str, Any]:
        bid = _pid(f"payout{receiver_email}{len(self.payouts)}{time.time()}", 13)
        item_id = _pid(f"item{bid}", 13)
        batch = {
            "batch_header": {
                "payout_batch_id": bid,
                "batch_status": "SUCCESS",
                "sender_batch_header": {"sender_batch_id": f"warden-{bid.lower()}", "email_subject": "You have a payout from Kestrel & Co."},
                "amount": {"currency": currency, "value": f"{float(amount):.2f}"},
            },
            "items": [{
                "payout_item_id": item_id,
                "transaction_status": "SUCCESS",
                "payout_item": {"recipient_type": "EMAIL", "receiver": receiver_email, "amount": {"currency": currency, "value": f"{float(amount):.2f}"}, "note": note},
            }],
        }
        self.payouts[bid] = batch
        return copy.deepcopy(batch)

    async def t_accept_dispute_claim(self, dispute_id: str, note: str = "") -> dict[str, Any]:
        d = self.disputes.get(dispute_id)
        if not d:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Dispute {dispute_id} not found")
        d["status"] = "RESOLVED"
        return {"links": [{"href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}", "rel": "self"}]}

    async def t_create_invoice(self, recipient_email: str, amount: float, description: str, currency: str = "USD") -> dict[str, Any]:
        iid = "INV2-" + "-".join(_pid(f"inv{recipient_email}{time.time()}", 16)[i : i + 4] for i in range(0, 16, 4))
        inv = self._invoice(iid, f"{43 + len(self.invoices):04d}", recipient_email, float(amount), description, "SENT", 0)
        inv["detail"]["currency_code"] = currency
        self.invoices[iid] = inv
        return copy.deepcopy(inv)

    async def t_send_invoice_reminder(self, invoice_id: str, note: str = "") -> dict[str, Any]:
        if invoice_id not in self.invoices:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Invoice {invoice_id} not found")
        return {"status": "REMINDER_SENT", "invoice_id": invoice_id}

    async def t_cancel_sent_invoice(self, invoice_id: str, note: str = "") -> dict[str, Any]:
        i = self.invoices.get(invoice_id)
        if not i:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Invoice {invoice_id} not found")
        i["status"] = "CANCELLED"
        return {"status": "CANCELLED", "invoice_id": invoice_id}

    async def t_provide_dispute_evidence(self, dispute_id: str, notes: str, tracking_number: str = "", carrier: str = "") -> dict[str, Any]:
        d = self.disputes.get(dispute_id)
        if not d:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"Dispute {dispute_id} not found")
        d["status"] = "UNDER_REVIEW"
        return {"links": [{"href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}", "rel": "self"}]}

    async def t_create_shipment_tracking(self, capture_id: str, tracking_number: str, carrier: str) -> dict[str, Any]:
        self.trackers[capture_id] = {"transaction_id": capture_id, "tracking_number": tracking_number, "carrier": carrier, "status": "SHIPPED"}
        return {"tracker_identifiers": [{"transaction_id": capture_id, "tracking_number": tracking_number}], "errors": []}
