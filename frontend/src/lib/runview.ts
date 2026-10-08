import type { AgentEvent, RunRecord, RunView, Step } from "./types";

export function emptyRun(id: string, userText: string, key: string = id): RunView {
  return {
    key,
    id,
    userText,
    status: "running",
    steps: [],
    costUsd: 0,
    modelSteps: 0,
    inputTokens: 0,
    outputTokens: 0,
    lastSeq: -1,
  };
}

/** Pure reducer: fold one event into a run view. Same code renders live streams and stored history. */
export function applyEvent(run: RunView, ev: AgentEvent): RunView {
  const d = ev.data;
  let steps = run.steps;
  const next: RunView = { ...run, lastSeq: Math.max(run.lastSeq, ev.seq) };

  const appendDelta = (kind: "text" | "thinking") => {
    const last = steps[steps.length - 1];
    if (last && last.kind === kind && last.live) {
      steps = [...steps.slice(0, -1), { ...last, text: last.text + d.text }];
    } else {
      steps = [...steps, { kind, text: d.text, live: true }];
    }
  };

  const finalize = (kind: "text" | "thinking") => {
    // replace the first still-live step of this kind with the authoritative full text
    const i = steps.findIndex((s) => s.kind === kind && "live" in s && s.live);
    if (i >= 0) {
      steps = steps.map((s, j) => (j === i ? ({ kind, text: d.text, live: false } as Step) : s));
    } else {
      steps = [...steps, { kind, text: d.text, live: false }];
    }
  };

  switch (ev.type) {
    case "run_started":
      next.status = "running";
      next.id = d.run_id ?? run.id;
      break;
    case "text_delta":
      appendDelta("text");
      break;
    case "thinking_delta":
      appendDelta("thinking");
      break;
    case "text":
      finalize("text");
      break;
    case "thinking":
      finalize("thinking");
      break;
    case "reset":
      steps = steps.filter((s) => !(s.kind === "text" || s.kind === "thinking") || !s.live);
      break;
    case "tool_call":
      steps = [
        ...steps,
        {
          kind: "tool",
          id: d.id,
          name: d.name,
          input: d.input,
          source: d.source,
          tier: d.tier ?? null,
          status: "running",
        },
      ];
      break;
    case "approval_request":
      steps = steps.map((s) =>
        s.kind === "tool" && s.id === d.id ? { ...s, status: "awaiting_approval", approvalTimeoutS: d.timeout_s } : s,
      );
      break;
    case "approval_decision":
      // answered (or timed out): the tool runs next if approved; either way tool_result settles the step
      steps = steps.map((s) =>
        s.kind === "tool" && s.id === d.id && s.status === "awaiting_approval" ? { ...s, status: "running" } : s,
      );
      break;
    case "tool_result":
      steps = steps.map((s) =>
        s.kind === "tool" && s.id === d.id
          ? {
              ...s,
              status: d.denied ? "denied" : d.is_error ? "error" : "done",
              output: d.output,
              durationMs: d.duration_ms,
            }
          : s,
      );
      break;
    case "usage":
      next.costUsd = d.run_cost_usd ?? next.costUsd;
      next.modelSteps = d.step ?? next.modelSteps;
      next.inputTokens += d.input_tokens ?? 0;
      next.outputTokens += d.output_tokens ?? 0;
      break;
    case "notice":
      steps = [...steps, { kind: "notice", message: d.message }];
      break;
    case "error":
      steps = [...steps, { kind: "error", message: d.message }];
      break;
    case "run_finished":
      next.status = d.status;
      next.costUsd = d.cost_usd ?? next.costUsd;
      next.modelSteps = d.steps ?? next.modelSteps;
      steps = steps.map((s) =>
        s.kind === "tool" && (s.status === "running" || s.status === "awaiting_approval")
          ? {
              ...s,
              status: "error",
              output: s.output ?? (s.status === "awaiting_approval" ? "(run ended before this was approved)" : "(no result recorded)"),
            }
          : s.kind === "text" || s.kind === "thinking"
            ? { ...s, live: false }
            : s,
      );
      break;
  }
  next.steps = steps;
  return next;
}

export function runFromRecord(rec: RunRecord, events: AgentEvent[] = rec.events ?? []): RunView {
  let view = emptyRun(rec.id, rec.user_text);
  view.status = rec.status;
  view.startedAt = rec.started_at;
  view.finishedAt = rec.finished_at;
  for (const ev of events) view = applyEvent(view, ev);
  return { ...view, status: rec.status, costUsd: rec.cost_usd };
}
