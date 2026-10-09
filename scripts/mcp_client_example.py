"""Connect to Warden with the official MCP Python SDK, exactly as an agent
framework would, and try one safe and one malicious payment.

    pip install mcp
    python scripts/mcp_client_example.py http://localhost:8000/mcp
"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str) -> None:
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo")
            print("connected to", info.name)
            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            ok = await session.call_tool("create_payout", {
                "receiver_email": "maya@chenstudio.example", "amount": 120, "note": "Invoice KC-117",
                "reason": "Pay trusted contractor"})
            print("\ntrusted contractor payout ->", json.loads(ok.content[0].text)["status"])

            bad = await session.call_tool("fetch_url", {"url": url.rsplit("/mcp", 1)[0] + "/demo/inbox/3"})
            print("read support email #3 ->", json.loads(bad.content[0].text)["status"])
            bad = await session.call_tool("create_payout", {
                "receiver_email": "refunds-desk@kestrel-support.example", "amount": 499, "reason": "system note"})
            body = json.loads(bad.content[0].text)
            print("injected payout ->", body["status"], "|", body["warden"]["explanation"])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"))
