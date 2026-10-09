"""Check the real PayPal client builds correct requests (paths, auth, bodies)
against a mock transport."""
import asyncio
import json

import httpx

from warden.paypal.errors import PayPalError
from warden.paypal.sandbox import PayPalSandbox


def make():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode() if req.content else ""
        seen.append((req.method, req.url.path, dict(req.headers), body))
        p = req.url.path
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AA-test", "expires_in": 32400})
        if p.endswith("/refund"):
            return httpx.Response(201, json={"id": "REF123", "status": "COMPLETED", "amount": json.loads(body)["amount"]})
        if p == "/v1/payments/payouts":
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "BATCH1", "batch_status": "PENDING"}})
        if p == "/v2/invoicing/invoices" and req.method == "POST":
            return httpx.Response(201, json={"href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-AAAA-BBBB"})
        if p.endswith("/send"):
            return httpx.Response(202, json={})
        if p == "/v2/invoicing/invoices/INV2-AAAA-BBBB":
            return httpx.Response(200, json={"id": "INV2-AAAA-BBBB", "status": "SENT"})
        if p == "/v2/payments/captures/CAP1":
            return httpx.Response(200, json={"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "42.00"},
                                             "supplementary_data": {"related_ids": {"order_id": "ORD1"}}})
        if p == "/v2/checkout/orders/ORD1":
            return httpx.Response(200, json={"id": "ORD1", "payer": {"email_address": "buyer@sandbox.example"}})
        if p == "/v2/payments/captures/NOPE":
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "not found", "debug_id": "d1"})
        return httpx.Response(500, json={"name": "UNEXPECTED", "message": p})

    pp = PayPalSandbox("cid", "secret", "https://api-m.sandbox.paypal.com")
    pp._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return pp, seen


def test_requests_have_correct_shape():
    async def go():
        pp, seen = make()
        r = await pp.call("create_refund", {"capture_id": "CAP1", "amount": 12.5, "note": "sorry"})
        assert r["id"] == "REF123"
        m, path, headers, body = seen[-1]
        assert (m, path) == ("POST", "/v2/payments/captures/CAP1/refund")
        assert headers["authorization"] == "Bearer A21AA-test" and "paypal-request-id" in headers
        assert json.loads(body) == {"amount": {"value": "12.50", "currency_code": "USD"}, "note_to_payer": "sorry"}

        await pp.call("create_payout", {"receiver_email": "maya@chenstudio.example", "amount": 380})
        b = json.loads(seen[-1][3])
        assert b["items"][0] == {**b["items"][0], "recipient_type": "EMAIL", "receiver": "maya@chenstudio.example",
                                 "amount": {"value": "380.00", "currency": "USD"}}

        inv = await pp.call("create_invoice", {"recipient_email": "a@b.example", "amount": 10, "description": "x"})
        assert inv["status"] == "SENT"
        assert ("POST", "/v2/invoicing/invoices/INV2-AAAA-BBBB/send") in [(s[0], s[1]) for s in seen]

        f = await pp.capture_facts("CAP1")
        assert f["amount"] == 42.0 and f["payer_email"] == "buyer@sandbox.example" and f["refundable"] == 42.0
        assert await pp.capture_facts("NOPE") is None
        assert sum(1 for s in seen if s[1] == "/v1/oauth2/token") == 1  # token cached
    asyncio.run(go())


def _client(routes):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 32400})
        key = (req.method, req.url.path)
        if key in routes:
            r = routes[key]
            return r(req) if callable(r) else httpx.Response(200, json=r)
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": req.url.path})

    pp = PayPalSandbox("cid", "secret", "https://api-m.sandbox.paypal.com")
    pp._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return pp, seen


def test_capture_facts_reads_refunds_from_the_order():
    # Real captures have no total_refunded_amount; the order lists each refund with an "up" link to its capture.
    up = lambda cap: [{"rel": "up", "href": f"https://api.sandbox.paypal.com/v2/payments/captures/{cap}"}]
    routes = {
        ("GET", "/v2/payments/captures/CAP5"): {
            "id": "CAP5", "status": "PARTIALLY_REFUNDED", "amount": {"currency_code": "USD", "value": "220.00"},
            "seller_receivable_breakdown": {"gross_amount": {"currency_code": "USD", "value": "220.00"}},
            "supplementary_data": {"related_ids": {"order_id": "ORD5"}}},
        ("GET", "/v2/checkout/orders/ORD5"): {"id": "ORD5", "payer": {"email_address": "buyer@sandbox.example"}, "purchase_units": [{
            "payments": {"captures": [{"id": "CAP5"}], "refunds": [
                {"id": "R1", "status": "COMPLETED", "amount": {"value": "45.00"}, "links": up("CAP5")},
                {"id": "R2", "status": "CANCELLED", "amount": {"value": "45.00"}, "links": up("CAP5")},
                {"id": "R3", "status": "COMPLETED", "amount": {"value": "10.00"}, "links": up("OTHER")}]}}]},
        ("GET", "/v1/customer/disputes"): {"items": []},
    }

    async def go():
        pp, _ = _client(routes)
        f = await pp.capture_facts("CAP5")
        assert f["refunded"] == 45.0 and f["refundable"] == 175.0 and f["open_dispute"] is False
    asyncio.run(go())


def test_capture_facts_sees_open_dispute():
    routes = {
        ("GET", "/v2/payments/captures/CAP6"): {"id": "CAP6", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "64.00"}},
        ("GET", "/v1/customer/disputes"): lambda req: httpx.Response(200, json={"items": [
            {"dispute_id": "PP-D-1", "status": "WAITING_FOR_SELLER_RESPONSE"}]} if req.url.params.get("disputed_transaction_id") == "CAP6"
            else {"items": []}),
    }

    async def go():
        pp, _ = _client(routes)
        assert (await pp.capture_facts("CAP6"))["open_dispute"] is True
    asyncio.run(go())


def _evidence_tracking(req: httpx.Request) -> dict:
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content.decode()
    assert 'name="input"' in body
    payload = json.loads(body[body.index("{"):body.rindex("}") + 1])
    assert payload["evidences"][0]["evidence_type"] == "PROOF_OF_FULFILLMENT"
    return payload["evidences"][0]["evidence_info"]["tracking_info"][0]


def test_dispute_evidence_carrier_outside_enum_uses_other():
    sent = []

    def evidence(req):
        sent.append(_evidence_tracking(req))
        return httpx.Response(200, json={"links": []})

    async def go():
        pp, _ = _client({("POST", "/v1/customer/disputes/PP-D-9/provide-evidence"): evidence})
        for carrier in ("Lumen Freight", "OTHER", "usps"):
            await pp.call("provide_dispute_evidence", {"dispute_id": "PP-D-9", "notes": "Delivered.",
                                                       "tracking_number": "LF1", "carrier": carrier})
        assert sent == [
            {"tracking_number": "LF1", "carrier_name": "OTHER", "carrier_name_other": "Lumen Freight"},
            {"tracking_number": "LF1", "carrier_name": "OTHER", "carrier_name_other": "Other carrier"},
            {"tracking_number": "LF1", "carrier_name": "USPS"},
        ]
    asyncio.run(go())


def test_shipment_tracking_uses_orders_v2_track():
    routes = {
        ("GET", "/v2/payments/captures/CAP1"): {"id": "CAP1", "supplementary_data": {"related_ids": {"order_id": "ORD1"}}},
        ("POST", "/v2/checkout/orders/ORD1/track"): lambda req: httpx.Response(201, json={"id": "ORD1"}),
        ("GET", "/v2/payments/captures/CAP2"): {"id": "CAP2"},
    }

    async def go():
        pp, seen = _client(routes)
        await pp.call("create_shipment_tracking", {"capture_id": "CAP1", "tracking_number": "LF1", "carrier": "Lumen Freight"})
        await pp.call("create_shipment_tracking", {"capture_id": "CAP1", "tracking_number": "1Z9", "carrier": "UPS"})
        posts = [r for r in seen if r.method == "POST" and r.url.path == "/v2/checkout/orders/ORD1/track"]
        assert len(posts) == 2 and all("paypal-request-id" in r.headers for r in posts)
        assert json.loads(posts[0].content) == {"capture_id": "CAP1", "tracking_number": "LF1", "carrier": "OTHER",
                                                "carrier_name_other": "Lumen Freight", "notify_payer": False}
        b = json.loads(posts[1].content)
        assert b["carrier"] == "UPS" and "carrier_name_other" not in b
        assert not any("trackers-batch" in r.url.path for r in seen)
        try:
            await pp.call("create_shipment_tracking", {"capture_id": "CAP2", "tracking_number": "X", "carrier": "UPS"})
            raise AssertionError("expected PayPalError for a capture with no order")
        except PayPalError as e:
            assert e.status == 422
    asyncio.run(go())


def test_scripted_agent_sends_real_carrier_name_for_dispute_email():
    from demo.agent import ScriptedAgent
    from demo.inbox import BY_N, render_email
    from warden.app import DEFAULT_MANDATE
    from warden.gateway import Warden
    from warden.ledger import Ledger
    from warden.mandate import compile_mandate

    sent = []

    def evidence(req):
        sent.append(_evidence_tracking(req))
        return httpx.Response(200, json={"links": []})

    async def go():
        pp, _ = _client({
            ("GET", "/v1/customer/disputes/PP-D-55210"): {"dispute_id": "PP-D-55210", "status": "WAITING_FOR_SELLER_RESPONSE",
                                                         "dispute_amount": {"currency_code": "USD", "value": "64.00"}},
            ("POST", "/v1/customer/disputes/PP-D-55210/provide-evidence"): evidence,
        })
        w = Warden(pp, Ledger(":memory:"))
        w.set_mandate(await compile_mandate(DEFAULT_MANDATE))

        async def fetcher(url):
            return 200, "text/html", render_email(BY_N[6])
        w.fetcher = fetcher
        await ScriptedAgent(w).run(6)
        assert len(sent) == 1
        assert sent[0]["carrier_name"] == "OTHER" and sent[0]["carrier_name_other"] == "Lumen Freight"
        assert sent[0]["tracking_number"] == "LF928374651US"
    asyncio.run(go())
