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
        pp, _ = _client({("POST", "/v1/customer/disputes/PP-D-9/provide-evidence"): evidence,
                         ("GET", "/v1/customer/disputes/PP-D-9"): {"links": [{"rel": "provide_evidence"}]}})
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
                                                         "dispute_amount": {"currency_code": "USD", "value": "64.00"},
                                                         "links": [{"rel": "provide_evidence"}]},
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



def _card_order(order_id, cap_id, status="COMPLETED"):
    pu = {"reference_id": "r", "payments": {"captures": [{"id": cap_id, "status": "COMPLETED"}]}} if status == "COMPLETED" else {"reference_id": "r"}
    return {"id": order_id, "status": status, "purchase_units": [pu]}


def test_create_card_capture_request_shape():
    def create(req):
        b = json.loads(req.content)
        card = b["payment_source"]["card"]
        assert b["intent"] == "CAPTURE" and b["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "42.00"}
        assert card["number"] == "4012888888881881" and len(card["expiry"]) == 7 and card["expiry"][4] == "-"
        assert card["billing_address"]["country_code"] == "US" and "paypal-request-id" in req.headers
        return httpx.Response(201, json=_card_order("ORDC", "CAPC"))

    async def go():
        pp, seen = _client({("POST", "/v2/checkout/orders"): create})
        assert await pp.create_card_capture("42.00", "Mug", "7XK") == ("ORDC", "CAPC")
        assert not any(r.url.path.endswith("/capture") for r in seen)  # captured on create: no second call

        # Some accounts return APPROVED; then Warden captures explicitly.
        pp, seen = _client({("POST", "/v2/checkout/orders"): _card_order("ORDA", "", "APPROVED"),
                            ("POST", "/v2/checkout/orders/ORDA/capture"): _card_order("ORDA", "CAPA")})
        assert await pp.create_card_capture("1.00", "x", "y") == ("ORDA", "CAPA")

        live = PayPalSandbox("cid", "secret", "https://api-m.paypal.com")
        try:
            await live.create_card_capture("1.00", "x", "y")
            raise AssertionError("must refuse outside the sandbox")
        except PayPalError as e:
            assert e.name == "SANDBOX_ONLY"
    asyncio.run(go())


def test_card_payer_shows_as_card_ending():
    routes = {
        ("GET", "/v2/payments/captures/CAPC"): {"id": "CAPC", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "42.00"},
                                               "supplementary_data": {"related_ids": {"order_id": "ORDC"}}},
        ("GET", "/v2/checkout/orders/ORDC"): {"id": "ORDC", "payment_source": {"card": {"last_digits": "1881", "brand": "VISA"}}},
        ("GET", "/v1/customer/disputes"): {"items": []},
    }

    async def go():
        pp, _ = _client(routes)
        assert (await pp.capture_facts("CAPC"))["payer_email"] == "card ending 1881"
    asyncio.run(go())


def test_reset_gives_refund_scenarios_fresh_captures(tmp_path, monkeypatch):
    from demo import inbox
    from warden.app import build_app
    from warden.config import settings
    from warden.gateway import Warden
    from warden.ledger import Ledger

    seed = tmp_path / "sandbox_seed.json"
    seed.write_text(json.dumps({"orders": {"8QT75510MC227094R": "PLANTER_ORDER"}, "captures": {"6DW31852LP480916E": "PLANTER_CAP"},
                                "invoices": {}, "disputes": {"PP-D-55210": "PP-D-REAL"}}))
    monkeypatch.setattr(inbox, "SEED_FILE", seed)
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    inbox.sandbox_ids.cache_clear()
    counter = {"n": 0}

    def create(req):
        counter["n"] += 1
        return httpx.Response(201, json=_card_order(f"ORD{counter['n']}", f"CAP{counter['n']}"))

    async def go():
        pp, _ = _client({("POST", "/v2/checkout/orders"): create})
        app = build_app(Warden(pp, Ledger(":memory:")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = (await c.post("/api/reset")).json()
            assert r["ok"] and r["fresh_captures"] == {"4RT88014HN112233K": "CAP1", "9LM20577QW341870P": "CAP2", "1VC66309TR775024A": "CAP3"}
            data = json.loads(seed.read_text())
            assert data["captures"]["6DW31852LP480916E"] == "PLANTER_CAP"  # the dispute's capture is untouched
            assert data["orders"]["7XK21934UB556120D"] == "ORD1" and data["disputes"] == {"PP-D-55210": "PP-D-REAL"}
            assert "ORD1" in inbox.localize("order 7XK21934UB556120D")  # the inbox sees the new ids right away

            await c.post("/api/reset")
            assert json.loads(seed.read_text())["captures"]["4RT88014HN112233K"] == "CAP4"  # fresh again
            await c.post("/api/reset")
            r = await c.post("/api/reset")
            assert r.status_code == 429 and counter["n"] == 9  # 3 resets a minute; the 4th creates no orders
    asyncio.run(go())
    inbox.sandbox_ids.cache_clear()



def test_sandbox_seed_env_merges_over_file_and_gates_email_6(tmp_path, monkeypatch):
    from demo import inbox
    from warden.app import build_app
    from warden.config import settings
    from warden.gateway import Warden
    from warden.ledger import Ledger
    from warden.paypal import Simulator

    seed = tmp_path / "sandbox_seed.json"
    seed.write_text(json.dumps({"captures": {"4RT88014HN112233K": "FILECAP"}, "invoices": {"INV2-KST7-4Q2M-8ZP3-HC41": "INV-FILE"}}))
    monkeypatch.setattr(inbox, "SEED_FILE", seed)
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    monkeypatch.delenv("WARDEN_SANDBOX_SEED", raising=False)
    inbox.sandbox_ids.cache_clear()

    async def go():
        app = build_app(Warden(Simulator(), Ledger(":memory:")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            six = next(e for e in (await c.get("/api/demo/emails")).json() if e["n"] == 6)
            assert six["unavailable"] == "Needs a sandbox dispute"
            r = await c.post("/api/demo/run", json={"email": 6})
            assert r.status_code == 409 and "README" in r.json()["detail"]
            assert next(e for e in (await c.get("/api/demo/emails")).json() if e["n"] == 1)["unavailable"] is None

            # WARDEN_SANDBOX_SEED (e.g. on Render) adds the dispute and overrides the invoice; file captures stay.
            monkeypatch.setenv("WARDEN_SANDBOX_SEED", json.dumps({"invoices": {"INV2-KST7-4Q2M-8ZP3-HC41": "INV-ENV"},
                                                                  "disputes": {"PP-D-55210": "PP-D-REAL"}}))
            inbox.sandbox_ids.cache_clear()
            six = next(e for e in (await c.get("/api/demo/emails")).json() if e["n"] == 6)
            assert six["unavailable"] is None and "PP-D-REAL" in six["subject"]
            assert inbox.sandbox_ids()["INV2-KST7-4Q2M-8ZP3-HC41"] == "INV-ENV"
            assert inbox.sandbox_ids()["4RT88014HN112233K"] == "FILECAP"

            monkeypatch.setenv("WARDEN_SANDBOX_SEED", "{not json")
            inbox.sandbox_ids.cache_clear()
            assert inbox.sandbox_ids()["4RT88014HN112233K"] == "FILECAP"  # bad JSON is ignored, not fatal
    asyncio.run(go())
    inbox.sandbox_ids.cache_clear()



def test_webhook_verification_rejects_unsigned_or_invalid_events():
    from warden.app import build_app
    from warden.config import settings
    from warden.gateway import Warden
    from warden.ledger import Ledger

    calls = []

    def verify(req):
        calls.append(json.loads(req.content))
        if json.loads(req.content)["transmission_sig"] == "bad":
            return httpx.Response(400, json={"name": "VALIDATION_ERROR", "message": "Invalid data provided"})
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    async def go():
        pp, _ = _client({("POST", "/v1/notifications/verify-webhook-signature"): verify})
        old = settings.paypal_webhook_id
        settings.paypal_webhook_id = "WH-1"
        try:
            app = build_app(Warden(pp, Ledger(":memory:")))
            ev = {"event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {"id": "REF1"}}
            sig = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
                   "paypal-transmission-id": "t1", "paypal-transmission-time": "2026-10-10T00:00:00Z"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                r = await c.post("/webhooks/paypal", json=ev)  # no signature headers at all
                assert r.status_code == 400 and not calls  # rejected without asking PayPal
                r = await c.post("/webhooks/paypal", json=ev, headers={**sig, "paypal-transmission-sig": "bad"})
                assert r.status_code == 400 and len(calls) == 1  # PayPal said invalid: 400, not a crash
                r = await c.post("/webhooks/paypal", json=ev, headers={**sig, "paypal-transmission-sig": "good"})
                assert r.status_code == 200 and calls[-1]["webhook_id"] == "WH-1"
        finally:
            settings.paypal_webhook_id = old
    asyncio.run(go())



def test_open_dispute_found_by_id_when_the_list_lags():
    # PayPal's dispute list can miss a new dispute for a few minutes; a known dispute id is checked directly.
    routes = {
        ("GET", "/v2/payments/captures/CAP6"): {"id": "CAP6", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "64.00"}},
        ("GET", "/v1/customer/disputes"): {"items": []},
        ("GET", "/v1/customer/disputes/PP-R-1"): {"dispute_id": "PP-R-1", "status": "UNDER_REVIEW",
                                                  "disputed_transactions": [{"seller_transaction_id": "CAP6"}]},
        ("GET", "/v1/customer/disputes/PP-R-2"): {"dispute_id": "PP-R-2", "status": "RESOLVED",
                                                  "disputed_transactions": [{"seller_transaction_id": "CAP7"}]},
    }

    async def go():
        pp, _ = _client(routes)
        assert (await pp.capture_facts("CAP6"))["open_dispute"] is False  # nothing known yet
        pp.known_disputes = {"PP-R-1", "PP-R-2"}
        assert (await pp.capture_facts("CAP6"))["open_dispute"] is True
        routes[("GET", "/v2/payments/captures/CAP7")] = {"id": "CAP7", "status": "COMPLETED",
                                                         "amount": {"currency_code": "USD", "value": "10.00"}}
        assert (await pp.capture_facts("CAP7"))["open_dispute"] is False  # that dispute is resolved
    asyncio.run(go())



def test_dispute_evidence_not_resent_when_paypal_already_has_it():
    posts = []
    answered = {"dispute_id": "PP-R-1", "status": "WAITING_FOR_BUYER_RESPONSE", "links": [{"rel": "self"}, {"rel": "escalate"}],
                "evidences": [{"source": "SUBMITTED_BY_BUYER"}, {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT"}]}

    def evidence(req):
        posts.append(req)
        return httpx.Response(200, json={"links": []})

    async def go():
        pp, _ = _client({("GET", "/v1/customer/disputes/PP-R-1"): answered,
                         ("POST", "/v1/customer/disputes/PP-R-1/provide-evidence"): evidence})
        r = await pp.call("provide_dispute_evidence", {"dispute_id": "PP-R-1", "notes": "Delivered.", "tracking_number": "LF1"})
        assert r["already_answered"] is True and "nothing new was sent" in r["message"] and not posts
    asyncio.run(go())
