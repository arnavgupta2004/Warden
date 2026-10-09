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
    state = {"json": None, "text": "Short explanation."}

    async def cj(system, user):
        return state["json"](system, user) if callable(state["json"]) else state["json"]

    async def ct(system, user):
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
        assert e.explanation == "Short explanation."
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
    _use_gemini(monkeypatch)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"text": "thinking...", "thought": True}, {"text": '{"aligned": false, "confidence": 0.9}'}]}}]})

    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    j = asyncio.run(llm.complete_json("Reviewer.", "Proposed action"))
    assert j == {"aligned": False, "confidence": 0.9}
    req = seen[0]
    assert req.url.path.endswith(f"/models/{llm.settings.llm_model}:generateContent")
    assert req.headers["x-goog-api-key"] == "test-key" and "key=" not in str(req.url)
    import json as _json
    body = _json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["contents"][0]["parts"][0]["text"] == "Proposed action"


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

        async def fake_generate(body):
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
