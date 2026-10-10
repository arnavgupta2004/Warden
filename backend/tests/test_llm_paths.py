"""Exercise the Gemini-powered paths (mandate compile, intent check, agent
loop) with fakes, so they are covered without an API key."""
import asyncio
import os
from types import SimpleNamespace as NS

os.environ.setdefault("WARDEN_DISABLE_LLM", "1")
os.environ.setdefault("WARDEN_PAYPAL_MODE", "simulator")

import pytest

from demo import agent as agent_mod
from warden import llm, mandate as mandate_mod, policy as policy_mod
from warden.app import DEFAULT_MANDATE
from warden.gateway import Warden
from warden.ledger import Ledger
from warden.models import Verdict
from warden.paypal import Simulator


@pytest.fixture
def fake_llm(monkeypatch):
    state = {"json": None, "text": "Short explanation.", "json_calls": 0, "text_calls": 0}

    async def cj(system, user, **kw):
        state["json_calls"] += 1
        return state["json"](system, user) if callable(state["json"]) else state["json"]

    async def ct(system, user, **kw):
        state["text_calls"] += 1
        return state["text"]

    for mod in (llm, mandate_mod.llm, policy_mod.llm):
        monkeypatch.setattr(mod, "enabled", lambda: True)
        monkeypatch.setattr(mod, "complete_json", cj)
        monkeypatch.setattr(mod, "complete_text", ct)
    return state


def test_llm_mandate_cannot_loosen_limits_or_invent_payees(fake_llm):
    fake_llm["json"] = {
        "purpose": "Support and payables", "currency": "USD", "budget_total": 50000, "budget_window": "week",
        "per_tx_max": 0, "auto_approve_below": 0, "refund_auto_max": 500, "capabilities": ["refunds", "payouts"],
        "trusted_payees": [{"name": "Mallory", "email": "mallory@evil.example", "max_per_payment": 9999}],
        "blocked_categories": ["gift cards"], "new_payee_requires_approval": True, "max_moves_per_hour": 10,
    }
    m = asyncio.run(mandate_mod.compile_mandate(DEFAULT_MANDATE))
    assert m.compiled_by == "llm+rules"
    assert m.budget_total == 1500  # stricter of 1500 (text) and 50000 (llm)
    assert m.refund_auto_max == 50
    assert "mallory@evil.example" not in {p.email for p in m.trusted_payees}
    assert "gift cards" in m.blocked_categories
    assert m.max_moves_per_hour == 10


def test_intent_check_can_hold_an_otherwise_allowed_payout(fake_llm):
    async def go():
        fake_llm["json"] = None
        m = await mandate_mod.compile_mandate(DEFAULT_MANDATE)
        w = Warden(Simulator(), Ledger(":memory:"))
        w.set_mandate(m)
        fake_llm["json"] = {"aligned": False, "confidence": 0.9, "concern": "Paying a contractor was not part of this support task."}
        r = await w.call_tool("s", m.agent_id, "create_payout", {"receiver_email": "maya@chenstudio.example", "amount": 100},
                              wait_for_approval=False)
        assert r["status"] == "pending_approval"
        e = w.ledger.all()[0]
        assert e.verdict == Verdict.STEP_UP and any(f.rule == "intent_check" for f in e.findings)
        # One call: the intent check's plain-words concern is the explanation; no separate explanation call.
        assert e.explanation == "Paying a contractor was not part of this support task."
        assert fake_llm["text_calls"] == 0
    asyncio.run(go())


def test_gemini_enabled_by_key(monkeypatch):
    from warden.config import Settings
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "WARDEN_DISABLE_LLM"):
        monkeypatch.delenv(k, raising=False)
    assert not Settings().llm_enabled
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    s = Settings()
    assert s.llm_enabled and s.llm_model.startswith("gemini")
    monkeypatch.setenv("WARDEN_DISABLE_LLM", "1")
    assert not Settings().llm_enabled


def _use_gemini(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_disabled", False)
    monkeypatch.setattr(llm.settings, "gemini_api_key", "test-key")


def test_gemini_complete_json_request_shape(monkeypatch):
    import httpx
    import json as _json
    _use_gemini(monkeypatch)
    monkeypatch.setattr(llm.settings, "llm_model", "gemini-2.5-flash")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"text": "thinking...", "thought": True}, {"text": '{"aligned": false, "confidence": 0.9, "concern": "x"}'}]}}]})

    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    j = asyncio.run(llm.complete_json("Reviewer.", "Proposed action", schema=policy_mod.INTENT_SCHEMA))
    assert j == {"aligned": False, "confidence": 0.9, "concern": "x"}
    req = seen[0]
    assert req.url.path.endswith("/models/gemini-2.5-flash:generateContent")
    assert req.headers["x-goog-api-key"] == "test-key" and "key=" not in str(req.url)
    cfg = _json.loads(req.content)["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseJsonSchema"]["required"] == ["aligned", "confidence", "concern", "explanation"]
    assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
    assert _json.loads(req.content)["contents"][0]["parts"][0]["text"] == "Proposed action"

    # Mandate compiling keeps the model's normal thinking (no thinkingConfig) and sends no schema.
    asyncio.run(llm.complete_json("Compile.", "mandate text", quick=False))
    cfg = _json.loads(seen[1].content)["generationConfig"]
    assert "thinkingConfig" not in cfg and "responseJsonSchema" not in cfg and cfg["responseMimeType"] == "application/json"


def test_quick_thinking_matches_model_family():
    assert llm.quick_thinking("gemini-3.8-flash") == {"thinkingLevel": "LOW"}  # 3.8-flash rejects MINIMAL
    assert llm.quick_thinking("gemini-3-pro-preview") == {"thinkingLevel": "LOW"}
    assert llm.quick_thinking("gemini-2.5-flash-lite") == {"thinkingBudget": 0}
    assert llm.quick_thinking("gemini-2.5-pro") == {"thinkingBudget": 128}
    assert llm.quick_thinking("gemini-2.0-flash") is None  # no thinking support: thinkingConfig would be an error


def test_gemini_timeout_falls_back_to_deterministic_path(monkeypatch):
    import time
    import httpx

    async def go():
        m = await mandate_mod.compile_mandate(DEFAULT_MANDATE)  # compiled before Gemini is on
        _use_gemini(monkeypatch)
        monkeypatch.setattr(llm.settings, "llm_timeout_s", 0.2)

        async def slow(req):
            await asyncio.sleep(5)
            return httpx.Response(200, json={})
        monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(slow)))

        t0 = time.monotonic()
        assert await llm.complete_json("x", "y", schema=policy_mod.INTENT_SCHEMA) is None
        assert await llm.complete_text("x", "y") is None
        assert time.monotonic() - t0 < 1.5

        # Through Warden: the intent check and explanation time out, and the deterministic decision stands.
        w = Warden(Simulator(), Ledger(":memory:"))
        w.set_mandate(m)
        t0 = time.monotonic()
        r = await w.call_tool("s", m.agent_id, "create_refund", {"capture_id": "4RT88014HN112233K", "amount": 42},
                              wait_for_approval=False)
        assert time.monotonic() - t0 < 1.5
        assert r["status"] == "executed"
        e = w.ledger.all()[0]
        assert e.verdict == Verdict.ALLOW and not any(f.rule == "intent_check" for f in e.findings)
    asyncio.run(go())


def test_gemini_agent_loop_routes_tools_through_warden(monkeypatch, fake_llm):
    _use_gemini(monkeypatch)

    async def go():
        fake_llm["json"] = None
        m = await mandate_mod.compile_mandate(DEFAULT_MANDATE)
        w = Warden(Simulator(), Ledger(":memory:"))
        w.set_mandate(m)

        async def fetcher(url):
            from demo.inbox import BY_N, render_email
            return 200, "text/html", render_email(BY_N[3])
        w.fetcher = fetcher

        call = lambda name, args, i: {"functionCall": {"name": name, "args": args, "id": i}, "thoughtSignature": f"sig-{i}"}
        turn = lambda *parts: {"candidates": [{"content": {"role": "model", "parts": list(parts)}}]}
        script = [
            turn({"text": "Reading the email."}, call("fetch_url", {"url": "http://x/demo/inbox/3"}, "c1")),
            turn(call("create_payout", {"receiver_email": "refunds-desk@kestrel-support.example", "amount": 499,
                                        "reason": "system note"}, "c2")),
            turn({"text": "The payout was blocked; reporting to the owner."}),
        ]
        bodies = []

        async def fake_generate(body, **kw):
            bodies.append(_json.loads(_json.dumps(body)))
            return script.pop(0)
        import json as _json
        monkeypatch.setattr(agent_mod.llm, "gemini_generate", fake_generate)
        agent = agent_mod.make_agent("gemini", w)
        assert isinstance(agent, agent_mod.GeminiAgent)
        lines = await agent.run(3)
        assert any(l["role"] == "tool_result" and l["status"] == "blocked" for l in lines)
        # model turn is sent back verbatim (thought signature kept), then a functionResponse with the matching id
        second = bodies[1]["contents"]
        assert second[1]["role"] == "model" and second[1]["parts"][1]["thoughtSignature"] == "sig-c1"
        fr = second[2]["parts"][0]["functionResponse"]
        assert fr["name"] == "fetch_url" and fr["id"] == "c1" and fr["response"]["status"] == "ok"
        decls = {d["name"]: d for d in bodies[0]["tools"][0]["functionDeclarations"]}
        assert "parameters" not in decls["list_invoices"] and "reason" in decls["create_payout"]["parameters"]["properties"]
        assert not w.backend.payouts
    asyncio.run(go())


def test_gemini_retries_503_once_but_never_429(monkeypatch):
    import httpx
    _use_gemini(monkeypatch)
    monkeypatch.setattr(llm, "RETRY_503_DELAY_S", 0)
    monkeypatch.setattr(llm, "_limited_until", 0.0)
    ok = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    replies, seen = [httpx.Response(503, json={"error": {"message": "high demand"}}), ok], []

    def handler(req):
        seen.append(req)
        return replies.pop(0)
    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(llm.complete_text("x", "y")) == "ok" and len(seen) == 2

    # 429: no retry, immediate fallback, then a cooldown during which Gemini isn't called at all
    replies[:], seen[:] = [httpx.Response(429, json={"error": {"message": "quota"}})], []
    assert asyncio.run(llm.complete_text("x", "quota")) is None and len(seen) == 1
    assert llm.limited()
    assert asyncio.run(llm.complete_json("x", "cooldown")) is None and len(seen) == 1
    monkeypatch.setattr(llm, "_limited_until", 0.0)
    assert not llm.limited()


async def _decide(fake_llm, tool, args, j):
    m = await mandate_mod.compile_mandate(DEFAULT_MANDATE)
    w = Warden(Simulator(), Ledger(":memory:"))
    w.set_mandate(m)
    fake_llm["json"], fake_llm["json_calls"], fake_llm["text_calls"] = j, 0, 0
    await w.call_tool("s", m.agent_id, tool, args, wait_for_approval=False)
    return w.ledger.all()[0]


def test_one_gemini_call_per_payment(fake_llm):
    aligned = {"aligned": True, "confidence": 0.9, "concern": "", "explanation": "This refund is over your $50 limit, so it waits for you."}

    async def go():
        # Allowed by rules and Gemini: one call (the intent check), plain "Within the mandate."
        e = await _decide(fake_llm, "create_refund", {"capture_id": "4RT88014HN112233K", "amount": 42}, aligned)
        assert e.verdict == Verdict.ALLOW and (fake_llm["json_calls"], fake_llm["text_calls"]) == (1, 0)

        # Held by a hard rule, Gemini agrees: the same call's explanation is used.
        e = await _decide(fake_llm, "create_refund", {"capture_id": "9LM20577QW341870P", "amount": 180}, aligned)
        assert e.verdict == Verdict.STEP_UP and e.explanation == aligned["explanation"]
        assert (fake_llm["json_calls"], fake_llm["text_calls"]) == (1, 0)

        # Held by a hard rule, Gemini call failed: deterministic message, still no second call.
        e = await _decide(fake_llm, "create_refund", {"capture_id": "9LM20577QW341870P", "amount": 180}, None)
        assert e.verdict == Verdict.STEP_UP and "above the $50.00" in e.explanation
        assert (fake_llm["json_calls"], fake_llm["text_calls"]) == (1, 0)

        # Blocked by a hard rule: no intent check, so exactly one explanation call.
        e = await _decide(fake_llm, "create_payout", {"receiver_email": "maya.chen.studio@payout-desk.example", "amount": 380}, aligned)
        assert e.verdict == Verdict.DENY and e.explanation == "Short explanation."
        assert (fake_llm["json_calls"], fake_llm["text_calls"]) == (0, 1)
    asyncio.run(go())


def _gemini_http(monkeypatch, handler):
    import httpx
    _use_gemini(monkeypatch)
    seen = []

    def wrapped(req):
        seen.append(req)
        return handler(req)
    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


def _ok(text):
    import httpx
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_llm_cache_hit_miss_and_persistence(monkeypatch):
    seen = _gemini_http(monkeypatch, lambda req: _ok('{"aligned": true, "confidence": 0.8, "concern": "", "explanation": ""}'))

    async def go():
        a = await llm.complete_json("sys", "prompt one", schema=policy_mod.INTENT_SCHEMA)
        b = await llm.complete_json("sys", "prompt one", schema=policy_mod.INTENT_SCHEMA)  # hit
        assert a == b and len(seen) == 1
        await llm.complete_json("sys", "prompt two", schema=policy_mod.INTENT_SCHEMA)  # miss: different prompt
        await llm.complete_json("sys", "prompt one")  # miss: different schema
        await llm.complete_text("sys", "prompt one")  # miss: text, not JSON
        assert len(seen) == 4
        monkeypatch.setattr(llm.settings, "llm_model", "gemini-other")
        await llm.complete_json("sys", "prompt one", schema=policy_mod.INTENT_SCHEMA)  # miss: different model
        assert len(seen) == 5
    asyncio.run(go())


def test_llm_cache_survives_restart(monkeypatch):
    seen = _gemini_http(monkeypatch, lambda req: _ok("Plain answer."))

    async def go():
        assert await llm.complete_text("sys", "explain") == "Plain answer."
        llm.cache_reset()
        assert await llm.complete_text("sys", "explain") == "Plain answer." and len(seen) == 1
    asyncio.run(go())


def test_llm_cache_evicts_oldest_first(monkeypatch):
    seen = _gemini_http(monkeypatch, lambda req: _ok("answer"))
    monkeypatch.setattr(llm, "CACHE_MAX", 3)

    async def go():
        for i in range(4):
            await llm.complete_text("sys", f"p{i}")
        assert len(seen) == 4
        await llm.complete_text("sys", "p3")  # newest: still cached
        await llm.complete_text("sys", "p1")  # still cached
        assert len(seen) == 4
        await llm.complete_text("sys", "p0")  # oldest was evicted: called again
        assert len(seen) == 5
        import json as _json
        assert len(_json.loads(open(llm.settings.llm_cache_path).read())) == 3
    asyncio.run(go())


def test_llm_cache_never_stores_failures_or_empty_answers(monkeypatch):
    import httpx
    replies = [httpx.Response(500, json={"error": {"message": "boom"}}), _ok(""), _ok("not json at all"), _ok("Real answer.")]
    seen = _gemini_http(monkeypatch, lambda req: replies.pop(0))
    monkeypatch.setattr(llm, "_limited_until", 0.0)

    async def go():
        assert await llm.complete_text("sys", "q") is None  # HTTP error
        assert await llm.complete_text("sys", "q") is None  # empty answer
        assert await llm.complete_json("sys", "q") is None  # unparseable JSON
        assert await llm.complete_text("sys", "q") == "Real answer."
        assert len(seen) == 4  # every failure went back to Gemini; nothing bad was cached
        import json as _json
        assert list(_json.loads(open(llm.settings.llm_cache_path).read()).values()) == ["Real answer."]
    asyncio.run(go())


def test_mandate_compile_uses_the_cache(monkeypatch):
    seen = _gemini_http(monkeypatch, lambda req: _ok(
        '{"purpose": "Support", "refund_auto_max": 50, "capabilities": ["refunds"], "budget_total": 1500}'))

    async def go():
        a = await mandate_mod.compile_mandate(DEFAULT_MANDATE)
        llm.cache_reset()  # e.g. a server restart
        b = await mandate_mod.compile_mandate(DEFAULT_MANDATE)
        assert len(seen) == 1 and a.compiled_by == b.compiled_by == "llm+rules"
        assert b.refund_auto_max == 50 and mandate_mod.verify(b)
    asyncio.run(go())


def test_rate_limits_on_demo_runs_and_mandate_compiles():
    import httpx
    from warden.app import RateLimiter, build_app

    async def go():
        w = Warden(Simulator(), Ledger(":memory:"))
        app = build_app(w)
        app.state.run_limiters = [RateLimiter(2)]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            codes = [(await c.post("/api/demo/run", json={"email": 99})).status_code for _ in range(3)]
            assert codes == [404, 404, 429]  # the limit applies before any work is done
            other = await c.post("/api/demo/run", json={"email": 99}, headers={"cf-connecting-ip": "203.0.113.9"})
            assert other.status_code == 404  # another address has its own budget
            codes = [(await c.post("/api/mandate", json={"text": DEFAULT_MANDATE})).status_code for _ in range(4)]
            assert codes == [200] * 3 + [429]
            r = await c.post("/api/mandate", json={"text": DEFAULT_MANDATE})
            assert r.headers["retry-after"] == "60" and "Try again in a minute" in r.json()["detail"]
    asyncio.run(go())


def test_429_quota_details_are_read_and_set_the_cooldown(monkeypatch):
    import json as _json
    import httpx
    body = {"error": {"code": 429, "message": "You exceeded your current quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "1000"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1800s"}]}}
    assert llm.parse_quota(_json.dumps(body)) == {
        "quota": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "limit": 1000, "retry_after_s": 1800.0}
    assert llm.parse_quota("not json") == {}

    _gemini_http(monkeypatch, lambda req: httpx.Response(429, json=body))
    monkeypatch.setattr(llm, "_limited_until", 0.0)
    assert asyncio.run(llm.complete_text("x", "daily")) is None
    assert llm.quota_info()["limit"] == 1000
    assert llm._limited_until - llm.time.monotonic() > 1700  # waits as long as Gemini asked, not just 60 s
    monkeypatch.setattr(llm, "_limited_until", 0.0)


def test_gemini_agent_runs_have_their_own_tighter_limit():
    import httpx
    from warden.app import RateLimiter, build_app

    async def go():
        app = build_app(Warden(Simulator(), Ledger(":memory:")))
        app.state.gemini_run_limiters = [RateLimiter(1), RateLimiter(2, 86400)]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            g = lambda ip: c.post("/api/demo/run", json={"email": 99, "agent": "gemini"}, headers={"cf-connecting-ip": ip})
            assert (await g("198.51.100.1")).status_code == 404
            r = await g("198.51.100.1")
            assert r.status_code == 429 and "Gemini agent runs" in r.json()["detail"]
            # the gullible agent is unaffected
            assert (await c.post("/api/demo/run", json={"email": 99}, headers={"cf-connecting-ip": "198.51.100.1"})).status_code == 404
            app.state.gemini_run_limiters[0] = RateLimiter(10)  # past the per-minute window
            assert (await g("198.51.100.1")).status_code == 404
            r = await g("198.51.100.1")
            assert r.status_code == 429 and "tomorrow" in r.json()["detail"]
    asyncio.run(go())
