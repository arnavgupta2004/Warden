"""Real PayPal REST client (sandbox or live), using the v2 Orders/Payments,
v1 Payouts, v2 Invoicing, v1 Disputes and v1 Reporting APIs."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .errors import PayPalError


# Carrier codes valid in both the Disputes v1 and Orders v2 carrier enums.
# Anything else is sent as OTHER plus carrier_name_other, which both APIs
# require for carriers outside the enum.
KNOWN_CARRIERS = {"UPS", "USPS", "FEDEX", "DHL", "ONTRAC", "ROYAL_MAIL", "AMAZON", "DPD", "GLS", "TNT", "PUROLATOR"}


def _carrier(name: str, key: str) -> dict[str, str]:
    """{key: ENUM} for a known carrier, else {key: "OTHER", "carrier_name_other": name}."""
    name = (name or "").strip()
    code = name.upper().replace(" ", "_")
    if code in KNOWN_CARRIERS:
        return {key: code}
    return {key: "OTHER", "carrier_name_other": name if code not in ("", "OTHER") else "Other carrier"}


def _refunded_from_order(order: dict[str, Any], capture_id: str) -> float:
    total = 0.0
    for pu in order.get("purchase_units", []):
        payments = pu.get("payments", {})
        caps = payments.get("captures", [])
        for r in payments.get("refunds", []):
            if r.get("status") not in ("COMPLETED", "PENDING"):
                continue
            up = [l.get("href", "") for l in r.get("links", []) if l.get("rel") == "up"]
            if (up and up[0].rstrip("/").endswith(capture_id)) or (not up and len(caps) == 1 and caps[0].get("id") == capture_id):
                total += float(r["amount"]["value"])
    return round(total, 2)


class PayPalSandbox:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base = base_url.rstrip("/")
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.AsyncClient(timeout=30)
        # Dispute ids Warden already knows (e.g. from the demo seed). PayPal's dispute list can miss a
        # new dispute for a few minutes, so the open-dispute check also looks these up directly.
        self.known_disputes: set[str] = set()

    async def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await self._http.post(
            f"{self.base}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
        )
        if r.status_code != 200:
            raise PayPalError(r.status_code, "AUTHENTICATION_FAILURE", r.text[:300])
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = time.time() + int(j.get("expires_in", 3000))
        return self._token

    async def _req(self, method: str, path: str, body: Any = None, params: dict | None = None,
                   idempotent: bool = False, files: Any = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._auth()}", "Prefer": "return=representation"}
        if idempotent:
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        kw: dict[str, Any] = {"headers": headers, "params": params}
        if files is not None:
            kw["files"] = files
        elif body is not None:
            kw["json"] = body
        r = await self._http.request(method, f"{self.base}{path}", **kw)
        if r.status_code >= 400:
            try:
                j = r.json()
                raise PayPalError(r.status_code, j.get("name", "ERROR"), j.get("message", r.text[:300]), j.get("debug_id"))
            except ValueError:
                raise PayPalError(r.status_code, "ERROR", r.text[:300])
        if not r.content:
            return {"status_code": r.status_code}
        return r.json()

    # -- facts for the policy engine -------------------------------------
    async def capture_facts(self, capture_id: str) -> dict[str, Any] | None:
        try:
            c = await self._req("GET", f"/v2/payments/captures/{capture_id}")
        except PayPalError as e:
            if e.status == 404:
                return None
            raise
        amt = float(c["amount"]["value"])
        # A capture does not report how much of it was refunded; the order does,
        # under purchase_units[].payments.refunds[] (each linking "up" to its capture).
        refunded = 0.0
        payer_email = ""
        order_id = c.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
        if order_id:
            try:
                o = await self._req("GET", f"/v2/checkout/orders/{order_id}")
                payer_email = o.get("payer", {}).get("email_address", "")
                if not payer_email and (card := o.get("payment_source", {}).get("card")):
                    payer_email = f"card ending {card.get('last_digits', '')}".strip()
                refunded = _refunded_from_order(o, capture_id)
            except PayPalError:
                pass
        status = c.get("status")
        refundable = 0.0 if status == "REFUNDED" else max(0.0, round(amt - refunded, 2))
        return {
            "capture_id": capture_id,
            "amount": amt,
            "currency": c["amount"]["currency_code"],
            "refunded": refunded,
            "refundable": refundable,
            "payer_email": payer_email,
            "status": status,
            "open_dispute": await self._has_open_dispute(capture_id),
        }

    async def _has_open_dispute(self, capture_id: str) -> bool:
        open_ = lambda d: d.get("status") not in ("RESOLVED", "OTHER")
        try:
            r = await self._req("GET", "/v1/customer/disputes", params={"disputed_transaction_id": capture_id})
            if any(open_(d) for d in r.get("items", [])):
                return True
        except PayPalError:
            pass  # app lacks the Disputes feature; the known disputes below may still answer
        for did in sorted(self.known_disputes):
            try:
                d = await self._req("GET", f"/v1/customer/disputes/{did}")
            except PayPalError:
                continue
            txs = d.get("disputed_transactions") or []
            if open_(d) and any(t.get("seller_transaction_id") == capture_id for t in txs):
                return True
        return False

    async def dispute_facts(self, dispute_id: str) -> dict[str, Any] | None:
        try:
            d = await self._req("GET", f"/v1/customer/disputes/{dispute_id}")
        except PayPalError as e:
            if e.status == 404:
                return None
            raise
        tx = (d.get("disputed_transactions") or [{}])[0]
        buyer = tx.get("buyer", {}).get("email", "") or tx.get("buyer", {}).get("name", "")
        return {"dispute_id": dispute_id, "amount": float(d["dispute_amount"]["value"]), "status": d.get("status"), "buyer_email": buyer}

    async def create_card_capture(self, amount: str, description: str, reference_id: str,
                                  card_number: str = "4012888888881881") -> tuple[str, str]:
        """Create and capture an order paid with a PayPal sandbox test card (no buyer login).
        Used by Reset demo to give the refund scenarios fresh, refundable captures.
        Returns (order_id, capture_id). Sandbox only: raw card data needs PCI SAQ D in live."""
        if "sandbox" not in self.base:
            raise PayPalError(400, "SANDBOX_ONLY", "Test-card captures are only for the PayPal sandbox.")
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference_id, "description": description,
                                "amount": {"currency_code": "USD", "value": amount}}],
            "payment_source": {"card": {
                "number": card_number, "expiry": f"{datetime.now().year + 3}-12", "security_code": "123",
                "name": "Kestrel Demo Buyer",
                "billing_address": {"address_line_1": "1 Demo Way", "admin_area_2": "San Jose", "admin_area_1": "CA",
                                    "postal_code": "95131", "country_code": "US"}}},
        }
        o = await self._req("POST", "/v2/checkout/orders", body, idempotent=True)
        if o.get("status") != "COMPLETED":  # card orders usually capture on create; capture if not
            o = await self._req("POST", f"/v2/checkout/orders/{o['id']}/capture", idempotent=True)
        cap = o["purchase_units"][0]["payments"]["captures"][0]
        if cap.get("status") not in ("COMPLETED", "PENDING"):
            raise PayPalError(422, "CAPTURE_NOT_COMPLETED", f"Test-card capture is {cap.get('status')}.")
        return o["id"], cap["id"]

    # -- tools ----------------------------------------------------------
    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, f"t_{tool}", None)
        if fn is None:
            raise PayPalError(400, "UNSUPPORTED_TOOL", f"{tool} is not supported")
        return await fn(**args)

    async def t_get_order(self, order_id: str):
        return await self._req("GET", f"/v2/checkout/orders/{order_id}")

    async def t_get_capture(self, capture_id: str):
        return await self._req("GET", f"/v2/payments/captures/{capture_id}")

    async def t_get_refund(self, refund_id: str):
        return await self._req("GET", f"/v2/payments/refunds/{refund_id}")

    async def t_list_transactions(self, days: float = 30):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=min(float(days or 30), 31))
        fmt = "%Y-%m-%dT%H:%M:%S-0000"
        return await self._req("GET", "/v1/reporting/transactions", params={
            "start_date": start.strftime(fmt), "end_date": end.strftime(fmt), "fields": "transaction_info,payer_info,cart_info", "page_size": 50})

    async def t_list_invoices(self):
        return await self._req("GET", "/v2/invoicing/invoices", params={"page_size": 20, "total_required": "true"})

    async def t_get_invoice(self, invoice_id: str):
        return await self._req("GET", f"/v2/invoicing/invoices/{invoice_id}")

    async def t_list_disputes(self):
        return await self._req("GET", "/v1/customer/disputes")

    async def t_get_dispute(self, dispute_id: str):
        return await self._req("GET", f"/v1/customer/disputes/{dispute_id}")

    async def t_get_payout(self, payout_batch_id: str):
        return await self._req("GET", f"/v1/payments/payouts/{payout_batch_id}")

    async def t_create_refund(self, capture_id: str, amount: float | None = None, currency: str = "USD", note: str = ""):
        body: dict[str, Any] = {}
        if amount is not None:
            body["amount"] = {"value": f"{float(amount):.2f}", "currency_code": currency}
        if note:
            body["note_to_payer"] = note[:255]
        return await self._req("POST", f"/v2/payments/captures/{capture_id}/refund", body, idempotent=True)

    async def t_create_payout(self, receiver_email: str, amount: float, currency: str = "USD", note: str = ""):
        batch_id = f"warden-{uuid.uuid4().hex[:16]}"
        body = {
            "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "You have a payout", "email_message": note[:255] or "Payment"},
            "items": [{
                "recipient_type": "EMAIL",
                "amount": {"value": f"{float(amount):.2f}", "currency": currency},
                "receiver": receiver_email,
                "note": note[:4000],
                "sender_item_id": batch_id,
            }],
        }
        return await self._req("POST", "/v1/payments/payouts", body)

    async def t_accept_dispute_claim(self, dispute_id: str, note: str = ""):
        return await self._req("POST", f"/v1/customer/disputes/{dispute_id}/accept-claim", {"note": note or "Accepted by merchant."})

    async def t_create_invoice(self, recipient_email: str, amount: float, description: str, currency: str = "USD"):
        draft = await self._req("POST", "/v2/invoicing/invoices", {
            "detail": {"currency_code": currency, "note": description[:4000]},
            "primary_recipients": [{"billing_info": {"email_address": recipient_email}}],
            "items": [{"name": description[:200], "quantity": "1", "unit_amount": {"currency_code": currency, "value": f"{float(amount):.2f}"}}],
        })
        iid = draft.get("id") or draft.get("href", "").rstrip("/").split("/")[-1]
        await self._req("POST", f"/v2/invoicing/invoices/{iid}/send", {"send_to_invoicer": False})
        return await self._req("GET", f"/v2/invoicing/invoices/{iid}")

    async def t_send_invoice_reminder(self, invoice_id: str, note: str = ""):
        return await self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", {"note": note or "Friendly reminder"})

    async def t_cancel_sent_invoice(self, invoice_id: str, note: str = ""):
        return await self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel", {"note": note or "Cancelled", "send_to_recipient": True})

    async def t_provide_dispute_evidence(self, dispute_id: str, notes: str, tracking_number: str = "", carrier: str = ""):
        evidence: dict[str, Any] = {"notes": notes[:2000]}
        if tracking_number:
            tracking = {"tracking_number": tracking_number, **_carrier(carrier, "carrier_name")}
            evidence["evidence_type"] = "PROOF_OF_FULFILLMENT"
            evidence["evidence_info"] = {"tracking_info": [tracking]}
        payload = {"evidences": [evidence]}
        # PayPal takes the seller's evidence once per request. If it already has it and isn't asking
        # for more, say so plainly instead of sending again (which PayPal would refuse).
        d = await self._req("GET", f"/v1/customer/disputes/{dispute_id}")
        can_send = any(l.get("rel") == "provide_evidence" for l in d.get("links", []))
        sent = [e for e in d.get("evidences", []) if e.get("source") == "SUBMITTED_BY_SELLER"]
        if sent and not can_send:
            return {"already_answered": True, "dispute_id": dispute_id, "status": d.get("status"),
                    "message": "PayPal already has this shop's evidence for this dispute, so nothing new was sent.",
                    "seller_evidence": sent}
        return await self._req("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                               files={"input": (None, json.dumps(payload), "application/json")})

    async def t_create_shipment_tracking(self, capture_id: str, tracking_number: str, carrier: str):
        # Orders v2 tracking (replaces the deprecated /v1/shipping/trackers-batch).
        c = await self._req("GET", f"/v2/payments/captures/{capture_id}")
        order_id = c.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
        if not order_id:
            raise PayPalError(422, "ORDER_NOT_FOUND", f"Capture {capture_id} has no related order to add tracking to.")
        return await self._req("POST", f"/v2/checkout/orders/{order_id}/track", {
            "capture_id": capture_id, "tracking_number": tracking_number, **_carrier(carrier, "carrier"),
            "notify_payer": False}, idempotent=True)

    async def verify_webhook(self, headers: dict[str, str], event: dict[str, Any], webhook_id: str) -> bool:
        body = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        if not all(body[k] for k in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time")):
            return False  # unsigned: not from PayPal
        try:
            r = await self._req("POST", "/v1/notifications/verify-webhook-signature", body)
        except PayPalError:
            return False  # PayPal rejected the signature data
        return r.get("verification_status") == "SUCCESS"
