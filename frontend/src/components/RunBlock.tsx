"use client";

import { AlertTriangle, Ban, Brain, Check, ChevronRight, Globe, Info, Loader2, ShieldAlert, Wrench, X } from "lucide-react";
import { useState } from "react";
import type { RunView, Step } from "@/lib/types";
import { Markdown } from "./Markdown";

const TIER_STYLE: Record<string, string> = {
  read: "text-ok border-ok/30",
  write: "text-warn border-warn/30",
  destructive: "text-err border-err/30",
};

export const fmtCost = (usd: number) => (usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(3)}`);

type ToolStepData = Extract<Step, { kind: "tool" }>;
/** Answers an approval request; rejects if the server could not take the answer. */
export type DecideFn = (toolUseId: string, approved: boolean) => Promise<void>;

const fmtTimeout = (s: number) => (s >= 60 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);

function TierBadge({ tier }: { tier: string }) {
  return (
    <span className={`rounded-full border px-1.5 text-[10px] uppercase ${TIER_STYLE[tier] ?? "text-muted border-line"}`}>
      {tier}
    </span>
  );
}

/** A risky tool call is parked until the user answers; this is where they do. */
function ApprovalCard({ step, onDecide }: { step: ToolStepData; onDecide?: DecideFn }) {
  const [sending, setSending] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decide = async (approved: boolean) => {
    if (!onDecide || sending) return;
    setSending(approved ? "approve" : "deny");
    setError(null);
    try {
      await onDecide(step.id, approved);
    } catch (e) {
      setSending(null); // let them try again
      setError(e instanceof Error ? e.message : "Could not send your answer");
    }
  };

  return (
    <div role="group" aria-label={`Approval needed for ${step.name}`} className="space-y-3 rounded-xl border border-warn/40 bg-warn/10 p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <ShieldAlert size={15} className="shrink-0 text-warn" />
        <span className="font-medium">Approval needed</span>
        <span className="font-mono text-[13px]">{step.name}</span>
        {step.tier && <TierBadge tier={step.tier} />}
      </div>
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-bg/60 p-2 font-mono text-xs">
        {JSON.stringify(step.input, null, 2)}
      </pre>
      {onDecide ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => decide(true)}
            disabled={sending !== null}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-bg hover:opacity-90 disabled:opacity-50"
          >
            {sending === "approve" ? "Approving…" : "Approve"}
          </button>
          <button
            onClick={() => decide(false)}
            disabled={sending !== null}
            className="rounded-lg border border-line bg-panel px-4 py-2 text-sm hover:border-err hover:text-err disabled:opacity-50"
          >
            {sending === "deny" ? "Denying…" : "Deny"}
          </button>
          {step.approvalTimeoutS !== undefined && (
            <span className="text-xs text-muted">Denied automatically after {fmtTimeout(step.approvalTimeoutS)} without an answer.</span>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted">Waiting for approval. Open this conversation in Chat to answer.</p>
      )}
      {error && (
        <p role="alert" className="text-xs text-err">
          {error}
        </p>
      )}
    </div>
  );
}

function ToolStep({ step, onDecide }: { step: ToolStepData; onDecide?: DecideFn }) {
  if (step.status === "awaiting_approval") return <ApprovalCard step={step} onDecide={onDecide} />;
  const Icon = step.source === "server" ? Globe : Wrench;
  const status =
    step.status === "running" ? (
      <Loader2 size={14} className="animate-spin text-accent" aria-label="running" />
    ) : step.status === "done" ? (
      <Check size={14} className="text-ok" aria-label="done" />
    ) : step.status === "denied" ? (
      <Ban size={14} className="text-warn" aria-label="denied" />
    ) : (
      <X size={14} className="text-err" aria-label="failed" />
    );
  const summary = Object.entries(step.input)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" · ");
  return (
    <details className="group rounded-xl border border-line bg-panel">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
        <ChevronRight size={14} className="shrink-0 text-muted transition-transform group-open:rotate-90" />
        <Icon size={14} className="shrink-0 text-accent2" />
        <span className="font-mono text-[13px]">{step.name}</span>
        {step.tier && <TierBadge tier={step.tier} />}
        <span className="min-w-0 flex-1 truncate text-xs text-muted">{summary}</span>
        {step.durationMs !== undefined && <span className="text-xs text-muted">{step.durationMs}ms</span>}
        {status}
      </summary>
      <div className="space-y-2 border-t border-line px-3 py-2 text-xs">
        <div>
          <div className="mb-1 text-muted">Input</div>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono">
            {JSON.stringify(step.input, null, 2)}
          </pre>
        </div>
        {step.output !== undefined && (
          <div>
            <div className="mb-1 text-muted">
              {step.status === "error" ? "Error" : step.status === "denied" ? "Denied" : "Result"}
            </div>
            <pre
              className={`max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono ${step.status === "error" ? "text-err" : ""}`}
            >
              {step.output}
            </pre>
          </div>
        )}
      </div>
    </details>
  );
}

/** Open while streaming, collapsed once done — until the user toggles it, after which their choice wins. */
function ThinkingStep({ step }: { step: Extract<Step, { kind: "thinking" }> }) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? step.live;
  return (
    <details open={open} className="group">
      <summary
        onClick={(e) => {
          e.preventDefault(); // we own the open state; the native toggle would fight React
          setUserOpen(!open);
        }}
        className="flex cursor-pointer list-none items-center gap-1.5 text-xs text-muted [&::-webkit-details-marker]:hidden"
      >
        <ChevronRight size={12} className="transition-transform group-open:rotate-90" />
        <Brain size={13} />
        {step.live ? "Thinking…" : "Thought process"}
      </summary>
      <p className="mt-1.5 whitespace-pre-wrap border-l-2 border-line pl-3 text-[13px] leading-relaxed text-muted">
        {step.text}
      </p>
    </details>
  );
}

function StepView({ step, onDecide }: { step: Step; onDecide?: DecideFn }) {
  switch (step.kind) {
    case "thinking":
      return <ThinkingStep step={step} />;
    case "text":
      return <Markdown live={step.live}>{step.text}</Markdown>;
    case "tool":
      return <ToolStep step={step} onDecide={onDecide} />;
    case "notice":
      return (
        <div className="flex items-start gap-2 text-xs text-muted">
          <Info size={14} className="mt-0.5 shrink-0" />
          {step.message}
        </div>
      );
    case "error":
      return (
        <div className="flex items-start gap-2 rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-sm text-err">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          {step.message}
        </div>
      );
  }
}

export function RunBlock({
  run,
  showUser = true,
  onDecide,
}: {
  run: RunView;
  showUser?: boolean;
  /** When given, pending approvals show Approve/Deny buttons (live chat); omitted for read-only replays. */
  onDecide?: DecideFn;
}) {
  const running = run.status === "running";
  return (
    <section className="space-y-3">
      {showUser && (
        <div className="flex justify-end">
          <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent/15 px-4 py-2.5 text-[15px]">
            {run.userText}
          </div>
        </div>
      )}
      <div className="space-y-3">
        {run.steps.map((s, i) => (
          <StepView key={i} step={s} onDecide={onDecide} />
        ))}
        {running && run.steps.length === 0 && (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Loader2 size={14} className="animate-spin" /> Working…
          </div>
        )}
      </div>
      {!running && (
        <div className="flex flex-wrap gap-x-3 text-[11px] text-muted">
          <span>{run.status}</span>
          {run.modelSteps > 0 && <span>{run.modelSteps} model call{run.modelSteps > 1 ? "s" : ""}</span>}
          {run.costUsd > 0 && <span>{fmtCost(run.costUsd)}</span>}
          {run.inputTokens + run.outputTokens > 0 && (
            <span>{(run.inputTokens + run.outputTokens).toLocaleString()} tokens</span>
          )}
        </div>
      )}
    </section>
  );
}
