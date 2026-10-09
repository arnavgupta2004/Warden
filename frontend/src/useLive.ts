import { useCallback, useEffect, useRef, useState } from "react";
import { AgentLine, Approval, LedgerEntry, Mandate, Status, api } from "./api";

export function useLive() {
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [mandate, setMandate] = useState<Mandate | null>(null);
  const [spent, setSpent] = useState(0);
  const [status, setStatus] = useState<Status | null>(null);
  const [lines, setLines] = useState<AgentLine[]>([]);
  const [connected, setConnected] = useState(false);
  const refreshTimer = useRef<number | undefined>(undefined);

  const refresh = useCallback(async () => {
    const [l, a, m, s] = await Promise.all([api.ledger(), api.approvals(), api.mandate().catch(() => null), api.status()]);
    setLedger(l);
    setApprovals(a);
    if (m) {
      setMandate(m.mandate);
      setSpent(m.spent);
    }
    setStatus(s);
  }, []);

  // Coalesce bursts of events into one summary refresh (budget, chain status).
  const soonRefreshMeta = useCallback(() => {
    window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(async () => {
      const [m, s] = await Promise.all([api.mandate().catch(() => null), api.status()]);
      if (m) {
        setMandate(m.mandate);
        setSpent(m.spent);
      }
      setStatus(s);
    }, 250);
  }, []);

  useEffect(() => {
    refresh().catch(() => undefined);
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        setConnected(true);
        refresh().catch(() => undefined);
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 1500);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data) as { type: string; data: any };
        if (msg.type === "ledger") {
          const e = msg.data as LedgerEntry;
          setLedger((prev) => {
            const i = prev.findIndex((x) => x.id === e.id);
            if (i === -1) return [e, ...prev];
            const next = prev.slice();
            next[i] = e;
            return next;
          });
          soonRefreshMeta();
        } else if (msg.type === "approval") {
          const a = msg.data as Approval;
          setApprovals((prev) => {
            const i = prev.findIndex((x) => x.id === a.id);
            if (i === -1) return [a, ...prev];
            const next = prev.slice();
            next[i] = a;
            return next;
          });
        } else if (msg.type === "agent") {
          setLines((prev) => [...prev.slice(-200), msg.data as AgentLine]);
        } else if (msg.type === "mandate") {
          setMandate(msg.data as Mandate);
          soonRefreshMeta();
        } else if (msg.type === "reset") {
          setLedger([]);
          setApprovals([]);
          setLines([]);
          soonRefreshMeta();
        }
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [refresh, soonRefreshMeta]);

  return { ledger, approvals, mandate, spent, status, lines, connected, refresh, setLines };
}
