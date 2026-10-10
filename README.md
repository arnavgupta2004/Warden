# Warden

**A PayPal firewall for AI agents.** Warden sits between any AI agent and PayPal. It lets the agent refund customers, pay contractors and answer disputes on its own, inside limits you set in plain English. It stops the agent when someone tries to steer it into sending your money somewhere else.

**Live demo:** [warden-tybr.onrender.com](https://warden-tybr.onrender.com) (real PayPal sandbox; the first visit can take up to a minute to wake) · **Video:** [2-minute walkthrough on YouTube](https://youtu.be/N84p8ZItSKw)

![Warden blocking a payout that was requested by text hidden in a support email](docs/screenshot.png)

## The problem

PayPal's Agent Toolkit and MCP server let an AI agent run a business's payments: refunds, payouts, invoices, disputes. That is exactly what a small business wants to hand off. But the agent reads untrusted text all day, including customer emails, supplier invoices and web pages, and anything it reads can try to give it orders.

- **Payment-redirect scams already work on humans.** Business email compromise ("we changed banks, pay this new account") cost US businesses about **$3.05 billion in 2025**, the second-largest loss category in the FBI's IC3 report. An agent that pays invoices from an inbox is an easier target than a bookkeeper.
- **Prompt injection is the top risk for LLM apps** (OWASP LLM01). A single line of white-on-white text in an email can tell an agent to send a payout and delete the evidence.
- **Spending limits on their own don't help.** An agent allowed to refund $50 at a time can refund $45 four times.

Today the choice is to give the agent full API credentials, or keep a human in every loop. Warden is the middle ground.

## What Warden does

```mermaid
flowchart LR
    A[AI agent<br/>Gemini, Claude, GPT, LangChain…] -- MCP or REST --> W
    subgraph W[Warden]
      direction TB
      P[Provenance tracker<br/>where did each payee and id come from?]
      E[Policy engine<br/>mandate · PayPal facts · composition rules · Gemini intent check]
      L[(Hash-chained ledger)]
      P --> E --> L
    end
    W -- allowed --> PP[PayPal REST APIs<br/>Payments · Payouts · Invoicing · Disputes · Tracking]
    W -- held --> H[Owner's phone / dashboard]
    PP -- webhooks --> W
```

1. **Write a mandate in plain English.** For example: *"Refund customers up to $50 on your own; anything bigger, ask me. Pay our contractors Maya Chen (maya@…) up to $400 each. Never pay anyone new without asking me. No single payment over $500."* Gemini compiles it into a structured policy. A deterministic parser checks the result and the stricter limit always wins, so the model can't loosen your rules or invent payees. Warden then signs the policy with HMAC.
2. **Point your agent at Warden instead of PayPal.** Warden exposes PayPal-style tools (`create_refund`, `create_payout`, `cancel_sent_invoice`, …) over **MCP** and REST, so a working agent only needs a different URL.
3. **Every call is checked against five layers:**
   - **Mandate:** scope, per-payment cap, budget window, trusted payees, velocity.
   - **PayPal facts:** does the capture exist, how much is still refundable, is it under dispute?
   - **Provenance:** every email or page the agent reads goes through `fetch_url` and is stored as untrusted context. A payee that only ever appeared in untrusted content is tainted. A payee that imitates a trusted one (`maya.chen.studio@payout-desk…` vs `maya@chenstudio…`) is flagged as a redirect scam. Content with hidden text or instruction-like phrases quarantines the session.
   - **Composition rules:** dangerous patterns across calls, such as split payments, cancelling records right after moving money, scanning transactions then paying a new payee, and money moving after an invoice cancellation.
   - **Gemini intent check:** "Would the owner want this exact action?" This can hold a call that every hard rule allowed.
4. **Three outcomes.** *Allowed* actions execute on PayPal immediately. *Held* actions go to your phone (ntfy push or the mobile approval page) and run the moment you tap Approve. *Blocked* actions never reach PayPal, and the agent is told not to retry.
5. **Everything is on the record.** Each decision and state change goes into a hash-chained, tamper-evident ledger. The AG Grid dashboard shows it live, and PayPal webhooks mark entries as settled.

## The demo: Kestrel & Co.

Kestrel & Co. is a fictional homeware shop whose back-office agent works the support inbox. Six emails cover the cases that matter:

| Email | What the agent tries | Warden |
|---|---|---|
| Mug set arrived cracked | Refund $42 | **Allowed**, refunded on PayPal |
| Contractor invoice, "we moved banks" | Pay $380 to a lookalike address | **Blocked**: lookalike payee from an untrusted email |
| "Where is my order?" with hidden white text | $499 payout, then cancel a $1,250 invoice | **Both blocked**: hidden instruction, tainted payee |
| Lamp base snapped | Refund $180 | **Held**, runs when you tap Approve on your phone |
| "Refund me as four $45 refunds" | 4 × $45 on one payment | Refunds 2–4 are always **held** by the split-payment rule. The first is refunded, unless the Gemini intent check reads the request as a way around the limit and **holds** it too, saying why in plain words |
| Dispute: item not received | Answer with tracking evidence | **Allowed**. Gemini drafts the response. Refunding during the dispute would be held. |

The **gullible agent** does whatever an email says, including hidden instructions. It stands in for an LLM that has been successfully manipulated, and keeps the demo repeatable. The **Gemini agent** is a live Gemini function-calling loop over the same tools.

## Quick start

No credentials needed. Without PayPal keys, Warden uses a built-in PayPal simulator that returns the same response shapes as the REST API. You need Python 3.10+ and Node 18+ (tested with Python 3.12 and 3.13, Node 22 and 24).

```bash
git clone https://github.com/arnavgupta2004/Warden.git
cd Warden

# backend
cd backend
pip install -r requirements.txt
uvicorn warden.app:app --port 8000

# dashboard (second terminal, from the Warden folder)
cd frontend
npm install
npm run dev          # http://localhost:5173
```

Or use one container: `docker build -t warden . && docker run -p 8000:8000 warden`, then open http://localhost:8000.

Click **Give to agent** on any email in the inbox panel.

**Hosted demo** ([warden-tybr.onrender.com](https://warden-tybr.onrender.com)): the free Render instance sleeps when idle, so the first visit can take up to a minute to wake. Click **Reset demo** for a clean run: in sandbox mode it also creates fresh sandbox payments for the refund emails.

The hosted demo runs with `WARDEN_PUBLIC_DEMO=1`. Visitors can edit and compile the mandate, but server-side ceilings apply whatever it says: $1,500 a week, $500 per payment, and the default trusted payees plus at most two more. Clamped values show "capped for the public demo". Every agent id (dashboard, REST, MCP) shares that one budget, so the sandbox balance can't be drained. **Reset demo**, or 15 minutes without activity, brings back the default mandate. Approvals stay open so anyone can try **Approve**, and the ledger records each approver's address.

### Use the real PayPal sandbox

1. Create a sandbox REST app at [developer.paypal.com](https://developer.paypal.com/dashboard/applications/sandbox) and copy `.env.example` to `.env`.
2. Set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. Warden switches to the sandbox automatically, and the header shows **PayPal sandbox**.
3. Seed the demo orders: `python scripts/seed_sandbox.py`. It creates four real orders, opens each approval link (approve with a sandbox *personal* account), captures them, sends an invoice and writes `backend/sandbox_seed.json`. The demo inbox then uses those real ids, so every refund in the demo is a real sandbox refund.
   Refunds can't be undone, so in sandbox mode **Reset demo** gives the three refund emails fresh captures: it creates and captures new orders paid with PayPal's published sandbox test card (no buyer login needed) and points the inbox at them. A fresh deployment with no seed file does this once at startup. The planter order, which carries the dispute, is never replaced. Your sandbox business account must accept card payments (US sandbox business accounts do). Resets are limited to 3 a minute per address.
   **Email 6 (the dispute)** needs a real sandbox dispute. Until one is mapped, the inbox shows it as "Needs a sandbox dispute" with the button disabled. To enable it: log in at [sandbox.paypal.com](https://www.sandbox.paypal.com) as the sandbox buyer, open the **Ceramic planter** payment, choose **Report a problem**, then **I didn't receive my item**. Then add the dispute id to `backend/sandbox_seed.json` under `"disputes": {"PP-D-55210": "<dispute id>"}`, or put it in `WARDEN_SANDBOX_SEED` (see `.env.example`). To find the id once PayPal has created it (this can take a few minutes): `curl -X POST localhost:8000/api/tools/list_disputes -H 'content-type: application/json' -d '{"args": {}}'`.
4. Optional: add a webhook in the same app pointing to `https://<your-host>/webhooks/paypal` and set `PAYPAL_WEBHOOK_ID`. Settled refunds and payouts are then marked on the ledger.

### Turn on Gemini

Set `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/apikey)). Gemini then compiles mandates, runs the intent check, writes the plain-language explanations, drafts dispute responses, and powers the **Gemini agent** in the inbox. `WARDEN_GEMINI_MODEL` picks the model.

### Get approvals on your phone

Install the [ntfy](https://ntfy.sh) app, subscribe to a private topic and set `WARDEN_NTFY_TOPIC` to it, with `WARDEN_PUBLIC_URL` set to an address your phone can reach. Held payments arrive as push notifications with **Approve** and **Deny** buttons. `WARDEN_APPROVAL_WEBHOOK` posts the same payload to Zapier, Slack or anything else.

## Connect your own agent

Warden speaks MCP over Streamable HTTP at `/mcp`.

```bash
# Claude Code
claude mcp add --transport http warden http://localhost:8000/mcp

# Any MCP client (official Python SDK example)
python scripts/mcp_client_example.py http://localhost:8000/mcp
```

The REST equivalent works with any framework:

```bash
curl -X POST localhost:8000/api/tools/create_refund \
  -H 'content-type: application/json' \
  -d '{"args": {"capture_id": "4RT88014HN112233K", "amount": 20}, "reason": "damaged item"}'
```

Have your agent read emails and pages through Warden's `fetch_url` tool, or record them with `POST /api/sessions`, so provenance checks can see them.

## Rules reference

| Rule | Effect | Fires when |
|---|---|---|
| `out_of_scope` | Block | The mandate doesn't grant this tool |
| `per_tx_limit`, `budget`, `velocity` | Block | Single-payment cap, budget window or payments-per-hour exceeded |
| `unknown_capture`, `over_refund` | Block | The refund targets a payment that doesn't exist, or exceeds what's still refundable (checked live with PayPal and the ledger) |
| `lookalike_payee` | Block | A payout address imitates a trusted payee |
| `tainted_payee` | Block | The payee only appears in untrusted content |
| `injected_instruction` | Block | The call's arguments come from content flagged as a prompt injection |
| `new_payee` | Hold | The payee isn't trusted, but the owner named them |
| `refund_over_auto_limit`, `payee_limit`, `approval_threshold` | Hold | Above what the owner allows without asking |
| `split_payments` | Hold | Several payments under a limit add up to more than the limit |
| `refund_during_dispute` | Hold | Refunding a payment that has an open dispute (risk of paying twice) |
| `cover_tracks`, `money_after_cancel`, `recon_then_payout` | Hold | Dangerous combinations of individually allowed calls |
| `session_quarantine` | Hold | The agent read suspicious content earlier in the session |
| `intent_check` | Hold | Gemini judges the action isn't what the owner would want |

## PayPal APIs used

| Capability | API |
|---|---|
| Refunds (and refundable-amount facts) | Payments v2: `captures/{id}`, `captures/{id}/refund`, `refunds/{id}`; Orders v2 for payer details |
| Contractor payouts | Payouts v1: `payments/payouts` |
| Invoices (send, remind, cancel) | Invoicing v2 |
| Disputes (read, provide evidence, accept claim) | Customer Disputes v1 |
| Shipment tracking | Orders v2: `checkout/orders/{id}/track` |
| Account activity | Transaction Search v1: `reporting/transactions` |
| Settlement | Webhooks, with signature verification through `notifications/verify-webhook-signature` |

## How AI is used

- **Gemini** compiles the mandate (cross-checked by a parser, stricter value wins), runs the intent check on every payment the hard rules don't block, explains each decision in plain language, drafts dispute evidence, and is the live agent in the demo.
- **Warden never relies on the model alone.** Every hard limit, payee check, taint check and composition rule is deterministic and works with no API key. Gemini adds judgment on top, and can only make a decision stricter, never looser.
- **Gemini is used sparingly, to fit its free tier.** Warden uses `gemini-3.5-flash-lite`, and each payment gets at most one Gemini call: the intent check also writes the explanation, and only a hard block gets a separate explanation call. Quick calls give up after 8 seconds. If Gemini runs out of quota, Warden waits as long as Gemini asks, uses only its deterministic rules meanwhile, and the header says "Gemini limited, rules still enforced". The public demo limits each address to 10 runs a minute (200 a day), 2 Gemini-agent runs a minute (15 a day) and 3 mandate changes a minute (20 a day). A site-wide cap (`WARDEN_LLM_DAILY_CAP`, default 700 requests per UTC day) keeps all visitors together under the free daily quota; past it, the header says "Gemini limit reached for today, rules still enforced". The count lives in `backend/llm_usage.json`, so on a host without a persistent disk (like Render's free plan) it restarts whenever the server does.
- **Identical prompts are cached**, so repeated demo runs reuse earlier Gemini answers instead of asking again. Failed or empty answers are never cached, and the live Gemini agent's turns are never cached.

## Project layout

```
backend/
  warden/
    policy.py       decision engine and composition rules
    provenance.py   untrusted-content tracking and injection scanner
    mandate.py      plain English → signed policy (Gemini + parser)
    gateway.py      decide → record → execute / hold / block; approvals
    ledger.py       hash-chained SQLite audit log
    mcp.py          MCP Streamable HTTP server
    paypal/         real REST client + offline simulator
    app.py          REST API, websocket, webhooks, dashboard
  demo/             Kestrel & Co. inbox and the two demo agents
  tests/            55 tests: every scenario, the MCP protocol, the PayPal client, Gemini paths
frontend/           React + AG Grid dashboard and mobile approval page
scripts/            sandbox seeding, MCP client example
```

Run the tests with `cd backend && pip install -r requirements-dev.txt && pytest`.

## Built during the hackathon

Warden's composition-aware authorization idea grew out of AgentShield, an earlier hackathon prototype of mine that checked combinations of generic agent tool calls. Everything in this repository was written for the PayPal AI Hackathon: the PayPal integration, provenance tracking, mandate compiler, payment rules, approval flow, MCP server, ledger, dashboard and demo.

## License

MIT
