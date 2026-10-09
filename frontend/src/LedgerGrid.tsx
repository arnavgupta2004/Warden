import { AllCommunityModule, ColDef, ICellRendererParams, ModuleRegistry, RowClassParams, themeQuartz } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useMemo, useState } from "react";
import { LedgerEntry, TOOL_LABEL, VERDICT_LABEL, money, statusLabel } from "./api";

ModuleRegistry.registerModules([AllCommunityModule]);

const theme = themeQuartz.withParams({
  fontFamily: "inherit",
  fontSize: 13,
  backgroundColor: "var(--surface)",
  foregroundColor: "var(--ink)",
  headerBackgroundColor: "var(--surface-2)",
  headerTextColor: "var(--ink-2)",
  headerFontWeight: 600,
  borderColor: "var(--line)",
  rowBorder: { color: "var(--line)" },
  oddRowBackgroundColor: "var(--surface)",
  selectedRowBackgroundColor: "var(--select)",
  rowHoverColor: "var(--hover)",
  accentColor: "var(--cobalt)",
  spacing: 6,
  wrapperBorderRadius: 10,
  headerHeight: 36,
  rowHeight: 42,
});

function VerdictCell(p: ICellRendererParams<LedgerEntry>) {
  const e = p.data;
  if (!e) return null;
  const reads = e.tool === "fetch_url" || e.tool.startsWith("get_") || e.tool.startsWith("list_");
  if (reads && e.verdict === "allow") {
    const flagged = e.findings.some((f) => f.rule === "injection_signals");
    return <span className={`pill ${flagged ? "pill-flag" : "pill-read"}`}>{flagged ? "Suspicious content" : "Read"}</span>;
  }
  return <span className={`pill pill-${e.verdict}`}>{VERDICT_LABEL[e.verdict]}</span>;
}

function RiskCell(p: ICellRendererParams<LedgerEntry>) {
  const r = p.data?.risk ?? 0;
  const tone = r >= 80 ? "deny" : r >= 40 ? "hold" : "ok";
  return (
    <span className="risk" aria-label={`risk ${r} of 100`}>
      <span className={`risk-bar risk-${tone}`} style={{ width: `${Math.max(r, 3)}%` }} />
      <span className="risk-num">{r}</span>
    </span>
  );
}

function ActionCell(p: ICellRendererParams<LedgerEntry>) {
  const e = p.data;
  if (!e) return null;
  let detail = "";
  if (e.tool === "fetch_url") {
    const u = String(e.args.url ?? "");
    const m = u.match(/inbox\/(\d+)/);
    detail = m ? `email #${m[1]}` : u.replace(/^https?:\/\//, "").slice(0, 40);
  } else if (e.args.order_id) detail = String(e.args.order_id);
  else if (e.args.dispute_id) detail = String(e.args.dispute_id);
  else if (e.args.invoice_id) detail = String(e.args.invoice_id).slice(0, 14) + "…";
  return (
    <span className="action-cell">
      <span className="action-name">{TOOL_LABEL[e.tool] ?? e.tool}</span>
      {detail && <span className="action-detail">{detail}</span>}
    </span>
  );
}

export function LedgerGrid({ rows, selectedId, onSelect }: { rows: LedgerEntry[]; selectedId: string | null; onSelect: (e: LedgerEntry) => void }) {
  const [quick, setQuick] = useState("");
  const [moneyOnly, setMoneyOnly] = useState(false);
  const data = useMemo(() => (moneyOnly ? rows.filter((r) => r.money > 0 || ["cancel_sent_invoice", "provide_dispute_evidence"].includes(r.tool)) : rows), [rows, moneyOnly]);

  const cols = useMemo<ColDef<LedgerEntry>[]>(
    () => [
      {
        headerName: "Time",
        field: "at",
        width: 96,
        sort: "desc",
        valueFormatter: (p) => new Date((p.value as number) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }),
      },
      { headerName: "Action", field: "tool", flex: 1.1, minWidth: 150, cellRenderer: ActionCell, filter: "agTextColumnFilter", getQuickFilterText: (p) => `${TOOL_LABEL[p.data!.tool] ?? ""} ${JSON.stringify(p.data!.args)}` },
      { headerName: "To", field: "counterparty", flex: 1.4, minWidth: 150, tooltipField: "counterparty", filter: "agTextColumnFilter", valueFormatter: (p) => (p.value as string) || "" },
      {
        headerName: "Amount",
        field: "money",
        width: 98,
        type: "rightAligned",
        filter: "agNumberColumnFilter",
        valueFormatter: (p) => ((p.value as number) > 0 ? money(p.value as number, p.data?.currency) : ""),
        cellClass: "tabular",
      },
      { headerName: "Warden", field: "verdict", width: 140, cellRenderer: VerdictCell, filter: "agTextColumnFilter", getQuickFilterText: (p) => VERDICT_LABEL[p.data!.verdict] },
      { headerName: "Risk", field: "risk", width: 92, cellRenderer: RiskCell, filter: "agNumberColumnFilter" },
      { headerName: "Outcome", field: "status", width: 150, valueGetter: (p) => (p.data ? statusLabel(p.data) : "") },
    ],
    [],
  );

  return (
    <div className="grid-wrap">
      <div className="grid-tools">
        <input className="search" placeholder="Search the ledger" value={quick} onChange={(e) => setQuick(e.target.value)} aria-label="Search the ledger" />
        <label className="check">
          <input type="checkbox" checked={moneyOnly} onChange={(e) => setMoneyOnly(e.target.checked)} /> Money moves only
        </label>
      </div>
      <div className="grid">
        <AgGridReact<LedgerEntry>
          theme={theme}
          rowData={data}
          columnDefs={cols}
          getRowId={(p) => p.data.id}
          quickFilterText={quick}
          rowSelection={{ mode: "singleRow", checkboxes: false, enableClickSelection: true }}
          onRowClicked={(e) => e.data && onSelect(e.data)}
          rowClassRules={{
            "row-deny": (p: RowClassParams<LedgerEntry>) => p.data?.verdict === "deny",
            "row-hold": (p: RowClassParams<LedgerEntry>) => p.data?.status === "pending_approval",
            "row-selected-ext": (p: RowClassParams<LedgerEntry>) => p.data?.id === selectedId,
          }}
          animateRows
          tooltipShowDelay={400}
          overlayNoRowsTemplate="<span class='empty'>No agent activity yet. Run an email from the inbox to watch Warden work.</span>"
        />
      </div>
    </div>
  );
}
