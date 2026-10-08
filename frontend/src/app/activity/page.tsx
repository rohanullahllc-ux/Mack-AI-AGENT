"use client";

import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { AuditLog } from "@/components/AuditLog";
import { RunBlock, fmtCost } from "@/components/RunBlock";
import { useApp } from "@/components/Shell";
import { api } from "@/lib/api";
import { runFromRecord } from "@/lib/runview";
import type { RunRecord, RunView } from "@/lib/types";
import { useUrlParam } from "@/lib/useUrlParam";

const STATUS_STYLE: Record<string, string> = {
  completed: "text-ok border-ok/30",
  running: "text-accent border-accent/30",
  error: "text-err border-err/30",
  refused: "text-err border-err/30",
  limit: "text-warn border-warn/30",
  cancelled: "text-muted border-line",
  budget_exceeded: "text-warn border-warn/30",
};

const duration = (r: RunRecord) =>
  r.finished_at ? `${Math.max(0, r.finished_at - r.started_at).toFixed(1)}s` : "—";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-line bg-panel px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 text-xl font-semibold">{value}</div>
    </div>
  );
}

function RunRow({ run }: { run: RunRecord }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<RunView | null>(null);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !view) setView(runFromRecord(await api.run(run.id)));
  };

  return (
    <div className="rounded-xl border border-line bg-panel">
      <button onClick={toggle} className="flex w-full items-center gap-3 px-3 py-3 text-left" aria-expanded={open}>
        <ChevronRight size={14} className={`shrink-0 text-muted transition-transform ${open ? "rotate-90" : ""}`} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm">{run.user_text}</div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
            <span>{new Date(run.started_at * 1000).toLocaleString()}</span>
            <span>{run.steps} calls</span>
            <span>{(run.input_tokens + run.output_tokens).toLocaleString()} tok</span>
            <span>{duration(run)}</span>
          </div>
        </div>
        <span className="shrink-0 text-sm tabular-nums">{fmtCost(run.cost_usd)}</span>
        <span className={`shrink-0 rounded-full border px-2 text-[11px] ${STATUS_STYLE[run.status] ?? "border-line"}`}>
          {run.status}
        </span>
      </button>
      {open && (
        <div className="border-t border-line px-4 py-4">
          {view ? <RunBlock run={view} showUser={false} /> : <p className="text-sm text-muted">Loading…</p>}
          {run.error && <p className="mt-2 text-xs text-err">{run.error}</p>}
        </div>
      )}
    </div>
  );
}

type Tab = "runs" | "audit";
const TABS: { id: Tab; label: string }[] = [
  { id: "runs", label: "Runs" },
  { id: "audit", label: "Audit log" },
];

export default function ActivityPage() {
  const { config } = useApp();
  const [tabParam, setTab] = useUrlParam("tab");
  const tab: Tab = tabParam === "audit" ? "audit" : "runs"; // the URL can say anything; fall back to the default
  const [runs, setRuns] = useState<RunRecord[] | null>(null);

  useEffect(() => {
    api.runs(100).then(setRuns).catch(() => setRuns([]));
  }, []);

  const totalCost = (runs ?? []).reduce((a, r) => a + r.cost_usd, 0);
  const done = (runs ?? []).filter((r) => r.status === "completed").length;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-6">
        <h1 className="text-xl font-semibold">Activity</h1>
        <div role="tablist" aria-label="Activity views" className="flex gap-1 border-b border-line">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              id={`activity-tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls="activity-panel"
              onClick={() => setTab(t.id === "runs" ? null : t.id, "replace")}
              className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                tab === t.id ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div role="tabpanel" id="activity-panel" aria-labelledby={`activity-tab-${tab}`} className="space-y-6">
          {tab === "audit" ? (
            <AuditLog />
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label="Spent this month" value={config ? `$${config.budget.spent_usd.toFixed(2)}` : "—"} />
                <Stat label="Monthly budget" value={config ? `$${config.budget.monthly_usd.toFixed(0)}` : "—"} />
                <Stat label="Runs (last 100)" value={runs ? String(runs.length) : "—"} />
                <Stat
                  label="Avg cost / run"
                  value={runs && runs.length ? fmtCost(totalCost / runs.length) : "—"}
                />
              </div>
              {runs && runs.length > 0 && (
                <p className="text-xs text-muted">
                  {done} of {runs.length} completed. Expand a run to replay every thought, tool call and result.
                </p>
              )}
              <div className="space-y-2">
                {runs === null && <p className="text-sm text-muted">Loading…</p>}
                {runs?.length === 0 && <p className="text-sm text-muted">No runs yet — ask the agent something in Chat.</p>}
                {runs?.map((r) => <RunRow key={r.id} run={r} />)}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
