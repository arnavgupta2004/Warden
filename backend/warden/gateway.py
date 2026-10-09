"""The gateway: the single path between an agent and PayPal.

decide -> record -> (execute | hold for approval | block) -> record outcome.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from . import llm
from .config import settings
from .events import Bus
from .ledger import Ledger
from .models import Approval, Decision, Finding, LedgerEntry, Mandate, ToolCall, Trust, Verdict
from .paypal import PayPalError
from .policy import PolicyEngine
from .provenance import ProvenanceStore, html_to_text
from .tools import TOOLS

log = logging.getLogger("warden.gateway")

Fetcher = Callable[[str], Awaitable[tuple[int, str, str]]]  # url -> (status, content_type, body)


async def default_fetcher(url: str) -> tuple[int, str, str]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "Warden/1.0 (+agent fetch)"})
        return r.status_code, r.headers.get("content-type", ""), r.text


class Warden:
    def __init__(self, backend: Any, ledger: Ledger | None = None, fetcher: Fetcher | None = None) -> None:
        self.backend = backend
        self.ledger = ledger or Ledger(":memory:")
        self.prov = ProvenanceStore()
        self.engine = PolicyEngine(self.prov, self.ledger, backend)
        self.bus = Bus()
        self.fetcher: Fetcher = fetcher or default_fetcher
        self.mandates: dict[str, Mandate] = {}
        self.approvals: dict[str, Approval] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._sessions: set[str] = set()
        self._waiting: set[str] = set()

    # ------------------------------------------------------------------ setup
    def set_mandate(self, m: Mandate) -> None:
        self.mandates[m.agent_id] = m
        self._sessions.clear()  # sessions re-seed principal context from the new mandate
        self.bus.publish("mandate", m.model_dump(mode="json"))

    def ensure_session(self, session_id: str, agent_id: str) -> None:
        if session_id in self._sessions:
            return
        self._sessions.add(session_id)
        m = self.mandates.get(agent_id)
        if m:
            payees = " ".join(f"{p.name} <{p.email}>" for p in m.trusted_payees)
            self.prov.record(session_id, Trust.PRINCIPAL, "mandate", f"{m.source_text}\nTrusted payees: {payees}")

    def add_task(self, session_id: str, agent_id: str, task: str) -> None:
        self.ensure_session(session_id, agent_id)
        self.prov.record(session_id, Trust.PRINCIPAL, "owner-task", task)

    # ----------------------------------------------------------------- tools
    async def call_tool(self, session_id: str, agent_id: str, tool: str, args: dict[str, Any],
                        stated_reason: str = "", wait_for_approval: bool = True) -> dict[str, Any]:
        self.ensure_session(session_id, agent_id)
        args = {k: v for k, v in (args or {}).items() if v is not None}
        reason = str(args.pop("reason", "") or stated_reason)
        if tool == "fetch_url":
            return await self._fetch(session_id, agent_id, str(args.get("url", "")))
        if tool == "warden_check_approval":
            return await self.check_approval(str(args.get("approval_id", "")))
        call = ToolCall(session_id=session_id, agent_id=agent_id, tool=tool, args=args, stated_reason=reason)
        return await self._gate(call, wait_for_approval)

    async def _fetch(self, session_id: str, agent_id: str, url: str) -> dict[str, Any]:
        try:
            status, ctype, body = await self.fetcher(url)
        except Exception as exc:
            return {"status": "error", "error": f"fetch failed: {exc}"}
        text = html_to_text(body) if "html" in ctype or body.lstrip().startswith("<") else body
        span = self.prov.record(session_id, Trust.UNTRUSTED, url, text, raw=body)
        findings = []
        if span.injection_signals:
            findings.append(Finding(rule="injection_signals", severity="medium", effect=Verdict.ALLOW,
                                    message="Content contains signs of a prompt injection. The session is now quarantined.",
                                    evidence={"signals": span.injection_signals, "source": url}))
        e = LedgerEntry(session_id=session_id, agent_id=agent_id, tool="fetch_url", args={"url": url},
                        verdict=Verdict.ALLOW, risk=40 if findings else 0, findings=findings,
                        explanation="Recorded as untrusted context." + (" Injection signals found." if findings else ""),
                        money=0.0, status="executed")
        self.ledger.record(e)
        self.bus.publish("ledger", e.model_dump(mode="json"))
        return {"status": "ok", "http_status": status, "content": text[:20000]}

    async def _explain(self, call: ToolCall, d: Decision) -> str:
        if d.verdict == Verdict.ALLOW:
            return "Within the mandate." if call.tool in TOOLS and TOOLS[call.tool].kind not in ("read", "context") else "Read-only."
        top = [x for x in d.findings if x.effect != Verdict.ALLOW][:3]
        fallback = top[0].message if top else ""
        if llm.enabled():
            txt = await llm.complete_text(
                "You explain payment-security decisions to a small-business owner in at most two short sentences. "
                "Plain words, no jargon, no markdown.",
                f"Verdict: {d.verdict.value}. Action: {call.tool} {call.args}. Reasons: {[x.message for x in top]}",
            )
            if txt:
                return txt
        return fallback

    async def _gate(self, call: ToolCall, wait: bool) -> dict[str, Any]:
        mandate = self.mandates.get(call.agent_id)
        d = await self.engine.evaluate(call, mandate)
        d.explanation = await self._explain(call, d)
        e = LedgerEntry(id=call.id.replace("call_", "led_"), session_id=call.session_id, agent_id=call.agent_id, tool=call.tool,
                        args=call.args, verdict=d.verdict, risk=d.risk, findings=d.findings, explanation=d.explanation,
                        money=d.money, currency=(mandate.currency if mandate else "USD"), counterparty=d.counterparty)
        self.ledger.record(e)
        self.bus.publish("ledger", e.model_dump(mode="json"))

        if d.verdict == Verdict.DENY:
            e = self.ledger.transition(e.id, "denied") or e
            self.bus.publish("ledger", e.model_dump(mode="json"))
            return {
                "status": "blocked",
                "warden": {"verdict": "deny", "risk": d.risk, "explanation": d.explanation,
                           "findings": [x.model_dump(mode="json") for x in d.findings if x.effect != Verdict.ALLOW]},
                "instruction": "Warden blocked this action. Do not retry it or try to achieve the same result another way. "
                               "Tell the owner what happened.",
            }
        if d.verdict == Verdict.STEP_UP:
            ap = Approval(ledger_id=e.id, call=call, decision=d)
            self.approvals[ap.id] = ap
            self._waiters[ap.id] = asyncio.Event()
            e = self.ledger.transition(e.id, "pending_approval") or e
            self.bus.publish("ledger", e.model_dump(mode="json"))
            self.bus.publish("approval", ap.model_dump(mode="json"))
            asyncio.create_task(self._notify(ap))
            if settings.demo_auto_approve:
                asyncio.get_running_loop().call_later(2.0, lambda: asyncio.create_task(self.decide(ap.id, True, "auto-approve (demo)")))
            if wait:
                self._waiting.add(ap.id)
                try:
                    await asyncio.wait_for(self._waiters[ap.id].wait(), timeout=settings.approval_timeout_s)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._waiting.discard(ap.id)
                if ap.status == "approved":
                    return await self._execute(call, e.id, approved_by=ap.decided_by)
                if ap.status == "denied":
                    return {"status": "blocked", "warden": {"verdict": "deny", "explanation": f"The owner declined this action ({ap.decided_by})."},
                            "instruction": "The owner declined. Do not retry."}
            return {
                "status": "pending_approval",
                "approval_id": ap.id,
                "warden": {"verdict": "step_up", "risk": d.risk, "explanation": d.explanation},
                "instruction": "This action is waiting for the owner's approval. Call warden_check_approval with the approval_id later.",
            }
        return await self._execute(call, e.id)

    async def _execute(self, call: ToolCall, entry_id: str, approved_by: str | None = None) -> dict[str, Any]:
        try:
            result = await self.backend.call(call.tool, call.args)
        except PayPalError as err:
            e = self.ledger.transition(entry_id, "failed", result=err.as_dict(), approved_by=approved_by)
            if e:
                self.bus.publish("ledger", e.model_dump(mode="json"))
            return {"status": "error", "paypal_error": err.as_dict()}
        except TypeError as err:  # bad arguments from the agent
            e = self.ledger.transition(entry_id, "failed", result={"error": str(err)}, approved_by=approved_by)
            if e:
                self.bus.publish("ledger", e.model_dump(mode="json"))
            return {"status": "error", "error": f"invalid arguments: {err}"}
        ref = paypal_ref(call.tool, result)
        self.prov.record(call.session_id, Trust.VERIFIED, f"paypal:{call.tool}", str(result)[:8000])
        e = self.ledger.transition(entry_id, "executed", result=result, paypal_ref=ref, approved_by=approved_by)
        if e:
            self.bus.publish("ledger", e.model_dump(mode="json"))
        out: dict[str, Any] = {"status": "executed", "paypal": result}
        if approved_by:
            out["approved_by"] = approved_by
        return out

    # ------------------------------------------------------------- approvals
    async def decide(self, approval_id: str, approve: bool, by: str = "owner") -> Approval | None:
        ap = self.approvals.get(approval_id)
        if not ap or ap.status != "pending":
            return ap
        ap.status = "approved" if approve else "denied"
        ap.decided_by = by
        ap.decided_at = time.time()
        if not approve:
            e = self.ledger.transition(ap.ledger_id, "denied", approved_by=None)
            if e:
                self.bus.publish("ledger", e.model_dump(mode="json"))
        else:
            e = self.ledger.transition(ap.ledger_id, "approved", approved_by=by)
            if e:
                self.bus.publish("ledger", e.model_dump(mode="json"))
        self.bus.publish("approval", ap.model_dump(mode="json"))
        ev = self._waiters.get(approval_id)
        if ev and not ev.is_set():
            ev.set()
        if approve and approval_id not in self._waiting:
            # Nobody is blocked on this call any more, so run it now.
            await self.check_approval(approval_id)
        return ap

    async def check_approval(self, approval_id: str) -> dict[str, Any]:
        ap = self.approvals.get(approval_id)
        if not ap:
            return {"status": "error", "error": "unknown approval_id"}
        if ap.status == "pending":
            return {"status": "pending_approval", "approval_id": approval_id}
        if ap.status == "denied":
            return {"status": "blocked", "warden": {"verdict": "deny", "explanation": "The owner declined this action."}}
        e = self.ledger.get(ap.ledger_id)
        if e and e.status == "executed":
            return {"status": "executed", "paypal": e.result}
        return await self._execute(ap.call, ap.ledger_id, approved_by=ap.decided_by)

    async def _notify(self, ap: Approval) -> None:
        d = ap.decision
        title = f"Warden: approve {ap.call.tool.replace('_', ' ')} ${d.money:,.2f}?"
        body = d.explanation or "An agent action needs your approval."
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                if settings.ntfy_topic:
                    base = settings.public_url.rstrip("/")
                    await c.post(
                        f"https://ntfy.sh/{settings.ntfy_topic}",
                        content=body.encode(),
                        headers={
                            "Title": title,
                            "Priority": "high",
                            "Tags": "lock",
                            "Click": f"{base}/approve/{ap.id}",
                            "Actions": f"http, Approve, {base}/api/approvals/{ap.id}/approve, method=POST, clear=true; "
                                       f"http, Deny, {base}/api/approvals/{ap.id}/deny, method=POST, clear=true",
                        },
                    )
                if settings.approval_webhook_url:
                    await c.post(settings.approval_webhook_url, json={
                        "approval_id": ap.id, "title": title, "explanation": body, "tool": ap.call.tool,
                        "args": ap.call.args, "amount": d.money, "counterparty": d.counterparty,
                        "approve_url": f"{settings.public_url.rstrip('/')}/approve/{ap.id}"})
        except Exception as exc:
            log.warning("approval notification failed: %s", exc)

    # ---------------------------------------------------------------- reset
    def reset(self) -> None:
        self.ledger.reset()
        self.prov.clear()
        self.approvals.clear()
        self._waiters.clear()
        self._sessions.clear()
        if hasattr(self.backend, "reset"):
            self.backend.reset()
        self.bus.publish("reset", {})


def paypal_ref(tool: str, r: dict[str, Any]) -> str | None:
    if not isinstance(r, dict):
        return None
    if "batch_header" in r:
        return r["batch_header"].get("payout_batch_id")
    for k in ("id", "invoice_id"):
        if r.get(k):
            return str(r[k])
    return None
