import { useEffect, useMemo, useState } from "react";
import { DecisionTrace } from "./DecisionTrace";
import { LedgerGrid } from "./LedgerGrid";
import { AgentConsole, ApprovalsPanel, ApprovePage, Header, InboxPanel, MandatePanel } from "./Panels";
import { DemoEmail, api } from "./api";
import { useLive } from "./useLive";

function Dashboard() {
  const live = useLive();
  const [emails, setEmails] = useState<DemoEmail[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [agent, setAgent] = useState("scripted");

  useEffect(() => {
    api.emails().then(setEmails).catch(() => undefined);
  }, []);

  // Follow the most important new event: select the latest blocked or held action.
  const latestAlert = live.ledger.find((e) => e.verdict !== "allow");
  useEffect(() => {
    if (latestAlert) setSelectedId(latestAlert.id);
  }, [latestAlert?.id]);

  const selected = useMemo(() => live.ledger.find((e) => e.id === selectedId) ?? null, [live.ledger, selectedId]);
  const reset = async () => {
    await api.reset();
    setSelectedId(null);
  };

  return (
    <div className="app">
      <Header status={live.status} connected={live.connected} onReset={reset} />
      <main className="layout">
        <aside className="col-left">
          <MandatePanel mandate={live.mandate} spent={live.spent} />
          <InboxPanel emails={emails} agent={agent} setAgent={setAgent} llm={!!live.status?.llm} />
        </aside>
        <section className="col-main">
          <LedgerGrid rows={live.ledger} selectedId={selectedId} onSelect={(e) => setSelectedId(e.id)} />
          <DecisionTrace entry={selected} emails={emails} onClose={() => setSelectedId(null)} />
        </section>
        <aside className="col-right">
          <ApprovalsPanel approvals={live.approvals} />
          <AgentConsole lines={live.lines} />
        </aside>
      </main>
      <footer className="foot">
        Connect any MCP agent to <code>{live.status?.mcp_url ?? "/mcp"}</code> in place of PayPal's MCP server.
      </footer>
    </div>
  );
}

export default function App() {
  const m = location.pathname.match(/^\/approve\/([\w-]+)/);
  return m ? <ApprovePage id={m[1]} /> : <Dashboard />;
}
