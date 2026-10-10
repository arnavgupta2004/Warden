# Demo video script (target 2:45, hard limit 3:00)

**Setup before recording**
- Run in sandbox mode with seeded orders, and Gemini turned on. Have the PayPal sandbox dashboard open in a second tab.
- Turn on WARDEN_NTFY_TOPIC and keep your phone ready on a desk mount or for screen mirroring.
- Click **Reset demo**. Use a browser window at 1600×1000 with the zoom at 100%.
- Use no copyrighted music. A silent recording with voice-over is fine.

| Time | On screen | Voice-over |
|---|---|---|
| 0:00–0:15 | Black title card: "Would you give an AI agent your PayPal password?" Cut to the dashboard. | "PayPal's agent tools let AI run a business's refunds, payouts and invoices. But agents read emails all day, and anything they read can give them orders. Warden is the firewall between AI agents and PayPal." |
| 0:15–0:35 | Mandate panel. Click **Edit**, show the plain-English text, then **Compile and sign**. | "Kestrel & Co. is a small shop. The owner writes the rules in plain English. Gemini compiles them into a signed policy, and a parser double-checks it, so the model can never loosen a limit." |
| 0:35–0:55 | Inbox: **Mug set arrived cracked**, then Give to agent. A green *Allowed* row appears. Switch to the PayPal sandbox tab and show the refund. | "Routine work just happens. A $42 refund: allowed, and it's a real PayPal refund." |
| 0:55–1:25 | Inbox on **Gullible agent**. **Where is my order?**, then Give to agent. Two red *Blocked* rows. Open the trace and toggle **What you see** to **What the agent read**. | "This customer email looks harmless. But it hides white text telling the agent to send $499 and cancel a $1,250 invoice. The agent fell for it. Warden didn't. The payee only exists inside an untrusted email, and the instruction came from content flagged as an injection. Nothing reached PayPal. Some models resist this trick. Warden doesn't rely on them resisting." |
| 1:25–1:45 | Switch the inbox to **Gemini agent**. **Invoice KC-118**, then Give to agent. The Agent's view shows Gemini calling `create_payout` to the new address (it takes a few seconds), then a *Blocked* lookalike-payee row. Switch back to **Gullible agent** afterwards. | "This isn't a scripted agent. It's a real AI model, and it fell for the scam. It tried to pay the lookalike address. Warden blocked it." |
| 1:45–2:10 | **Lamp base snapped**, then Give to agent. *Held for you*. The phone buzzes with the ntfy push. Tap Approve on the phone. The row turns to *Approved, paid*. | "Bigger refunds aren't blocked, they're held. The owner gets a push, sees why, and approves from their phone. The payment runs instantly." |
| 2:10–2:25 | Inbox on **Gullible agent**. **Refund for cushions**, then Give to agent. Refunds 2–4 show *Held* for splitting. The first is either *Allowed*, or *Held* by the Gemini intent check; if so, open it to show Gemini's plain-words reason. | "Limits alone aren't enough. Four $45 refunds dodge a $50 limit. Warden's split rule holds every refund after the first, every time, and Gemini can flag the first one too, just from how the request reads." |
| 2:25–2:40 | Terminal: `claude mcp add --transport http warden …`. Then the ledger chip "Ledger intact" and the AG Grid search. | "Any agent connects over MCP with one line. Every decision lands in a tamper-evident ledger." |
| 2:40–2:50 | The dashboard, with the closing line on screen. | "Warden. Let your agents use PayPal without handing them the keys." |

Tips: cut dead time between clicks. Use 1.5× zoom on the trace panel during the injection reveal; it's the moment judges will remember.
