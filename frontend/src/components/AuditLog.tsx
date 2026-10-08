"use client";

import { ChevronRight, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { AuditEntry, AuditOutcome, AuditPage } from "@/lib/types";
import { TierBadge } from "./RunBlock";

const PAGE_SIZE = 25;

type Filter = AuditOutcome | "all";
type Loaded = AuditPage & { filter: Filter; error: string | null };

const OUTCOME: Record<AuditOutcome, { label: string; style: string }> = {
  approved: { label: "Approved", style: "text-ok border-ok/30" },
  auto: { label: "Auto-allowed", style: "text-accent2 border-accent2/30" },
  denied: { label: "Denied", style: "text-err border-err/30" },
  timed_out: { label: "Timed out", style: "text-warn border-warn/30" },
  cancelled: { label: "Cancelled", style: "text-muted border-line" },
  pending: { label: "Waiting", style: "text-accent border-accent/30" },
};

const FILTERS: Filter[] = ["all", "approved", "denied", "timed_out", "cancelled", "auto", "pending"];

const RESULT = { ok: "Ran", error: "Failed", interrupted: "Interrupted" } as const;

const resultLabel = (e: AuditEntry) => (e.result ? RESULT[e.result] : e.outcome === "pending" ? null : "Not run");

const fmtWait = (s: number) => (s < 1 ? "under a second" : s < 60 ? `${s.toFixed(1)} s` : `${Math.round(s / 60)} min`);

const toParam = (f: Filter) => (f === "all" ? undefined : f);

/** How long the request sat waiting, worded for how it ended. Nothing to say when it never had to wait. */
function waitNote(e: AuditEntry): string | null {
  if (e.decided_at === null || e.outcome === "auto") return null;
  const waited = fmtWait(e.decided_at - e.requested_at);
  if (e.outcome === "timed_out") return `No answer after ${waited}.`;
  if (e.outcome === "cancelled") return `Cancelled after ${waited}.`;
  return `Answered after ${waited}.`;
}

function Row({ entry }: { entry: AuditEntry }) {
  const outcome = OUTCOME[entry.outcome];
  const result = resultLabel(entry);
  const wait = waitNote(entry);
  return (
    <li>
      <details className="group rounded-xl border border-line bg-panel">
        <summary className="cursor-pointer list-none px-3 py-2.5 [&::-webkit-details-marker]:hidden">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <ChevronRight size={14} className="shrink-0 text-muted transition-transform group-open:rotate-90" />
            <span className="font-mono text-[13px]">{entry.tool}</span>
            <TierBadge tier={entry.tier} />
            <span className={`rounded-full border px-2 text-[11px] ${outcome.style}`}>{outcome.label}</span>
            {result && <span className="text-xs text-muted">{result}</span>}
            <time className="ml-auto text-xs text-muted" dateTime={new Date(entry.requested_at * 1000).toISOString()}>
              {new Date(entry.requested_at * 1000).toLocaleString()}
            </time>
          </div>
          <div className="mt-1 truncate pl-[22px] font-mono text-xs text-muted">{entry.input_preview}</div>
        </summary>
        <div className="space-y-2 border-t border-line px-3 py-2 text-xs">
          <div>
            <div className="mb-1 text-muted">Input</div>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono">{entry.input_preview}</pre>
          </div>
          {entry.reason && (
            <div>
              <div className="mb-1 text-muted">Reason</div>
              <div>{entry.reason}</div>
            </div>
          )}
          {wait && <div className="text-muted">{wait}</div>}
          {entry.run_text && (
            <div>
              <div className="mb-1 text-muted">Asked in</div>
              <div className="whitespace-pre-wrap break-words">{entry.run_text}</div>
            </div>
          )}
        </div>
      </details>
    </li>
  );
}

export function AuditLog() {
  const [filter, setFilter] = useState<Filter>("all");
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [more, setMore] = useState<"idle" | "loading" | "error">("idle");

  useEffect(() => {
    let stale = false;
    api
      .audit({ limit: PAGE_SIZE, outcome: toParam(filter) })
      .then((page) => {
        if (stale) return;
        setLoaded({ ...page, filter, error: null });
        setMore("idle");
      })
      .catch((e) => {
        if (stale) return;
        const error = e instanceof ApiError ? e.message : "Could not load the audit log";
        setLoaded((prev) => ({ items: [], total: 0, counts: prev?.counts ?? {}, filter, error }));
      });
    return () => {
      stale = true;
    };
  }, [filter, reloads]);

  const loadMore = async () => {
    if (!loaded || loaded.filter !== filter) return;
    setMore("loading");
    try {
      const page = await api.audit({ limit: PAGE_SIZE, offset: loaded.items.length, outcome: toParam(filter) });
      setLoaded((cur) => {
        if (!cur || cur.filter !== filter) return cur; // the filter changed while this was in flight
        const seen = new Set(cur.items.map((i) => i.id)); // rows added meanwhile shift the offsets
        return { ...cur, items: [...cur.items, ...page.items.filter((i) => !seen.has(i.id))], total: page.total, counts: page.counts };
      });
      setMore("idle");
    } catch {
      setMore("error");
    }
  };

  const loading = loaded === null || loaded.filter !== filter;
  const counts = loaded?.counts ?? {};
  const everything = Object.values(counts).reduce((sum, n) => sum + (n ?? 0), 0);
  const items = loading ? [] : loaded.items;

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <p className="flex-1 text-xs text-muted">
          Every action that changes something: the ones you approved or denied, and the ones the agent was allowed to run on its
          own. Entries stay here even after you delete the chat they came from.
        </p>
        <button
          onClick={() => setReloads((n) => n + 1)}
          aria-label="Refresh"
          className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-line bg-panel text-muted hover:border-accent hover:text-fg"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by outcome">
        {FILTERS.filter((f) => f === "all" || f === filter || (counts[f] ?? 0) > 0).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            aria-pressed={filter === f}
            className={`rounded-full border px-3 py-1 text-xs ${
              filter === f ? "border-accent bg-accent/15 text-fg" : "border-line bg-panel text-muted hover:border-accent"
            }`}
          >
            {f === "all" ? "All" : OUTCOME[f].label} <span className="tabular-nums">{f === "all" ? everything : (counts[f] ?? 0)}</span>
          </button>
        ))}
      </div>

      {loading && (
        <p className="text-sm text-muted" aria-busy="true">
          Loading…
        </p>
      )}
      {!loading && loaded.error && (
        <p role="alert" className="rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-sm text-err">
          {loaded.error}
        </p>
      )}
      {!loading && !loaded.error && items.length === 0 && (
        <p className="text-sm text-muted">
          {filter === "all"
            ? "Nothing recorded yet. Once the agent tries something that changes state, such as saving a note, it shows up here."
            : "No entries with this outcome."}
        </p>
      )}
      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((entry) => (
            <Row key={entry.id} entry={entry} />
          ))}
        </ul>
      )}

      {!loading && !loaded.error && items.length > 0 && (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <span>
            Showing {items.length} of {loaded.total}
          </span>
          {items.length < loaded.total && (
            <button
              onClick={loadMore}
              disabled={more === "loading"}
              className="rounded-lg border border-line bg-panel px-3 py-1.5 text-fg hover:border-accent disabled:opacity-50"
            >
              {more === "loading" ? "Loading…" : "Load more"}
            </button>
          )}
          {more === "error" && (
            <span role="alert" className="text-err">
              Could not load more.
            </span>
          )}
        </div>
      )}
    </div>
  );
}
