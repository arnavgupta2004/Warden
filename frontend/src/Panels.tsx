import { useEffect, useRef, useState } from "react";
import { AgentLine, Approval, DemoEmail, Finding, Mandate, Status, TOOL_LABEL, api, money } from "./api";

export function Header({ status, connected, onReset }: { status: Status | null; connected: boolean; onReset: () => void }) {
  const mode = status?.paypal_mode === "sandbox" ? "PayPal sandbox" : "PayPal simulator";
  return (
    <header className="top">
      <div className="brand">
        <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden>
          <path d="M16 2 4 7v8c0 7.5 5 13 12 15 7-2 12-7.5 12-15V7z" fill="currentColor" />
          <path d="m11 16 4 4 7-8" stroke="var(--paper)" strokeWidth="3" fill="none" />
        </svg>
        <div>
          <div className="wordmark">Warden</div>
          <div className="tagline">Lets AI agents use your PayPal account without handing them the keys</div>
        </div>
      </div>
      <div className="chips">
        <span className={`chip ${status?.paypal_mode === "sandbox" ? "chip-ok" : ""}`} title="Where payments are sent">{mode}</span>
        <span
          className={`chip ${status?.llm && !status.llm_limited && !status.llm_capped ? "chip-ok" : ""}`}
          title={
            status?.llm_capped
              ? "This demo used its Gemini budget for today. Every hard rule still applies; Gemini's checks resume tomorrow (UTC)."
              : status?.llm_limited
                ? "Gemini hit its usage limit. Every hard rule still applies; Gemini's extra checks resume shortly."
                : status?.llm_model ?? "Set GEMINI_API_KEY to enable"
          }
        >
          {!status?.llm
            ? "Gemini off"
            : status.llm_capped
              ? "Gemini limit reached for today, rules still enforced"
              : status.llm_limited
                ? "Gemini limited, rules still enforced"
                : "Gemini on"}
        </span>
        <span className={`chip ${status?.chain.ok ? "chip-ok" : "chip-bad"}`} title={status?.chain.tip ?? ""}>
          {status ? (status.chain.ok ? `Ledger intact, ${status.chain.records} records` : "Ledger tampered") : "Ledger"}
        </span>
        <span className={`live ${connected ? "on" : ""}`} aria-label={connected ? "Live" : "Reconnecting"}>{connected ? "Live" : "Reconnecting"}</span>
        <button className="ghost" onClick={onReset}>Reset demo</button>
      </div>
    </header>
  );
}

export function MandatePanel({ mandate, spent }: { mandate: Mandate | null; spent: number }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  if (!mandate) return <section className="panel"><h3>Mandate</h3><p className="muted">Loading…</p></section>;
  const pct = mandate.budget_total ? Math.min(100, (spent / mandate.budget_total) * 100) : 0;
  const save = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.setMandate(text);
      setEditing(false);
    } catch (e) {
      setErr((e as { status?: number }).status === 429
        ? String((e as { detail?: string }).detail)
        : "Warden couldn't compile that. Check the limits are written as amounts, like $50.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel mandate">
      <div className="panel-head">
        <h3>Mandate</h3>
        {!editing && (
          <button className="link" onClick={() => { setText(mandate.source_text); setEditing(true); }}>Edit</button>
        )}
      </div>
      {editing ? (
        <div className="mandate-edit">
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={8} aria-label="Mandate in your own words" />
          {err && <p className="error">{err}</p>}
          <div className="row">
            <button className="primary" onClick={save} disabled={busy}>{busy ? "Compiling…" : "Compile and sign"}</button>
            <button className="ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <blockquote className="owner-words">{mandate.source_text}</blockquote>
      )}
      <div className="budget">
        <div className="budget-line">
          <span>Spent this {mandate.budget_window}</span>
          <span className="tabular">{money(spent)} of {money(mandate.budget_total)}</span>
        </div>
        <div className="meter" role="meter" aria-valuenow={spent} aria-valuemax={mandate.budget_total}>
          <span style={{ width: `${pct}%` }} />
        </div>
      </div>
      <dl className="rules">
        {mandate.refund_auto_max != null && (<><dt>Refunds without asking</dt><dd>up to {money(mandate.refund_auto_max)}</dd></>)}
        {mandate.auto_approve_below > 0 && (<><dt>Ask above</dt><dd>{money(mandate.auto_approve_below)}</dd></>)}
        <dt>Single payment cap</dt><dd>{money(mandate.per_tx_max)}</dd>
        <dt>New payees</dt><dd>{mandate.new_payee_requires_approval ? "need your approval" : "allowed"}</dd>
      </dl>
      <div className="payees">
        <div className="sub">Trusted payees</div>
        {mandate.trusted_payees.length === 0 && <p className="muted">None. Every payout needs approval.</p>}
        {mandate.trusted_payees.map((p) => (
          <div className="payee" key={p.email}>
            <span className="payee-name">{p.name}</span>
            <span className="payee-email">{p.email}</span>
            {p.max_per_payment && <span className="payee-cap tabular">≤ {money(p.max_per_payment)}</span>}
          </div>
        ))}
      </div>
      <p className="sig" title={mandate.signature}>
        Compiled by {mandate.compiled_by === "rules" ? "Warden's parser" : "Gemini, checked by Warden's parser"}. Signed {mandate.signature.slice(0, 10)}.
      </p>
    </section>
  );
}

const SCENARIO_HINT: Record<string, string> = {
  refund: "Routine",
  bec: "Scam",
  injection: "Hidden attack",
  step_up: "Over limit",
  split: "Split refund",
  dispute: "Routine",
};

export function InboxPanel({ emails, agent, setAgent, llm }: { emails: DemoEmail[]; agent: string; setAgent: (a: string) => void; llm: boolean }) {
  const [running, setRunning] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const run = async (n: number) => {
    setRunning(n);
    setNote("");
    try {
      await api.run(n, agent);
    } catch (e) {
      setNote((e as { status?: number }).status === 429 ? String((e as { detail?: string }).detail) : "Couldn't start the agent.");
    } finally {
      window.setTimeout(() => setRunning(null), 900);
    }
  };
  return (
    <section className="panel inbox">
      <div className="panel-head">
        <h3>Kestrel &amp; Co. inbox</h3>
        <div className="seg small" role="group" aria-label="Which agent works the inbox">
          <button className={agent === "scripted" ? "on" : ""} onClick={() => setAgent("scripted")} title="Deterministic agent that obeys whatever it reads">Gullible agent</button>
          <button className={agent === "gemini" ? "on" : ""} onClick={() => setAgent("gemini")} disabled={!llm} title={llm ? "Live Gemini function-calling agent" : "Needs GEMINI_API_KEY"}>Gemini agent</button>
        </div>
      </div>
      <p className="muted small-print">A fictional shop. Hand an email to the agent and watch what reaches PayPal.</p>
      {note && <p className="small-print" role="status">{note}</p>}
      <ul className="emails">
        {emails.map((e) => (
          <li key={e.n}>
            <div className="email-meta">
              <div className="email-subject">{e.subject}</div>
              <div className="email-from">{e.sender.replace(/<.*>/, "").trim()} <span className={`hint hint-${e.scenario}`}>{SCENARIO_HINT[e.scenario]}</span></div>
            </div>
            <button className="run" onClick={() => run(e.n)} disabled={running === e.n}>
              {running === e.n ? "Sent" : "Give to agent"}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function FindingList({ findings }: { findings: Finding[] }) {
  const shown = findings.filter((f) => f.effect !== "allow");
  return (
    <ul className="mini-findings">
      {shown.map((f, i) => (
        <li key={i}>{f.message}</li>
      ))}
    </ul>
  );
}

export function ApprovalCard({ a, big = false }: { a: Approval; big?: boolean }) {
  const [busy, setBusy] = useState(false);
  const decide = async (ok: boolean) => {
    setBusy(true);
    try {
      await api.decide(a.id, ok, big ? "phone" : "dashboard");
    } finally {
      setBusy(false);
    }
  };
  const d = a.decision;
  return (
    <article className={`approval ${big ? "approval-big" : ""} approval-${a.status}`}>
      <div className="approval-amount tabular">{d.money > 0 ? money(d.money) : TOOL_LABEL[a.call.tool]}</div>
      <div className="approval-what">
        {TOOL_LABEL[a.call.tool] ?? a.call.tool}
        {d.counterparty && <> to <b>{d.counterparty}</b></>}
      </div>
      {a.call.stated_reason && <p className="approval-reason">Agent says: “{a.call.stated_reason}”</p>}
      <FindingList findings={d.findings} />
      {a.status === "pending" ? (
        <div className="row">
          <button className="primary approve" disabled={busy} onClick={() => decide(true)}>Approve</button>
          <button className="ghost deny" disabled={busy} onClick={() => decide(false)}>Decline</button>
        </div>
      ) : (
        <p className="approval-done">{a.status === "approved" ? `Approved by ${a.decided_by}` : `Declined by ${a.decided_by}`}</p>
      )}
    </article>
  );
}

export function ApprovalsPanel({ approvals }: { approvals: Approval[] }) {
  const pending = approvals.filter((a) => a.status === "pending");
  const done = approvals.filter((a) => a.status !== "pending").slice(0, 3);
  return (
    <section className="panel approvals">
      <div className="panel-head">
        <h3>Waiting for you {pending.length > 0 && <span className="count">{pending.length}</span>}</h3>
      </div>
      {pending.length === 0 && <p className="muted">Nothing to approve. Actions over your limits will appear here and on your phone.</p>}
      {pending.map((a) => (
        <ApprovalCard key={a.id} a={a} />
      ))}
      {done.length > 0 && (
        <div className="resolved">
          {done.map((a) => (
            <div key={a.id} className={`resolved-row resolved-${a.status}`}>
              <span>{TOOL_LABEL[a.call.tool]} {a.decision.money > 0 ? money(a.decision.money) : ""}</span>
              <span>{a.status === "approved" ? "Approved" : "Declined"}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function AgentConsole({ lines }: { lines: AgentLine[] }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [lines.length]);
  return (
    <section className="panel console" aria-live="polite">
      <div className="panel-head">
        <h3>Agent's view</h3>
      </div>
      {lines.length === 0 && <p className="muted">The agent's reasoning and tool calls show up here while it works.</p>}
      <div className="lines">
        {lines.map((l, i) => (
          <div key={i} className={`line line-${l.role} ${l.status ? `st-${l.status}` : ""}`}>
            {l.role === "tool_call" ? <span className="call">{l.text}</span> : l.text}
          </div>
        ))}
        <div ref={end} />
      </div>
    </section>
  );
}

export function ApprovePage({ id }: { id: string }) {
  const [a, setA] = useState<Approval | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () => api.approval(id).then((x) => alive && setA(x)).catch(() => alive && setErr("This approval link has expired or doesn't exist."));
    load();
    const t = window.setInterval(load, 2000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [id]);
  return (
    <main className="approve-page">
      <div className="brand compact">
        <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden>
          <path d="M16 2 4 7v8c0 7.5 5 13 12 15 7-2 12-7.5 12-15V7z" fill="currentColor" />
        </svg>
        <span className="wordmark">Warden</span>
      </div>
      {err && <p className="error">{err}</p>}
      {a && (
        <>
          <p className="approve-intro">Your back-office agent wants to do this. Warden held it because:</p>
          <ApprovalCard a={a} big />
        </>
      )}
    </main>
  );
}
