import { useMemo, useState } from "react";
import { DemoEmail, Finding, LedgerEntry, TOOL_LABEL, VERDICT_LABEL, money, statusLabel } from "./api";

const RULE_LABEL: Record<string, string> = {
  injected_instruction: "Follows a hidden instruction",
  tainted_payee: "Payee came from untrusted content",
  lookalike_payee: "Imitates a trusted payee",
  new_payee: "New payee",
  session_quarantine: "Session quarantined",
  split_payments: "Split payments",
  cover_tracks: "Covering tracks",
  recon_then_payout: "Records scanned, then payout",
  money_after_cancel: "Money after cancellation",
  refund_over_auto_limit: "Above your refund limit",
  refund_during_dispute: "Payment is under dispute",
  over_refund: "More than was paid",
  unknown_capture: "Payment does not exist",
  unknown_dispute: "Dispute does not exist",
  per_tx_limit: "Above single-payment cap",
  budget: "Over budget",
  approval_threshold: "Above approval threshold",
  payee_limit: "Above payee limit",
  velocity: "Too many payments",
  out_of_scope: "Outside the mandate",
  blocked_category: "Blocked category",
  intent_check: "AI intent check",
  injection_signals: "Prompt-injection signals",
  no_mandate: "No mandate",
  mandate_tampered: "Mandate altered",
  mandate_expired: "Mandate expired",
  concede_dispute: "Concedes a dispute",
  bad_amount: "Invalid amount",
  unknown_tool: "Unknown tool",
};

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlight(html: string, needles: string[]) {
  let out = html;
  for (const n of needles.filter((x) => x && x.length >= 4)) {
    out = out.replace(new RegExp(`(?![^<]*>)(${escapeRe(n)})`, "gi"), "<mark>$1</mark>");
  }
  return out;
}

function EmailEvidence({ email, needles }: { email: DemoEmail; needles: string[] }) {
  const [agentView, setAgentView] = useState(true);
  const hasHidden = /color:\s*#fff|font-size:\s*1px|display:\s*none/i.test(email.html);
  const html = useMemo(() => highlight(email.html, needles), [email.html, needles]);
  return (
    <div className="evidence">
      <div className="evidence-head">
        <div>
          <div className="evidence-from">{email.sender}</div>
          <div className="evidence-subject">{email.subject}</div>
        </div>
        {hasHidden && (
          <div className="seg" role="group" aria-label="Email view">
            <button className={!agentView ? "on" : ""} onClick={() => setAgentView(false)}>What you see</button>
            <button className={agentView ? "on" : ""} onClick={() => setAgentView(true)}>What the agent read</button>
          </div>
        )}
      </div>
      <div className={`email-body ${agentView ? "agent-view" : ""}`} dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

function FindingRow({ f }: { f: Finding }) {
  const signals = (f.evidence.signals as string[] | undefined) ?? [];
  return (
    <li className={`finding finding-${f.effect}`}>
      <div className="finding-title">
        <span className={`dot dot-${f.effect}`} aria-hidden />
        {RULE_LABEL[f.rule] ?? f.rule}
        <span className="finding-sev">{f.severity}</span>
      </div>
      <p>{f.message}</p>
      {typeof f.evidence.excerpt === "string" && <blockquote>{f.evidence.excerpt}</blockquote>}
      {signals.length > 0 && (
        <ul className="signals">
          {signals.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function DecisionTrace({ entry, emails, onClose }: { entry: LedgerEntry | null; emails: DemoEmail[]; onClose: () => void }) {
  if (!entry) {
    return (
      <section className="trace trace-empty" aria-label="Decision detail">
        <p>Select a row to see why Warden decided what it did, including the content that steered the agent.</p>
      </section>
    );
  }
  const sourceUrl = entry.findings.map((f) => f.evidence.source).find((s) => typeof s === "string") as string | undefined
    ?? (entry.tool === "fetch_url" ? String(entry.args.url) : undefined);
  const num = sourceUrl?.match(/inbox\/(\d+)/)?.[1];
  const email = num ? emails.find((e) => e.n === Number(num)) : undefined;
  const needles = [entry.counterparty, ...entry.findings.map((f) => String(f.evidence.matched ?? "")), ...Object.values(entry.args).map(String)];
  const reads = entry.tool === "fetch_url" || entry.tool.startsWith("get_") || entry.tool.startsWith("list_");
  const tone = reads && entry.verdict === "allow" ? (entry.findings.length ? "step_up" : "read") : entry.verdict;
  const ppRef = entry.paypal_ref;

  return (
    <section className={`trace trace-${tone}`} aria-label="Decision detail">
      <header className="trace-head">
        <div>
          <div className="trace-verdict">{reads && entry.verdict === "allow" ? (entry.findings.length ? "Suspicious content recorded" : "Read-only") : VERDICT_LABEL[entry.verdict]}</div>
          <h2>
            {TOOL_LABEL[entry.tool] ?? entry.tool}
            {entry.money > 0 && <> of {money(entry.money, entry.currency)}</>}
            {entry.counterparty && <> to {entry.counterparty}</>}
          </h2>
          <p className="trace-expl">{entry.explanation}</p>
        </div>
        <button className="ghost close" onClick={onClose} aria-label="Close detail">Close</button>
      </header>

      <div className="trace-body">
        <div className="trace-col">
          {entry.findings.length > 0 ? (
            <ul className="findings">
              {entry.findings.map((f, i) => (
                <FindingRow key={i} f={f} />
              ))}
            </ul>
          ) : (
            <p className="muted">No rules fired. The action matched the mandate.</p>
          )}
          <dl className="facts">
            <dt>Outcome</dt>
            <dd>{statusLabel(entry)}{entry.approved_by ? ` by ${entry.approved_by}` : ""}</dd>
            {ppRef && (
              <>
                <dt>PayPal reference</dt>
                <dd className="tabular">{ppRef}</dd>
              </>
            )}
            <dt>Arguments</dt>
            <dd>
              <code className="args">{JSON.stringify(entry.args, null, 1)}</code>
            </dd>
            <dt>Ledger hash</dt>
            <dd className="hash" title={entry.hash}>{entry.hash.slice(0, 16)}…</dd>
          </dl>
        </div>
        {email && (
          <div className="trace-col">
            <EmailEvidence email={email} needles={needles} />
          </div>
        )}
      </div>
    </section>
  );
}
