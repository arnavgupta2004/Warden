"""HTTP surface: MCP endpoint, REST gate, dashboard API, live events, PayPal
webhooks, the demo inbox, and the built dashboard."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
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
from .mandate import DEFAULT_MANDATE, compile_mandate
from .mcp import router as mcp_router, tool_list
from .paypal import make_backend

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

log = logging.getLogger("warden.app")
PUBLIC_IDLE_REVERT_S = 15 * 60


class RateLimiter:
    """Sliding-window limit per client IP. Keeps the public demo from burning the Gemini quota."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window_s = limit, window_s
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def full(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        return len(q) >= self.limit

    def hit(self, key: str) -> None:
        self.hits[key].append(time.monotonic())


def client_ip(request: Request) -> str:
    """CF-Connecting-IP (set by the CDN in front of Render), else the rightmost
    X-Forwarded-For hop (added by the nearest proxy, not the client), else the socket."""
    if ip := request.headers.get("cf-connecting-ip"):
        return ip.strip()
    if xff := request.headers.get("x-forwarded-for"):
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _limit(limiters: list[RateLimiter], request: Request, what: str) -> None:
    """Count one request against every window; refuse (without counting) if any is full."""
    ip = client_ip(request)
    for lim in limiters:
        if lim.full(ip):
            when = "in a minute" if lim.window_s <= 60 else "tomorrow"
            raise HTTPException(429, f"Too many {what} from your address. Try again {when}.",
                                headers={"Retry-After": str(int(lim.window_s))})
    for lim in limiters:
        lim.hit(ip)


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


def _load_known_disputes(warden: Warden) -> None:
    """Tell the sandbox client which disputes the demo seed maps, for its open-dispute check."""
    from demo.inbox import _read_seed
    if getattr(warden.backend, "mode", "") == "sandbox":
        warden.backend.known_disputes = set(_read_seed()["disputes"].values())


async def _ensure_sandbox_captures(warden: Warden) -> None:
    """On a fresh sandbox deployment (no seed file), create the refund scenarios' captures once."""
    from demo.inbox import has_refund_captures, refresh_refund_captures
    _load_known_disputes(warden)
    if getattr(warden.backend, "mode", "") != "sandbox" or has_refund_captures():
        return
    try:
        await refresh_refund_captures(warden.backend)
        log.info("Created fresh sandbox captures for the refund scenarios.")
    except Exception as exc:  # the dashboard still works; refunds will show as unknown captures
        log.warning("Could not create sandbox captures: %s", exc)


def build_app(warden: Warden | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not app.state.warden.mandates:
            app.state.warden.set_mandate(await compile_mandate(DEFAULT_MANDATE))
        await _ensure_sandbox_captures(app.state.warden)
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
    # Per-address limits, sized so one visitor can't spend the Gemini free tier (assumed ~15 RPM / ~1,000 RPD
    # for flash-lite; the real numbers show up on /api/status after the first 429). Gullible-agent runs mostly
    # hit the response cache; Gemini-agent runs are uncached (~3-8 calls each) and mandate edits cost one call.
    app.state.run_limiters = [RateLimiter(10), RateLimiter(200, 86400)]
    app.state.gemini_run_limiters = [RateLimiter(2), RateLimiter(15, 86400)]
    app.state.mandate_limiters = [RateLimiter(3), RateLimiter(20, 86400)]
    app.state.reset_limiters = [RateLimiter(3)]  # each sandbox reset creates three PayPal orders
    app.state.last_activity = time.monotonic()  # last POST; drives the public demo's mandate auto-revert

    async def restore_default_mandate() -> None:
        warden.set_mandate(await compile_mandate(DEFAULT_MANDATE))

    @app.middleware("http")
    async def public_demo_revert(request: Request, call_next):
        """Public demo: put back the default mandate after PUBLIC_IDLE_REVERT_S without any POST."""
        if settings.public_demo:
            now = time.monotonic()
            m = warden.mandates.get("backoffice-agent")
            if m and m.source_text != DEFAULT_MANDATE and now - app.state.last_activity > PUBLIC_IDLE_REVERT_S:
                await restore_default_mandate()
                log.info("Public demo: mandate edit reverted after inactivity.")
            if request.method == "POST":
                app.state.last_activity = now
        return await call_next(request)
    app.include_router(mcp_router)

    # ------------------------------------------------------------- status
    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        m = warden.mandates.get("backoffice-agent")
        return {
            "paypal_mode": getattr(warden.backend, "mode", "unknown"),
            "llm": llm.enabled(),
            "llm_limited": llm.enabled() and llm.limited(),
            "llm_capped": llm.enabled() and llm.capped(),
            "llm_calls_today": llm.calls_today(),
            "llm_quota": llm.quota_info() or None,
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
        spent = warden.ledger.spent(None if settings.public_demo else agent_id, _t.time() - since_map[m.budget_window])
        return {"mandate": m.model_dump(mode="json"), "spent": spent}

    @app.post("/api/mandate")
    async def set_mandate(body: MandateIn, request: Request) -> dict[str, Any]:
        _limit(app.state.mandate_limiters, request, "mandate changes")
        if not body.text.strip():
            raise HTTPException(400, "empty mandate")
        # The public demo has one agent and one budget: edits can't create mandates for other agent ids.
        m = await compile_mandate(body.text, "backoffice-agent" if settings.public_demo else body.agent_id)
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
        if settings.public_demo:  # approvals stay open to judges; record where each decision came from
            by = f"{by} ({client_ip(request)})"
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
        from demo.inbox import EMAILS, localize, unavailable_reason
        return [{"n": e.n, "sender": e.sender, "subject": localize(e.subject), "scenario": e.scenario, "summary": e.summary,
                 "html": localize(e.body_html), "unavailable": unavailable_reason(e)} for e in EMAILS]

    @app.post("/api/demo/run")
    async def demo_run(body: RunIn, request: Request) -> dict[str, Any]:
        _limit(app.state.gemini_run_limiters if body.agent == "gemini" else app.state.run_limiters, request,
               "Gemini agent runs" if body.agent == "gemini" else "demo runs")
        from demo.agent import make_agent
        from demo.inbox import BY_N, unavailable_reason
        if body.email not in BY_N:
            raise HTTPException(404, "unknown email")
        if reason := unavailable_reason(BY_N[body.email]):
            raise HTTPException(409, f"{reason}. See the README to set one up.")
        run_id = uuid.uuid4().hex[:8]
        agent = make_agent(body.agent, warden)
        app.state.runs[run_id] = asyncio.create_task(agent.run(body.email, run_id))
        return {"run_id": run_id, "agent": agent.name}

    @app.post("/api/reset")
    async def reset(request: Request) -> dict[str, Any]:
        _limit(app.state.reset_limiters, request, "resets")
        warden.reset()
        if settings.public_demo:
            await restore_default_mandate()  # a reset always brings back the owner's original rules
        elif "backoffice-agent" in warden.mandates:
            warden.set_mandate(warden.mandates["backoffice-agent"])
        out: dict[str, Any] = {"ok": True}
        if getattr(warden.backend, "mode", "") == "sandbox":
            # Refunds can't be undone on PayPal, so each reset gets fresh captures for the refund emails.
            from demo.inbox import refresh_refund_captures
            try:
                out["fresh_captures"] = await refresh_refund_captures(warden.backend)
            except Exception as exc:
                out["fresh_captures_error"] = str(exc)
        return out

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
