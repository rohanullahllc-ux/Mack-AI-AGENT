# Mack Agent — Personal Agentic AI

A personal AI agent with a live dashboard: it plans, calls tools, shows every step as it happens, and
enforces cost limits. Built to be used daily and shown to clients.

**Status: Phase 1 (core + dashboard).** See the full plan in `.claude/plans` / project notes.

## Architecture (phase 1)

```
Browser / phone ──► Next.js dashboard (PWA-ready, port 3100)
                        │  /api/* proxied (SSE streams through)
                        ▼
                    FastAPI backend (port 8000)
                        ├─ Agent loop (Claude, streaming, tool use, cost + step limits)
                        ├─ Tool registry (auto-registered; MCP plugs in here later)
                        ├─ RunManager (runs survive disconnects; clients re-attach by seq)
                        └─ SQLite (conversations, API-format history, runs, replayable events)
```

Key behaviours: a run keeps going if your phone drops off the network and you can re-attach to it;
every durable event is stored so any past run can be replayed in **Activity**; per-run cost/step
limits and a monthly budget cap stop runaway spend; `user_id` is on every table for a future multi-tenant path.

## Run it

Backend (Python 3.12+):

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # then put your ANTHROPIC_API_KEY in .env
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Frontend (Node 20+):

```powershell
cd frontend
npm install
npm run dev -- --port 3100    # open http://localhost:3100
```

> Windows note: if your user profile path contains `&` (e.g. `C:\Users\Name & Co`), `npx`/`npm` can fail.
> Point the npm cache somewhere else: `$env:npm_config_cache = "D:\npm-cache"`.

## Tests

```powershell
cd backend;  .\.venv\Scripts\python -m pytest        # agent loop, tools, API, SSE (fake LLM, no key needed)
cd frontend; npm test; npm run typecheck; npm run lint
```

## Configuration (`backend/.env`)

See `backend/.env.example`. The important ones: `ANTHROPIC_API_KEY`, `MODEL_MAIN`, `EFFORT`,
`MAX_RUN_COST_USD`, `MONTHLY_BUDGET_USD`, `WEB_SEARCH_ENABLED`, `APP_TOKEN` (set this before exposing the app beyond localhost).

## Adding a tool

Create `backend/app/tools/my_tool.py`: a pydantic input model + a function decorated with
`@tool(name=..., description=..., input_model=..., tier="read" | "write" | "destructive")`.
It is auto-registered, validated, timed out, and shown in the dashboard. Tiers drive the approval
flow in phase 2.

## Roadmap

1. ✅ Core agent + dashboard + run log
2. Approvals / permission tiers / audit log
3. Memory (facts + semantic search)
4. Phone access: PWA install, Cloudflare Tunnel + Access, demo deployment on free tiers
5. MCP tools (Gmail / Calendar / Drive …)
6. Sub-agents + scheduler (proactive jobs)
7. Voice
8. Portfolio polish (demo mode, video, case study)
