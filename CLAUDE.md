# Warden: notes for Claude Code

PayPal AI Hackathon entry (Devpost; submissions close 13 Nov 2026, 1:30 AM IST / 12 Nov noon PT).
Warden is a policy gateway between AI agents and PayPal: provenance tracking + mandate + composition rules + Gemini intent check, exposed over MCP and REST, with an AG Grid dashboard.

## Run
- Backend: `cd backend && pip install -r requirements-dev.txt && uvicorn warden.app:app --reload --port 8000`
- Frontend: `cd frontend && npm install && npm run dev` (proxies /api, /ws, /mcp, /demo to :8000). `npm run build` writes to `backend/static`, which the backend serves.
- Tests: `cd backend && pytest` (52 tests, about 4 s). Keep them green.
- No PAYPAL_* env → offline simulator (`warden/paypal/simulator.py`). With PAYPAL_CLIENT_ID/SECRET → real sandbox (`warden/paypal/sandbox.py`).
- No GEMINI_API_KEY → every LLM call falls back to deterministic behavior. Gemini may only make decisions stricter. All model calls go through `warden/llm.py` (Gemini REST via httpx).

## Map
- `backend/warden/policy.py`: `PolicyEngine.evaluate` returns a Decision. Findings carry `effect`; the strongest wins (DENY > STEP_UP > ALLOW). New rules go here or in `_composition`.
- `backend/warden/provenance.py`: spans with Trust (PRINCIPAL/VERIFIED/UNTRUSTED) and the injection scanner.
- `backend/warden/gateway.py`: `Warden.call_tool` is the single entry point. Handles approvals (waits up to WARDEN_APPROVAL_TIMEOUT), execution, and ntfy/webhook notifications.
- `backend/warden/tools.py`: tool registry. Names mirror PayPal's Agent Toolkit where possible. A new tool needs a `t_<name>` method in both PayPal backends.
- `backend/warden/ledger.py`: `entries` (current state) plus the `chain` table (append-only, hash-chained).
- `backend/warden/mcp.py`: hand-rolled MCP Streamable HTTP (JSON responses). Verified with the official `mcp` SDK via `scripts/mcp_client_example.py`.
- `backend/demo/`: fictional Kestrel & Co. inbox (6 scenario emails) and the ScriptedAgent/GeminiAgent.
- `frontend/src/`: `App.tsx` layout, `LedgerGrid.tsx` (AG Grid v34, Theming API), `DecisionTrace.tsx` (evidence view with the hidden-text reveal), `Panels.tsx`, and the `/approve/:id` mobile page.

## Conventions
- UI copy: plain, sentence case, from the shop owner's point of view ("Held for you", "Blocked"). No all-caps labels.
- Colors live in CSS variables in `styles.css` (light and dark).
- Demo content is fictional (".example" domains). Don't use real brands or people.

## Open to-dos before submitting
1. Run `scripts/seed_sandbox.py` against a real sandbox app and click through all six emails in sandbox mode. Fix any API-shape surprises in `paypal/sandbox.py`. Dispute evidence (multipart) and Payouts permissions are the likeliest ones.
2. Deploy to Render (`render.yaml`). Set WARDEN_PUBLIC_URL, the PayPal keys, GEMINI_API_KEY and WARDEN_NTFY_TOPIC.
3. Record the video from `docs/DEMO_SCRIPT.md`. Fill in `docs/DEVPOST.md` and submit.
4. Make the GitHub repo public, with the license showing in the About sidebar.
