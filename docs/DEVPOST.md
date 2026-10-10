# Devpost submission text

**Project name:** Warden

**Tagline:** A PayPal firewall for AI agents. Let agents refund, pay and invoice on their own without handing them the keys.

## Inspiration
PayPal's Agent Toolkit and MCP server make it possible for an AI agent to run a small business's payments. That's exactly the work owners want to hand off. But the agent reads customer emails, supplier invoices and web pages all day, and any of that text can try to give it orders. Payment-redirect scams (business email compromise) already cost US businesses about $3.05B in 2025 according to the FBI's IC3, and prompt injection is the top risk on OWASP's LLM list. We wanted a way to say yes to agents without giving them a blank cheque.

## What it does
Warden sits between any AI agent and PayPal and exposes the same PayPal tools over MCP and REST. The owner writes a mandate in plain English ("refund up to $50 on your own, pay Maya up to $400, never pay anyone new without asking"). Every tool call is then checked against five layers:
1. **The signed mandate:** scope, caps, budget, trusted payees, velocity.
2. **Live PayPal facts:** does the capture exist, how much is still refundable, is it disputed?
3. **Provenance:** where did this payee or id first appear? Content the agent reads is tracked as untrusted. Hidden text and instruction-like phrases quarantine the session, and lookalike payees are caught.
4. **Composition rules** across calls: split payments, cancelling records after moving money, scanning transactions then paying a new payee.
5. **A Gemini intent check:** would the owner want this exact action?

Allowed actions execute on PayPal. Held actions go to the owner's phone and run on approval. Blocked actions never reach PayPal. Everything is written to a hash-chained ledger shown live in an AG Grid dashboard, and PayPal webhooks mark payments as settled.

## How we built it
- **Backend:** Python and FastAPI. A real PayPal REST client covers Payments v2 (refunds and captures), Orders v2 (including shipment tracking), Payouts v1, Invoicing v2, Disputes v1, Transaction Search and webhook verification. An offline simulator returns the same response shapes so judges can run it without keys.
- **MCP:** a Streamable HTTP server, verified with the official MCP Python SDK. Any agent swaps one URL.
- **AI:** Gemini compiles mandates (cross-checked by a deterministic parser, where the stricter value wins), runs intent checks, explains decisions, drafts dispute evidence, and powers the live demo agent (a Gemini function-calling loop). Gemini can only make a decision stricter, never looser.
- **Frontend:** React and AG Grid (Theming API, custom cell renderers, quick filter, row rules), with a mobile approval page and ntfy push notifications.
- **Deploy:** one Docker image on Render.
- 41 automated tests cover every scenario, the MCP protocol, request shapes for the PayPal client, and the Gemini code paths.

## Challenges
- Telling a legitimate order id in a customer email apart from an attacker's payee in the same email. Warden weighs arguments by role: refunds can only return to the original payer, so an order id from an email is low risk, while a payout recipient from an email is critical.
- Catching attacks that are made of individually allowed steps, which led to the composition rules.
- Keeping the LLM useful without letting it weaken any policy.

## Accomplishments
- A hidden white-text injection and a payment-redirect scam are both blocked before reaching PayPal, while routine refunds and dispute responses still run without a human.
- One-line integration for any MCP agent.

## What we learned
Agent safety for payments is mostly about provenance: knowing *who* asked for a payment matters more than how big it is.

## What's next
Per-agent mandates for teams, PayPal Payouts batch limits, and support for buyer-side agentic checkout (Orders v2 with vaulted payment methods) under the same policy engine.

## Built with
python, fastapi, paypal-rest-api, paypal-payouts, paypal-invoicing, paypal-disputes, model-context-protocol, gemini, gemini-api, react, typescript, ag-grid, vite, sqlite, docker, render, ntfy

## Testing instructions for judges
- **Hosted demo:** <your Render URL>. Click **Give to agent** on any inbox email.
- **Locally:** see the README Quick start. It runs with no keys using the PayPal simulator. Add sandbox keys and run `scripts/seed_sandbox.py` to use the real PayPal sandbox.
