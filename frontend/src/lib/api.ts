import type {
  AgentEvent,
  AppConfig,
  Conversation,
  ConversationDetail,
  RunRecord,
  ToolInfo,
} from "./types";

const TOKEN_KEY = "agent_token";

export const getToken = () => {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
};
export const setToken = (t: string) => {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: token just won't persist */
  }
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function headers(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  const t = getToken();
  if (t) h.set("Authorization", `Bearer ${t}`);
  return h;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: headers({ "Content-Type": "application/json", ...(init.headers as object) }),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, String(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  tools: () => request<ToolInfo[]>("/api/tools"),
  conversations: () => request<Conversation[]>("/api/conversations"),
  createConversation: () => request<Conversation>("/api/conversations", { method: "POST" }),
  conversation: (id: string) => request<ConversationDetail>(`/api/conversations/${id}`),
  deleteConversation: (id: string) => request<{ deleted: boolean }>(`/api/conversations/${id}`, { method: "DELETE" }),
  runs: (limit = 100) => request<RunRecord[]>(`/api/runs?limit=${limit}`),
  run: (id: string) => request<RunRecord>(`/api/runs/${id}`),
  cancel: (id: string) => request<{ cancelled: boolean }>(`/api/runs/${id}/cancel`, { method: "POST" }),
  decide: (runId: string, toolUseId: string, approved: boolean) =>
    request<{ decided: boolean }>(`/api/runs/${runId}/approvals/${encodeURIComponent(toolUseId)}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),
};

/**
 * Read a Server-Sent-Events response (fetch-based, so we can POST and send auth headers).
 * Resolves when the stream closes; throws ApiError on a non-2xx status.
 */
export async function readSSE(
  path: string,
  init: RequestInit,
  onEvent: (ev: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(path, {
    ...init,
    signal,
    headers: headers({ "Content-Type": "application/json", ...(init.headers as object) }),
    cache: "no-store",
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, String(detail));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let id = -1;
      let type = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith(":")) continue; // keep-alive comment
        if (line.startsWith("id: ")) id = Number(line.slice(4));
        else if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent({ seq: id, type, data: JSON.parse(data) });
    }
  }
}
