export type Verdict = "allow" | "step_up" | "deny";

export interface Finding {
  rule: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  effect: Verdict;
  message: string;
  evidence: Record<string, unknown>;
}

export interface LedgerEntry {
  seq: number;
  id: string;
  at: number;
  session_id: string;
  agent_id: string;
  tool: string;
  args: Record<string, unknown>;
  verdict: Verdict;
  risk: number;
  findings: Finding[];
  explanation: string;
  money: number;
  currency: string;
  counterparty: string;
  status: string;
  approved_by: string | null;
  paypal_ref: string | null;
  result: Record<string, unknown> | null;
  hash: string;
}

export interface Payee {
  name: string;
  email: string;
  max_per_payment: number | null;
}

export interface Mandate {
  id: string;
  source_text: string;
  purpose: string;
  currency: string;
  budget_total: number;
  budget_window: string;
  per_tx_max: number;
  auto_approve_below: number;
  refund_auto_max: number | null;
  allowed_tools: string[];
  trusted_payees: Payee[];
  new_payee_requires_approval: boolean;
  max_moves_per_hour: number;
  compiled_by: string;
  public_demo_caps: string[];
  signature: string;
}

export interface Approval {
  id: string;
  ledger_id: string;
  call: { tool: string; args: Record<string, unknown>; stated_reason: string; session_id: string };
  decision: { verdict: Verdict; risk: number; findings: Finding[]; explanation: string; money: number; counterparty: string };
  status: "pending" | "approved" | "denied" | "expired";
  decided_by: string | null;
  created_at: number;
}

export interface Status {
  paypal_mode: string;
  llm: boolean;
  llm_limited: boolean;
  llm_capped: boolean;
  llm_model: string | null;
  chain: { ok: boolean; records: number; tip?: string };
  pending_approvals: number;
  mcp_url: string;
}

export interface DemoEmail {
  n: number;
  sender: string;
  subject: string;
  scenario: string;
  summary: string;
  html: string;
  unavailable: string | null;
}

export interface AgentLine {
  run_id: string;
  scenario: string;
  agent: string;
  role: "thought" | "tool_call" | "tool_result" | "final";
  text: string;
  status?: string;
  tool?: string;
}

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) {
    const body = await r.text();
    let detail = body;
    try {
      detail = JSON.parse(body).detail ?? body;
    } catch {
      /* not JSON */
    }
    throw Object.assign(new Error(`${r.status} ${detail}`), { status: r.status, detail });
  }
  return r.json() as Promise<T>;
}

export const api = {
  status: () => j<Status>("/api/status"),
  mandate: () => j<{ mandate: Mandate; spent: number }>("/api/mandate"),
  setMandate: (text: string) => j<{ mandate: Mandate }>("/api/mandate", { method: "POST", body: JSON.stringify({ text }) }),
  ledger: () => j<LedgerEntry[]>("/api/ledger"),
  approvals: () => j<Approval[]>("/api/approvals"),
  approval: (id: string) => j<Approval>(`/api/approvals/${id}`),
  decide: (id: string, approve: boolean, via = "dashboard") =>
    j<Approval>(`/api/approvals/${id}/${approve ? "approve" : "deny"}`, { method: "POST", headers: { "X-Warden-User": `owner (${via})` } }),
  emails: () => j<DemoEmail[]>("/api/demo/emails"),
  run: (email: number, agent: string) => j<{ run_id: string }>("/api/demo/run", { method: "POST", body: JSON.stringify({ email, agent }) }),
  reset: () => j<{ ok: boolean }>("/api/reset", { method: "POST" }),
};

export function money(v: number, cur = "USD") {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: cur }).format(v);
}

export const TOOL_LABEL: Record<string, string> = {
  fetch_url: "Read content",
  get_order: "Look up order",
  get_capture: "Look up payment",
  get_dispute: "Look up dispute",
  list_transactions: "List transactions",
  list_invoices: "List invoices",
  list_disputes: "List disputes",
  create_refund: "Refund",
  create_payout: "Payout",
  accept_dispute_claim: "Concede dispute",
  create_invoice: "Send invoice",
  send_invoice_reminder: "Invoice reminder",
  cancel_sent_invoice: "Cancel invoice",
  provide_dispute_evidence: "Answer dispute",
  create_shipment_tracking: "Add tracking",
};

export const VERDICT_LABEL: Record<string, string> = {
  allow: "Allowed",
  step_up: "Held for you",
  deny: "Blocked",
};

export function statusLabel(e: LedgerEntry): string {
  switch (e.status) {
    case "executed":
      return e.approved_by ? "Approved, paid" : "Done";
    case "settled":
      return "Settled";
    case "pending_approval":
      return "Waiting for you";
    case "approved":
      return "Approved";
    case "denied":
      return e.verdict === "step_up" ? "You declined" : "Stopped";
    case "failed":
      return "PayPal error";
    default:
      return e.status;
  }
}
