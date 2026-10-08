export type RunStatus =
  | "running"
  | "completed"
  | "error"
  | "cancelled"
  | "limit"
  | "refused"
  | "budget_exceeded";

// Event payloads are produced by our own backend and discriminated by `type`; a loose map keeps the reducer readable.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = Record<string, any>;

export interface AgentEvent {
  seq: number;
  type: string;
  data: Json;
  /** When the server stored the event (epoch seconds). Only on replayed events, not on the live stream. */
  ts?: number;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface RunRecord {
  id: string;
  conversation_id: string;
  user_text: string;
  status: RunStatus;
  model: string;
  steps: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
  error: string | null;
  started_at: number;
  finished_at: number | null;
  events?: AgentEvent[];
}

export interface ConversationDetail extends Conversation {
  runs: RunRecord[];
  active_run_id: string | null;
}

export interface AppConfig {
  agent_name: string;
  model: string;
  effort: string;
  demo_mode: boolean;
  api_key_configured: boolean;
  auth_required: boolean;
  budget: { monthly_usd: number; spent_usd: number };
  limits: { max_steps: number; max_run_cost_usd: number };
  approval_level: "write" | "destructive" | "never";
}

export interface ToolInfo {
  name: string;
  description: string;
  tier: "read" | "write" | "destructive";
  source: "client" | "server";
}

// ---- audit log: who allowed what ------------------------------------------

/** pending = waiting for an answer; auto = the approval policy let it run without asking. */
export type AuditOutcome = "pending" | "auto" | "approved" | "denied" | "timed_out" | "cancelled";
export type ToolTier = ToolInfo["tier"];

export interface AuditEntry {
  id: number;
  run_id: string;
  tool_use_id: string;
  tool: string;
  tier: ToolTier;
  /** Compact JSON of the call's input, capped in length by the server. */
  input_preview: string;
  outcome: AuditOutcome;
  reason: string;
  /** Whether the tool then ran: null means it never did (denied, timed out, cancelled, still waiting). */
  result: "ok" | "error" | "interrupted" | null;
  requested_at: number;
  decided_at: number | null;
  /** The run's prompt, while its conversation still exists. */
  run_text: string | null;
}

export interface AuditPage {
  items: AuditEntry[];
  /** Rows matching the filters (not just this page). */
  total: number;
  /** Rows per outcome across the whole log, whatever the filters. */
  counts: Partial<Record<AuditOutcome, number>>;
}

export interface AuditQuery {
  limit?: number;
  offset?: number;
  outcome?: AuditOutcome;
  tool?: string;
  tier?: ToolTier;
  run_id?: string;
}

// ---- a run, folded from its events, ready to render ----------------------

/** How an approval request ended, as recorded on the tool step it was about. */
export interface ApprovalInfo {
  outcome: "approved" | "denied" | "timed_out" | "cancelled";
  reason: string;
  /** When it was answered; only known for replayed runs. */
  ts?: number;
}

export type Step =
  | { kind: "thinking"; text: string; live: boolean }
  | { kind: "text"; text: string; live: boolean }
  | {
      kind: "tool";
      id: string;
      name: string;
      input: Json;
      source: "client" | "server";
      tier: string | null;
      status: "running" | "awaiting_approval" | "done" | "error" | "denied";
      output?: string;
      durationMs?: number;
      approvalTimeoutS?: number;
      approval?: ApprovalInfo;
    }
  | { kind: "notice"; message: string }
  | { kind: "error"; message: string };

export interface RunView {
  /** Stable React key: unlike `id`, it never changes when the server assigns the real run id. */
  key: string;
  id: string;
  userText: string;
  status: RunStatus;
  steps: Step[];
  costUsd: number;
  modelSteps: number;
  inputTokens: number;
  outputTokens: number;
  lastSeq: number;
  startedAt?: number;
  finishedAt?: number | null;
}
