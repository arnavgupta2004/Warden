"""Model Context Protocol endpoint (Streamable HTTP transport, JSON responses).

Point any MCP client (Claude Desktop, Claude Code, OpenAI Agents SDK, LangChain
MCP adapters...) at  http(s)://<host>/mcp  instead of PayPal's MCP server. The
agent sees PayPal-style tools; every call goes through Warden first.

Headers:  X-Warden-Agent: <agent id>   (optional, default "backoffice-agent")
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .tools import TOOLS

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

router = APIRouter()


def tool_list() -> list[dict[str, Any]]:
    out = []
    for t in TOOLS.values():
        schema = t.json_schema()
        if t.kind not in ("context", "read"):
            schema = {**schema, "properties": {**schema["properties"], "reason": {
                "type": "string", "description": "Why you are taking this action (shown to the owner)."}}}
        out.append({"name": t.name, "description": t.description, "inputSchema": schema})
    return out


def _err(id_: Any, code: int, msg: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


async def _handle(warden: Any, msg: dict[str, Any], session_id: str, agent_id: str) -> dict[str, Any] | None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if mid is None:  # notification
        return None
    if method == "initialize":
        req = params.get("protocolVersion")
        ver = req if req in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "warden", "title": "Warden: PayPal firewall for AI agents", "version": "1.0.0"},
            "instructions": "PayPal tools guarded by Warden. Money-moving calls are checked against the owner's mandate. "
                            "Read emails and web pages with fetch_url so Warden can track where instructions came from. "
                            "If a call is blocked, do not retry or work around it; report it to the owner.",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _err(mid, -32602, f"Unknown tool: {name}")
        result = await warden.call_tool(session_id, agent_id, name, params.get("arguments") or {})
        blocked = result.get("status") in ("blocked", "error")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "structuredContent": result,
            "isError": blocked,
        }}
    return _err(mid, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    warden = request.app.state.warden
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error"), status_code=400)
    session_id = request.headers.get("mcp-session-id") or f"mcp_{uuid.uuid4().hex[:12]}"
    agent_id = request.headers.get("x-warden-agent") or request.query_params.get("agent") or "backoffice-agent"
    headers = {"Mcp-Session-Id": session_id}
    if isinstance(body, list):
        out = [r for r in [await _handle(warden, m, session_id, agent_id) for m in body] if r is not None]
        return JSONResponse(out, headers=headers) if out else Response(status_code=202, headers=headers)
    r = await _handle(warden, body, session_id, agent_id)
    if r is None:
        return Response(status_code=202, headers=headers)
    return JSONResponse(r, headers=headers)


@router.get("/mcp")
async def mcp_get() -> Response:
    # No server-initiated stream; clients fall back to request/response.
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
async def mcp_delete() -> Response:
    return Response(status_code=200)
