from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.loop import AgentRunner
from app.config import SINGLE_USER_ID, Settings, get_settings
from app.db import Database
from app.runs import RunManager, RunRejected
from app.tools.registry import load_builtin_tools

log = logging.getLogger("agent")

SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


class MessageIn(BaseModel):  # module level: FastAPI can't resolve locally-defined models under postponed annotations
    text: str = Field(min_length=1, max_length=20_000)


class ApprovalIn(BaseModel):
    approved: bool


def create_app(settings: Settings | None = None, client: Any = None) -> FastAPI:
    """`client` overrides the Anthropic client (used by tests to inject a fake)."""
    settings = settings or get_settings()
    registry = load_builtin_tools()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        orphaned = db.fail_orphaned_runs()
        if orphaned:
            log.warning("closed %d orphaned run(s) from a previous crash", orphaned)
        app.state.db = db
        app.state.runs = RunManager(settings, db, AgentRunner(settings, db, registry, client))
        yield
        db.close()

    app = FastAPI(title="Personal AI Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- auth ---------------------------------------------------------
    def require_user(request: Request) -> str:
        """Bearer-token gate. Returns the user id (single-user for now)."""
        if settings.app_token:
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), settings.app_token.encode()):
                raise HTTPException(status_code=401, detail="Invalid or missing token")
        return SINGLE_USER_ID

    def db_of(request: Request) -> Database:
        return request.app.state.db

    def runs_of(request: Request) -> RunManager:
        return request.app.state.runs

    # ---- public -------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    # ---- api ----------------------------------------------------------
    @app.get("/api/config")
    async def config(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        spent = db_of(request).month_spend(user)
        return {
            "agent_name": settings.agent_name,
            "model": settings.model_main,
            "effort": settings.effort,
            "demo_mode": settings.demo_mode,
            "api_key_configured": bool(settings.anthropic_api_key),
            "auth_required": bool(settings.app_token),
            "budget": {"monthly_usd": settings.monthly_budget_usd, "spent_usd": round(spent, 4)},
            "limits": {"max_steps": settings.max_steps, "max_run_cost_usd": settings.max_run_cost_usd},
            "approval_level": settings.approval_level,
        }

    @app.get("/api/tools")
    async def tools(user: str = Depends(require_user)) -> list[dict[str, Any]]:
        items = [
            {"name": t.name, "description": t.description, "tier": t.tier, "source": "client"}
            for t in registry.all()
        ]
        if settings.web_search_enabled:
            items.append(
                {"name": "web_search", "description": "Search the web (Anthropic-hosted).",
                 "tier": "read", "source": "server"}
            )
        return items

    @app.get("/api/conversations")
    async def list_conversations(request: Request, user: str = Depends(require_user)) -> list[dict[str, Any]]:
        return db_of(request).list_conversations(user)

    @app.post("/api/conversations")
    async def create_conversation(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        return db_of(request).create_conversation(user)

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str, request: Request, user: str = Depends(require_user)
    ) -> dict[str, Any]:
        db = db_of(request)
        conv = db.get_conversation(conversation_id, user)
        if not conv:
            raise HTTPException(404, "Conversation not found")
        runs = db.conversation_runs(conversation_id, user)
        active = next(
            (r.run_id for r in runs_of(request).active.values()
             if r.conversation_id == conversation_id and not r.done),
            None,
        )
        return {**conv, "runs": runs, "active_run_id": active}

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str, request: Request, user: str = Depends(require_user)
    ) -> dict[str, bool]:
        if not db_of(request).delete_conversation(conversation_id, user):
            raise HTTPException(404, "Conversation not found")
        return {"deleted": True}

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_message(
        conversation_id: str, body: MessageIn, request: Request, user: str = Depends(require_user)
    ) -> StreamingResponse:
        db = db_of(request)
        conv = db.get_conversation(conversation_id, user)
        if not conv:
            raise HTTPException(404, "Conversation not found")
        try:
            state = runs_of(request).start(conversation_id=conversation_id, user_id=user, text=body.text)
        except RunRejected as e:
            raise HTTPException(e.status_code, str(e))
        if conv["title"] == "New conversation":
            title = " ".join(body.text.split())[:60]
            db.rename_conversation(conversation_id, user, title)
        return StreamingResponse(
            runs_of(request).stream(state.run_id, user), media_type="text/event-stream", headers=SSE_HEADERS
        )

    @app.get("/api/runs")
    async def list_runs(
        request: Request, limit: int = Query(50, ge=1, le=200), user: str = Depends(require_user)
    ) -> list[dict[str, Any]]:
        return db_of(request).list_runs(user, limit)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        db = db_of(request)
        run = db.get_run(run_id, user)
        if not run:
            raise HTTPException(404, "Run not found")
        return {**run, "events": db.get_events(run_id)}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str, request: Request, after: int = -1, user: str = Depends(require_user)
    ) -> StreamingResponse:
        if not db_of(request).get_run(run_id, user):
            raise HTTPException(404, "Run not found")
        return StreamingResponse(
            runs_of(request).stream(run_id, user, after), media_type="text/event-stream", headers=SSE_HEADERS
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request, user: str = Depends(require_user)) -> dict[str, bool]:
        if not db_of(request).get_run(run_id, user):
            raise HTTPException(404, "Run not found")
        return {"cancelled": runs_of(request).cancel(run_id)}

    @app.post("/api/runs/{run_id}/approvals/{tool_use_id}")
    async def decide_approval(
        run_id: str, tool_use_id: str, body: ApprovalIn, request: Request, user: str = Depends(require_user)
    ) -> dict[str, bool]:
        if not db_of(request).get_run(run_id, user):
            raise HTTPException(404, "Run not found")
        if not runs_of(request).decide(run_id, tool_use_id, body.approved):
            raise HTTPException(409, "No pending approval for that action (already decided, or it timed out).")
        return {"decided": True}

    return app


app = create_app()
