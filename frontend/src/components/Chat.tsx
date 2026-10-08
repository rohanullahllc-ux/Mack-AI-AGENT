"use client";

import { ArrowUp, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, readSSE } from "@/lib/api";
import { applyEvent, emptyRun, runFromRecord } from "@/lib/runview";
import type { AgentEvent, RunView } from "@/lib/types";
import { useUrlParam } from "@/lib/useUrlParam";
import { RunBlock } from "./RunBlock";
import { useApp } from "./Shell";

const SUGGESTIONS = [
  "What’s today’s date and what are the top AI news stories this week?",
  "Save a note called “Ideas” with three product ideas for a bootstrapped SaaS.",
  "List my notes and summarise what’s in them.",
];

const MAX_RECONNECTS = 8;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let localKeySeq = 0;

function HistorySkeleton() {
  return (
    <div className="animate-pulse space-y-6" aria-busy="true" aria-label="Loading conversation">
      <div className="flex justify-end">
        <div className="h-10 w-1/2 rounded-2xl bg-panel2" />
      </div>
      <div className="space-y-2">
        <div className="h-4 w-11/12 rounded bg-panel2" />
        <div className="h-4 w-4/5 rounded bg-panel2" />
        <div className="h-4 w-2/3 rounded bg-panel2" />
      </div>
    </div>
  );
}

export function Chat() {
  const { config, refresh } = useApp();
  const [conversationId, setConversationParam] = useUrlParam("c");
  const [runs, setRuns] = useState<RunView[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);

  const scroller = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const stickToBottom = useRef(true);
  const skipLoadFor = useRef<string | null>(null); // conversation we just created locally
  const runIdRef = useRef<string>("");

  // ---- autoscroll ---------------------------------------------------------
  useEffect(() => {
    if (stickToBottom.current) scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [runs]);

  // grow the input with its content (CSS field-sizing is Chrome-only)
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  // ---- streaming ----------------------------------------------------------
  const onEvent = useCallback((ev: AgentEvent) => {
    if (ev.type === "run_started") runIdRef.current = ev.data.run_id;
    setRuns((rs) => {
      if (rs.length === 0) return rs;
      const i = rs.findIndex((r) => r.id === runIdRef.current || r.id === "pending");
      const idx = i >= 0 ? i : rs.length - 1;
      return rs.map((r, j) => (j === idx ? applyEvent(r, ev) : r));
    });
  }, []);

  /** (Re)attach to a run's event stream; survives phone sleep / dropped connections. */
  const attach = useCallback(
    async (runId: string, after: number) => {
      let last = after;
      let finished = false;
      for (let attempt = 0; attempt <= MAX_RECONNECTS && !finished; attempt++) {
        try {
          await readSSE(`/api/runs/${runId}/events?after=${last}`, { method: "GET" }, (ev) => {
            last = Math.max(last, ev.seq);
            if (ev.type === "run_finished") finished = true;
            onEvent(ev);
          });
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) return;
          if ((e as Error).name === "AbortError") return;
        }
        if (!finished) await sleep(Math.min(1000 * 2 ** attempt, 8000));
      }
    },
    [onEvent],
  );

  // ---- load history when the conversation changes ----------------------
  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- syncing UI state to the selected conversation */
    setLoadError(null);
    if (!conversationId) {
      setRuns([]);
      setBusy(false);
      setLoadingHistory(false);
      return;
    }
    if (skipLoadFor.current === conversationId) {
      skipLoadFor.current = null; // we just created this conversation and are already streaming into it
      return;
    }
    setLoadingHistory(true);
    /* eslint-enable react-hooks/set-state-in-effect */
    let cancelled = false;
    api
      .conversation(conversationId)
      .then((detail) => {
        if (cancelled) return;
        setLoadingHistory(false);
        const views = detail.runs.map((r) =>
          // a run still executing is rebuilt from its live stream (which includes deltas), not from SQLite
          r.id === detail.active_run_id ? emptyRun(r.id, r.user_text) : runFromRecord(r),
        );
        setRuns(views);
        stickToBottom.current = true;
        if (detail.active_run_id) {
          setBusy(true);
          runIdRef.current = detail.active_run_id;
          attach(detail.active_run_id, -1).finally(() => !cancelled && setBusy(false));
        } else setBusy(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setLoadingHistory(false);
        setRuns([]);
        setLoadError(e instanceof ApiError ? e.message : "Could not load conversation");
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, attach]);

  const send = useCallback(
    async (text: string) => {
      text = text.trim();
      if (!text || busy) return;
      setInput("");
      setBusy(true);
      stickToBottom.current = true;
      setLoadError(null);

      let id = conversationId;
      try {
        if (!id) {
          const conv = await api.createConversation();
          id = conv.id;
          skipLoadFor.current = id;
          setConversationParam(id, "replace");
        }
        runIdRef.current = "pending";
        setRuns((rs) => [...rs, emptyRun("pending", text, `local-${++localKeySeq}`)]);

        let started = false;
        let last = -1;
        let finished = false;
        try {
          await readSSE(`/api/conversations/${id}/messages`, { method: "POST", body: JSON.stringify({ text }) }, (ev) => {
            started = true;
            last = Math.max(last, ev.seq);
            if (ev.type === "run_finished") finished = true;
            // the backend titles/creates the conversation before streaming: show it in the sidebar now
            if (ev.type === "run_started") refresh();
            onEvent(ev);
          });
        } catch (e) {
          if (e instanceof ApiError) {
            // rejected before a run started (budget, busy, auth...): show it inline
            setRuns((rs) => rs.slice(0, -1));
            setLoadError(e.message);
            setInput(text);
            return;
          }
          // network drop mid-run: fall through to reconnect below
        }
        if (!finished && started && runIdRef.current !== "pending") await attach(runIdRef.current, last);
      } catch (e) {
        setLoadError(e instanceof ApiError ? e.message : "Something went wrong");
      } finally {
        setBusy(false);
        refresh();
      }
    },
    [busy, conversationId, onEvent, attach, refresh, setConversationParam],
  );

  const stop = async () => {
    if (runIdRef.current && runIdRef.current !== "pending") await api.cancel(runIdRef.current).catch(() => {});
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const desktop = window.matchMedia("(pointer: fine)").matches;
    if (e.key === "Enter" && !e.shiftKey && desktop && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send(input);
    }
  };

  const empty = runs.length === 0 && !loadingHistory;

  return (
    <div className="flex h-full flex-col">
      <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-8 px-4 py-6">
          {empty && (
            <div className="space-y-6 pt-[8vh] text-center">
              <div>
                <div className="text-3xl font-semibold tracking-tight">
                  <span className="text-accent">◆</span> {config?.agent_name ?? "Agent"}
                </div>
                <p className="mt-2 text-muted">Give me a goal. I’ll plan it, use my tools, and report back.</p>
              </div>
              <div className="mx-auto grid max-w-xl gap-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="rounded-xl border border-line bg-panel px-4 py-3 text-left text-sm text-muted transition-colors hover:border-accent hover:text-fg"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {loadingHistory ? (
            <HistorySkeleton />
          ) : (
            runs.map((r) => (
              <RunBlock
                key={r.key}
                run={r}
                onDecide={r.status === "running" ? (toolId, approved) => api.decide(r.id, toolId, approved).then(() => {}) : undefined}
              />
            ))
          )}
        </div>
      </div>

      <div className="border-t border-line bg-bg px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
        <div className="mx-auto max-w-3xl">
          {loadError && (
            <div role="alert" className="mb-2 rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-sm text-err">
              {loadError}
            </div>
          )}
          <div className="flex items-end gap-2 rounded-2xl border border-line bg-panel p-2 focus-within:border-accent">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              rows={1}
              placeholder={busy ? "Working…" : "Message your agent"}
              aria-label="Message"
              className="max-h-40 min-h-[2.5rem] flex-1 resize-none bg-transparent px-2 py-2 text-[16px] outline-none placeholder:text-muted md:text-[15px]"
            />
            {busy ? (
              <button
                onClick={stop}
                aria-label="Stop"
                className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-err/20 text-err hover:bg-err/30"
              >
                <Square size={16} fill="currentColor" />
              </button>
            ) : (
              <button
                onClick={() => send(input)}
                disabled={!input.trim()}
                aria-label="Send"
                className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent text-bg disabled:opacity-30"
              >
                <ArrowUp size={18} />
              </button>
            )}
          </div>
          <p className="mt-1.5 text-center text-[11px] text-muted">
            Limits: {config?.limits.max_steps ?? "–"} steps · ${config?.limits.max_run_cost_usd.toFixed(2) ?? "–"} per run
          </p>
        </div>
      </div>
    </div>
  );
}
