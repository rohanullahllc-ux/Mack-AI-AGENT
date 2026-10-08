import assert from "node:assert/strict";
import { test } from "node:test";
import { applyEvent, emptyRun, runFromRecord } from "../src/lib/runview.ts";
import type { AgentEvent, RunRecord, RunView, Step } from "../src/lib/types.ts";

let seq = 0;
const ev = (type: string, data: Record<string, unknown> = {}): AgentEvent => ({ seq: seq++, type, data });
const fold = (events: AgentEvent[]) => events.reduce(applyEvent, emptyRun("pending", "hi"));

test("live deltas build one step, final text event replaces it", () => {
  const run = fold([
    ev("run_started", { run_id: "r1" }),
    ev("text_delta", { text: "Hel" }),
    ev("text_delta", { text: "lo" }),
  ]);
  assert.equal(run.id, "r1");
  assert.deepEqual(run.steps, [{ kind: "text", text: "Hello", live: true }]);

  const done = applyEvent(run, ev("text", { text: "Hello!" }));
  assert.deepEqual(done.steps, [{ kind: "text", text: "Hello!", live: false }]);
});

test("React key stays stable when the server assigns the real run id", () => {
  const start = emptyRun("pending", "hi", "local-1");
  const run = applyEvent(start, ev("run_started", { run_id: "r1" }));
  assert.equal(run.id, "r1");
  assert.equal(run.key, "local-1"); // a changing key would remount the block and collapse open panels
});

test("thinking + text stream in order and finalize against the right live step", () => {
  const run = fold([
    ev("thinking_delta", { text: "hm" }),
    ev("text_delta", { text: "ans" }),
    // end-of-message events arrive thinking-first, matching block order
    ev("thinking", { text: "hmm full" }),
    ev("text", { text: "answer" }),
  ]);
  assert.deepEqual(run.steps, [
    { kind: "thinking", text: "hmm full", live: false },
    { kind: "text", text: "answer", live: false },
  ]);
});

test("tool call then result updates the same step", () => {
  const run = fold([
    ev("tool_call", { id: "t1", name: "write_note", input: { title: "a" }, source: "client", tier: "write" }),
    ev("tool_result", { id: "t1", output: "Saved", is_error: false, duration_ms: 12 }),
  ]);
  const step = run.steps[0];
  assert.equal(step.kind, "tool");
  if (step.kind === "tool") {
    assert.equal(step.status, "done");
    assert.equal(step.output, "Saved");
    assert.equal(step.durationMs, 12);
  }
});

test("failed tool is marked error; unfinished tools are closed out on run_finished", () => {
  const run = fold([
    ev("tool_call", { id: "a", name: "x", input: {}, source: "client" }),
    ev("tool_call", { id: "b", name: "y", input: {}, source: "client" }),
    ev("tool_result", { id: "a", output: "boom", is_error: true }),
    ev("run_finished", { status: "cancelled", cost_usd: 0.01, steps: 1 }),
  ]);
  const [a, b] = run.steps;
  assert.equal(a.kind === "tool" && a.status, "error");
  assert.equal(b.kind === "tool" && b.status, "error"); // never left spinning forever
  assert.equal(run.status, "cancelled");
});

test("usage accumulates tokens and tracks cost", () => {
  const run = fold([
    ev("usage", { step: 1, input_tokens: 100, output_tokens: 10, run_cost_usd: 0.001 }),
    ev("usage", { step: 2, input_tokens: 200, output_tokens: 20, run_cost_usd: 0.003 }),
  ]);
  assert.equal(run.inputTokens, 300);
  assert.equal(run.outputTokens, 30);
  assert.equal(run.costUsd, 0.003);
  assert.equal(run.modelSteps, 2);
});

test("reset drops live steps but keeps finalized ones", () => {
  const run = fold([
    ev("tool_call", { id: "t", name: "x", input: {}, source: "client" }),
    ev("text_delta", { text: "partial" }),
    ev("reset"),
  ]);
  assert.equal(run.steps.length, 1);
  assert.equal(run.steps[0].kind, "tool");
});

test("history (no deltas) renders from stored events", () => {
  const rec: RunRecord = {
    id: "r9",
    conversation_id: "c",
    user_text: "q",
    status: "completed",
    model: "m",
    steps: 1,
    input_tokens: 5,
    output_tokens: 5,
    cache_read_tokens: 0,
    cost_usd: 0.002,
    error: null,
    started_at: 1,
    finished_at: 2,
    events: [
      { seq: 0, type: "run_started", data: { run_id: "r9" } },
      { seq: 2, type: "text", data: { text: "stored answer" } },
      { seq: 3, type: "run_finished", data: { status: "completed", cost_usd: 0.002, steps: 1 } },
    ],
  };
  const view = runFromRecord(rec);
  assert.deepEqual(view.steps, [{ kind: "text", text: "stored answer", live: false }]);
  assert.equal(view.status, "completed");
  assert.equal(view.costUsd, 0.002);
});

// ---- approvals ---------------------------------------------------------------

const call = () => ev("tool_call", { id: "t1", name: "write_note", input: { title: "a" }, source: "client", tier: "write" });
const request = () => ev("approval_request", { id: "t1", name: "write_note", tier: "write", timeout_s: 300 });
const decision = (data: Record<string, unknown>, ts?: number): AgentEvent => ({
  ...ev("approval_decision", { id: "t1", ...data }),
  ts,
});
const toolStep = (run: RunView, i = 0) => {
  const step = run.steps[i];
  assert.equal(step.kind, "tool");
  return step as Extract<Step, { kind: "tool" }>;
};

test("an approval request parks the tool call until it is answered", () => {
  const step = toolStep(fold([call(), request()]));
  assert.equal(step.status, "awaiting_approval");
  assert.equal(step.approvalTimeoutS, 300);
  assert.equal(step.approval, undefined);
});

test("approving resumes the tool, records how it was answered, and the result settles it", () => {
  const answered = fold([call(), request(), decision({ approved: true, reason: "", outcome: "approved" }, 1_700_000_000)]);
  assert.equal(toolStep(answered).status, "running");
  assert.deepEqual(toolStep(answered).approval, { outcome: "approved", reason: "", ts: 1_700_000_000 });

  const done = fold([
    call(),
    request(),
    decision({ approved: true, outcome: "approved" }),
    ev("tool_result", { id: "t1", output: "Saved", is_error: false }),
  ]);
  assert.equal(toolStep(done).status, "done");
  assert.equal(toolStep(done).approval?.outcome, "approved");
});

test("a denial and a timeout are told apart, and neither runs the tool", () => {
  const denied = fold([
    call(),
    request(),
    decision({ approved: false, reason: "the user denied this action", outcome: "denied" }),
    ev("tool_result", { id: "t1", output: "Not run (the user denied this action).", is_error: true, denied: true }),
  ]);
  assert.equal(toolStep(denied).status, "denied");
  assert.deepEqual(toolStep(denied).approval, { outcome: "denied", reason: "the user denied this action", ts: undefined });

  const timedOut = fold([
    call(),
    request(),
    decision({ approved: false, reason: "no response within 300s", outcome: "timed_out" }),
    ev("tool_result", { id: "t1", output: "Not run (no response within 300s).", is_error: true, denied: true }),
  ]);
  assert.equal(toolStep(timedOut).status, "denied");
  assert.equal(toolStep(timedOut).approval?.outcome, "timed_out");
});

test("a cancelled request stays open until the run ends, then reads as never approved", () => {
  const cancelled = decision({ approved: false, reason: "the run was cancelled", outcome: "cancelled" });
  const mid = toolStep(fold([call(), request(), cancelled]));
  assert.equal(mid.status, "awaiting_approval"); // no tool_result is coming; the end of the run settles it
  assert.equal(mid.approval?.outcome, "cancelled");

  const end = toolStep(fold([call(), request(), cancelled, ev("run_finished", { status: "cancelled", steps: 1 })]));
  assert.equal(end.status, "error");
  assert.equal(end.output, "(run ended before this was approved)");
  assert.equal(end.approval?.outcome, "cancelled");
});

test("runs stored before decisions carried an outcome fall back to the yes/no", () => {
  assert.equal(toolStep(fold([call(), request(), decision({ approved: true })])).approval?.outcome, "approved");
  assert.equal(
    toolStep(fold([call(), request(), decision({ approved: false, reason: "no response within 300s" })])).approval?.outcome,
    "denied",
  );
});

test("a decision for a call we have never seen changes nothing", () => {
  const before = fold([call(), request()]);
  const after = applyEvent(before, { ...decision({ approved: true, outcome: "approved" }), data: { id: "ghost", approved: true } });
  assert.deepEqual(after.steps, before.steps);
});

test("replayed runs keep the time each approval was answered", () => {
  const rec: RunRecord = {
    id: "r7",
    conversation_id: "c",
    user_text: "q",
    status: "completed",
    model: "m",
    steps: 1,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cost_usd: 0,
    error: null,
    started_at: 1,
    finished_at: 2,
    events: [call(), request(), decision({ approved: true, outcome: "approved" }, 1_700_000_123)],
  };
  assert.equal(toolStep(runFromRecord(rec)).approval?.ts, 1_700_000_123);
});
