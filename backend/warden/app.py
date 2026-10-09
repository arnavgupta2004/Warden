"""HTTP surface: MCP endpoint, REST gate, dashboard API, live events, PayPal
webhooks, the demo inbox, and the built dashboard."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm
from .config import settings
from .gateway import Warden
from .ledger import Ledger
from .mandate import compile_mandate
from .mcp import router as mcp_router, tool_list
from .paypal import make_backend

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_MANDATE = (
    "Run customer support and payables for Kestrel & Co. Refund customers up to $50 on your own; anything bigger, ask me. "
    "Pay our contractors Maya Chen (maya@chenstudio.example) and Diego Alvarez (diego.alvarez@lumenfreight.example) "
    "up to $400 each. Chase unpaid invoices and answer disputes with tracking info. "
    "Never pay anyone new without asking me. Keep total spend under $1,500 a week."
)

class MandateIn(BaseModel):
    text: str
    agent_id: str = "backoffice-agent"


class ToolIn(BaseModel):
    session_id: str | None = None
    agent_id: str = "backoffice-agent"
    args: dict[str, Any] = {}
    reason: str = ""
    wait: bool = False


class SessionIn(BaseModel):
    agent_id: str = "backoffice-agent"
    task: str


class RunIn(BaseModel):
    email: int
    agent: str = "scripted"


STATIC = Path(__file__).resolve().parent.parent / "static"


def build_app(warden: Warden | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not app.state.warden.mandates:
            app.state.warden.set_mandate(await compile_mandate(DEFAULT_MANDATE))
        yield

    app = FastAPI(title="Warden", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["Mcp-Session-Id"])

    if warden is None:
        warden = Warden(make_backend(), Ledger(settings.db_path))

    # Fetch our own demo pages in-process; everything else goes over the network.
    async def fetcher(url: str) -> tuple[int, str, str]:
        base = settings.public_url.rstrip("/")
        if url.startswith(base + "/demo/") or url.startswith("/demo/"):
            path = url[len(base):] if url.startswith(base) else url
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://warden.local") as c:
                r = await c.get(path)
                return r.status_code, r.headers.get("content-type", ""), r.text
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
            return r.status_code, r.headers.get("content-type", ""), r.text

    warden.fetcher = fetcher
    app.state.warden = warden
    app.state.runs: dict[str, asyncio.Task] = {}
    app.include_router(mcp_router)

    # ------------------------------------------------------------- status
    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        m = warden.mandates.get("backoffice-agent")
        return {
            "paypal_mode": getattr(warden.backend, "mode", "unknown"),
            "llm": llm.enabled(),
            "llm_model": settings.llm_model if llm.enabled() else None,
            "chain": warden.ledger.verify(),
            "mandate_id": m.id if m else None,
            "pending_approvals": sum(1 for a in warden.approvals.values() if a.status == "pending"),
            "mcp_url": f"{settings.public_url.rstrip('/')}/mcp",
        }

    # ------------------------------------------------------------ mandate
    @app.get("/api/mandate")
    async def get_mandate(agent_id: str = "backoffice-agent") -> dict[str, Any]:
        m = warden.mandates.get(agent_id)
        if not m:
            raise HTTPException(404, "no mandate")
        since_map = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "total": 10**10}
        import time as _t
        spent = warden.ledger.spent(agent_id, _t.time() - since_map[m.budget_window])
        return {"mandate": m.model_dump(mode="json"), "spent": spent}

    @app.post("/api/mandate")
    async def set_mandate(body: MandateIn) -> dict[str, Any]:
        if not body.text.strip():
            raise HTTPException(400, "empty mandate")
        m = await compile_mandate(body.text, body.agent_id)
        warden.set_mandate(m)
        return {"mandate": m.model_dump(mode="json")}

    # ------------------------------------------------------------- ledger
    @app.get("/api/ledger")
    async def ledger(limit: int = 300) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in warden.ledger.all(limit)]

    @app.get("/api/verify")
    async def verify() -> dict[str, Any]:
        return warden.ledger.verify()

    # ---------------------------------------------------------- approvals
    @app.get("/api/approvals")
    async def approvals() -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in sorted(warden.approvals.values(), key=lambda a: -a.created_at)]

    @app.get("/api/approvals/{aid}")
    async def approval(aid: str) -> dict[str, Any]:
        a = warden.approvals.get(aid)
        if not a:
            raise HTTPException(404, "unknown approval")
        return a.model_dump(mode="json")

    @app.post("/api/approvals/{aid}/{action}")
    async def decide(aid: str, action: str, request: Request) -> dict[str, Any]:
        if action not in ("approve", "deny"):
            raise HTTPException(400, "action must be approve or deny")
        by = request.headers.get("x-warden-user", "owner")
        a = await warden.decide(aid, action == "approve", by)
        if not a:
            raise HTTPException(404, "unknown approval")
        return a.model_dump(mode="json")

    # --------------------------------------------------------- REST gate
    @app.get("/api/tools")
    async def tools() -> list[dict[str, Any]]:
        return tool_list()

    @app.post("/api/tools/{tool}")
    async def call_tool(tool: str, body: ToolIn) -> dict[str, Any]:
        sid = body.session_id or f"rest_{uuid.uuid4().hex[:10]}"
        r = await warden.call_tool(sid, body.agent_id, tool, body.args, stated_reason=body.reason, wait_for_approval=body.wait)
        return {"session_id": sid, **r}

    @app.post("/api/sessions")
    async def new_session(body: SessionIn) -> dict[str, str]:
        sid = f"sess_{uuid.uuid4().hex[:10]}"
        warden.add_task(sid, body.agent_id, body.task)
        return {"session_id": sid}

    # --------------------------------------------------------------- demo
    @app.get("/api/demo/emails")
    async def demo_emails() -> list[dict[str, Any]]:
        from demo.inbox import EMAILS, localize
        return [{"n": e.n, "sender": e.sender, "subject": localize(e.subject), "scenario": e.scenario, "summary": e.summary,
                 "html": localize(e.body_html)} for e in EMAILS]

    @app.post("/api/demo/run")
    async def demo_run(body: RunIn) -> dict[str, Any]:
        from demo.agent import make_agent
        from demo.inbox import BY_N
        if body.email not in BY_N:
            raise HTTPException(404, "unknown email")
        run_id = uuid.uuid4().hex[:8]
        agent = make_agent(body.agent, warden)
        app.state.runs[run_id] = asyncio.create_task(agent.run(body.email, run_id))
        return {"run_id": run_id, "agent": agent.name}

    @app.post("/api/reset")
    async def reset() -> dict[str, Any]:
        warden.reset()
        if "backoffice-agent" in warden.mandates:
            warden.set_mandate(warden.mandates["backoffice-agent"])
        return {"ok": True}

    @app.get("/demo/inbox", response_class=HTMLResponse)
    async def inbox() -> str:
        from demo.inbox import render_inbox
        return render_inbox(settings.public_url.rstrip("/"))

    @app.get("/demo/inbox/{n}", response_class=HTMLResponse)
    async def inbox_email(n: int) -> str:
        from demo.inbox import BY_N, render_email
        if n not in BY_N:
            raise HTTPException(404, "no such email")
        return render_email(BY_N[n])

    # ---------------------------------------------------------- webhooks
    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request) -> JSONResponse:
        event = await request.json()
        if getattr(warden.backend, "mode", "") == "sandbox" and settings.paypal_webhook_id:
            ok = await warden.backend.verify_webhook({k.lower(): v for k, v in request.headers.items()}, event, settings.paypal_webhook_id)
            if not ok:
                return JSONResponse({"ok": False, "error": "signature verification failed"}, status_code=400)
        res = event.get("resource", {}) or {}
        ref = res.get("id") or (res.get("batch_header") or {}).get("payout_batch_id") or res.get("payout_batch_id")
        settled = None
        if ref:
            for e in warden.ledger.all(1000):
                if e.paypal_ref == ref and e.status == "executed":
                    settled = warden.ledger.transition(e.id, "settled")
                    if settled:
                        warden.bus.publish("ledger", settled.model_dump(mode="json"))
                    break
        warden.bus.publish("webhook", {"event_type": event.get("event_type"), "resource_id": ref, "matched": bool(settled)})
        return JSONResponse({"ok": True, "matched": bool(settled)})

    # ------------------------------------------------------------ live
    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        q = warden.bus.subscribe()
        try:
            while True:
                msg = await q.get()
                await sock.send_text(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            warden.bus.unsubscribe(q)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"ok": "true"}

    # ------------------------------------------------------- dashboard
    if (STATIC / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = STATIC / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(STATIC / "index.html")

    return app


app = build_app()
