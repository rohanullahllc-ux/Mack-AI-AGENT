"use client";

import { Activity, KeyRound, Menu, MessageSquare, Plus, Trash2, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, ApiError, setToken } from "@/lib/api";
import type { AppConfig, Conversation } from "@/lib/types";
import { useUrlParam } from "@/lib/useUrlParam";

interface AppCtx {
  config: AppConfig | null;
  conversations: Conversation[];
  refresh: () => Promise<void>;
  openConversation: (id: string | null) => void;
}

const Ctx = createContext<AppCtx | null>(null);
export const useApp = () => {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside <Shell>");
  return v;
};

function TokenGate({ onSaved }: { onSaved: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="flex h-dvh items-center justify-center p-4">
      <form
        className="w-full max-w-sm space-y-4 rounded-2xl border border-line bg-panel p-6"
        onSubmit={(e) => {
          e.preventDefault();
          setToken(value.trim());
          onSaved();
        }}
      >
        <div className="flex items-center gap-2 text-lg font-semibold">
          <KeyRound size={18} className="text-accent" /> Access token
        </div>
        <p className="text-sm text-muted">This agent is protected. Enter the APP_TOKEN from the backend .env.</p>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          placeholder="Token"
        />
        <button className="w-full rounded-lg bg-accent px-3 py-2 text-sm font-medium text-bg hover:opacity-90">
          Unlock
        </button>
      </form>
    </div>
  );
}

function Sidebar({ onNavigate }: { onNavigate: () => void }) {
  const { config, conversations, refresh, openConversation } = useApp();
  const pathname = usePathname();
  const [active] = useUrlParam("c");
  const spent = config?.budget.spent_usd ?? 0;
  const cap = config?.budget.monthly_usd ?? 0;
  const pct = cap > 0 ? Math.min(100, (spent / cap) * 100) : 0;

  return (
    <div className="flex h-full flex-col gap-3 p-3">
      <div className="flex items-center justify-between px-1 pt-1">
        <div className="text-lg font-semibold tracking-tight">
          <span className="text-accent">◆</span> {config?.agent_name ?? "Agent"}
        </div>
        {config?.demo_mode && (
          <span className="rounded-full border border-warn/40 px-2 text-[10px] uppercase text-warn">demo</span>
        )}
      </div>

      <button
        onClick={() => {
          openConversation(null);
          onNavigate();
        }}
        className="flex items-center gap-2 rounded-xl border border-line bg-panel2 px-3 py-2 text-sm hover:border-accent"
      >
        <Plus size={16} /> New chat
      </button>

      <nav className="flex gap-1 text-sm">
        <Link
          href="/"
          onClick={onNavigate}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 ${pathname === "/" ? "bg-panel2 text-fg" : "text-muted hover:text-fg"}`}
        >
          <MessageSquare size={14} /> Chat
        </Link>
        <Link
          href="/activity"
          onClick={onNavigate}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 ${pathname === "/activity" ? "bg-panel2 text-fg" : "text-muted hover:text-fg"}`}
        >
          <Activity size={14} /> Activity
        </Link>
      </nav>

      <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto pr-1">
        {conversations.length === 0 && <p className="px-2 py-3 text-xs text-muted">No conversations yet.</p>}
        {conversations.map((c) => (
          <div
            key={c.id}
            className={`group flex items-center gap-1 rounded-lg ${active === c.id && pathname === "/" ? "bg-panel2" : "hover:bg-panel"}`}
          >
            <button
              onClick={() => {
                openConversation(c.id);
                onNavigate();
              }}
              className="min-w-0 flex-1 truncate px-2.5 py-2 text-left text-sm"
              title={c.title}
            >
              {c.title}
            </button>
            <button
              aria-label={`Delete ${c.title}`}
              onClick={async () => {
                if (!confirm(`Delete "${c.title}"?`)) return;
                await api.deleteConversation(c.id);
                if (active === c.id) openConversation(null);
                refresh();
              }}
              className="mr-1 rounded p-1.5 text-muted opacity-100 hover:text-err md:opacity-0 md:group-hover:opacity-100"
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </div>

      {config && (
        <div className="rounded-xl border border-line bg-panel p-3 text-xs">
          <div className="mb-1.5 flex justify-between text-muted">
            <span>Monthly budget</span>
            <span>
              ${spent.toFixed(2)} / ${cap.toFixed(0)}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-line">
            <div
              className={`h-full rounded-full ${pct > 85 ? "bg-err" : pct > 60 ? "bg-warn" : "bg-accent"}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="mt-2 truncate text-muted">
            {config.model} · effort {config.effort}
          </div>
        </div>
      )}
    </div>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [, setParam] = useUrlParam("c");
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [needsToken, setNeedsToken] = useState(false);
  const [backendDown, setBackendDown] = useState(false);
  const [drawer, setDrawer] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [cfg, convs] = await Promise.all([api.config(), api.conversations()]);
      setConfig(cfg);
      setConversations(convs);
      setNeedsToken(false);
      setBackendDown(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setNeedsToken(true);
      else setBackendDown(true);
    }
  }, []);

  useEffect(() => {
    // fetch-on-mount: state is only set after the awaited responses arrive
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  const openConversation = useCallback(
    (id: string | null) => {
      if (pathname === "/") setParam(id);
      else router.push(id ? `/?c=${id}` : "/");
    },
    [pathname, router, setParam],
  );

  const value = useMemo(
    () => ({ config, conversations, refresh, openConversation }),
    [config, conversations, refresh, openConversation],
  );

  if (needsToken) return <TokenGate onSaved={refresh} />;

  return (
    <Ctx.Provider value={value}>
      <div className="flex h-dvh">
        <aside className="hidden w-72 shrink-0 border-r border-line bg-panel/50 md:block">
          <Sidebar onNavigate={() => {}} />
        </aside>

        {drawer && (
          <div className="fixed inset-0 z-40 md:hidden">
            <div className="absolute inset-0 bg-black/60" onClick={() => setDrawer(false)} />
            <aside className="absolute inset-y-0 left-0 w-[85%] max-w-xs border-r border-line bg-bg">
              <button
                aria-label="Close menu"
                onClick={() => setDrawer(false)}
                className="absolute right-2 top-2 rounded p-2 text-muted"
              >
                <X size={18} />
              </button>
              <Sidebar onNavigate={() => setDrawer(false)} />
            </aside>
          </div>
        )}

        <main className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center gap-2 border-b border-line px-3 py-2 md:hidden">
            <button aria-label="Open menu" onClick={() => setDrawer(true)} className="rounded p-2 text-muted">
              <Menu size={20} />
            </button>
            <span className="font-semibold">
              <span className="text-accent">◆</span> {config?.agent_name ?? "Agent"}
            </span>
          </header>
          {backendDown && (
            <div className="border-b border-err/30 bg-err/10 px-4 py-2 text-sm text-err">
              Can’t reach the agent backend. Start it with <code>uvicorn app.main:app</code> in /backend.
            </div>
          )}
          {config && !config.api_key_configured && (
            <div className="border-b border-warn/30 bg-warn/10 px-4 py-2 text-sm text-warn">
              No ANTHROPIC_API_KEY configured — set it in backend/.env and restart the backend.
            </div>
          )}
          <div className="min-h-0 flex-1">{children}</div>
        </main>
      </div>
    </Ctx.Provider>
  );
}
