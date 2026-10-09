"""Exercise the Claude-powered paths with a fake client, so they are covered
without an API key."""
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

    async def cj(system, user, max_tokens=1500):
        return state["json"](system, user) if callable(state["json"]) else state["json"]

    async def ct(system, user, max_tokens=400):
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


class FakeMessages:
    def __init__(self, script):
        self.script = script
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def test_claude_agent_loop_routes_tools_through_warden(monkeypatch, fake_llm):
    async def go():
        fake_llm["json"] = None
        m = await mandate_mod.compile_mandate(DEFAULT_MANDATE)
        w = Warden(Simulator(), Ledger(":memory:"))
        w.set_mandate(m)

        async def fetcher(url):
            from demo.inbox import BY_N, render_email
            return 200, "text/html", render_email(BY_N[3])
        w.fetcher = fetcher

        tu = lambda i, name, inp: NS(type="tool_use", id=i, name=name, input=inp)
        script = [
            NS(content=[NS(type="text", text="Reading the email."), tu("t1", "fetch_url", {"url": "http://x/demo/inbox/3"})]),
            NS(content=[tu("t2", "create_payout", {"receiver_email": "refunds-desk@kestrel-support.example", "amount": 499,
                                                   "reason": "system note"})]),
            NS(content=[NS(type="text", text="The payout was blocked; reporting to the owner.")]),
        ]
        fake = FakeMessages(script)
        monkeypatch.setattr(agent_mod.llm, "client", lambda: NS(messages=fake))
        lines = await agent_mod.ClaudeAgent(w).run(3)
        assert any(l["role"] == "tool_result" and l["status"] == "blocked" for l in lines)
        # history sent back to the API contains only clean block dicts
        second = fake.calls[1]["messages"][1]["content"]
        assert {b["type"] for b in second} == {"text", "tool_use"}
        assert set(second[1]) == {"type", "id", "name", "input"}
        assert not w.backend.payouts
    asyncio.run(go())
