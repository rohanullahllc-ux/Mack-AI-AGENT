"""Run lifecycle: a run executes as a background task, independent of any HTTP connection.

Clients (browser, phone) attach to a run's event stream and can drop and re-attach at will;
the run keeps going and every durable event is stored in SQLite for replay.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import anthropic

from app.agent.loop import AgentRunner, ApprovalDecision, RunStats
from app.config import Settings
from app.db import Database, new_id

log = logging.getLogger(__name__)
KEEPALIVE_S = 15
KEEP_FINISHED_S = 300


class RunRejected(Exception):
    """Raised when a run cannot start (budget exhausted, conversation busy, ...)."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class RunState:
    run_id: str
    conversation_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None
    approvals: dict[str, asyncio.Future[ApprovalDecision]] = field(default_factory=dict)  # by tool_use id


def sse(ev: dict[str, Any]) -> str:
    return f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev['data'])}\n\n"


class RunManager:
    def __init__(self, settings: Settings, db: Database, runner: AgentRunner):
        self.settings = settings
        self.db = db
        self.runner = runner
        self.active: dict[str, RunState] = {}

    # ------------------------------------------------------------------
    def start(self, *, conversation_id: str, user_id: str, text: str) -> RunState:
        if any(s.conversation_id == conversation_id and not s.done for s in self.active.values()):
            raise RunRejected("This conversation already has a run in progress.")
        spent = self.db.month_spend(user_id)
        if spent >= self.settings.monthly_budget_usd:
            raise RunRejected(
                f"Monthly budget of ${self.settings.monthly_budget_usd:.2f} reached "
                f"(spent ${spent:.2f}). Raise MONTHLY_BUDGET_USD to continue.",
                status_code=402,
            )
        run_id = new_id()
        self.db.create_run(run_id, conversation_id, user_id, text, self.settings.model_main)
        state = RunState(run_id=run_id, conversation_id=conversation_id)
        self.active[run_id] = state
        state.task = asyncio.create_task(self._execute(state, user_id, text))
        return state

    def cancel(self, run_id: str) -> bool:
        state = self.active.get(run_id)
        if state and not state.done and state.task:
            state.task.cancel()
            return True
        return False

    def decide(self, run_id: str, tool_use_id: str, approved: bool) -> bool:
        """Resolve a pending approval. False if the run or the request is gone (already decided, timed out)."""
        state = self.active.get(run_id)
        fut = state.approvals.get(tool_use_id) if state else None
        if fut is None or fut.done():
            return False
        fut.set_result(ApprovalDecision(approved, "" if approved else "the user denied this action"))
        return True

    async def _ask(
        self, state: RunState, tool_use_id: str, name: str, tool_input: dict[str, Any], tier: str
    ) -> ApprovalDecision:
        """Park the run until the user approves or denies a tool call (or the timeout lapses = denied)."""
        timeout = self.settings.approval_timeout_s
        fut: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        state.approvals[tool_use_id] = fut  # register before announcing, so a fast client can't miss it
        await self._emit(
            state,
            "approval_request",
            {"id": tool_use_id, "name": name, "input": tool_input, "tier": tier, "timeout_s": timeout},
        )
        try:
            decision = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            decision = ApprovalDecision(False, f"no response within {timeout:.0f}s")
        finally:
            state.approvals.pop(tool_use_id, None)
        await self._emit(
            state,
            "approval_decision",
            {"id": tool_use_id, "approved": decision.approved, "reason": decision.reason},
        )
        return decision

    # ------------------------------------------------------------------
    async def _emit(self, state: RunState, type_: str, data: dict[str, Any], persist: bool = True) -> None:
        seq = len(state.events)
        state.events.append({"seq": seq, "type": type_, "data": data})
        if persist:
            self.db.add_event(state.run_id, seq, type_, data)
        async with state.cond:
            state.cond.notify_all()

    async def _execute(self, state: RunState, user_id: str, text: str) -> None:
        async def emit(type_: str, data: dict[str, Any], persist: bool = True) -> None:
            await self._emit(state, type_, data, persist)

        async def approve(tool_use_id: str, name: str, tool_input: dict[str, Any], tier: str) -> ApprovalDecision:
            return await self._ask(state, tool_use_id, name, tool_input, tier)

        # owned here and filled in by the runner, so cost already spent is still recorded if the run
        # ends in an exception or a cancellation
        stats = RunStats()
        await emit("run_started", {"run_id": state.run_id, "conversation_id": state.conversation_id})
        try:
            await self.runner.run(
                conversation_id=state.conversation_id,
                user_id=user_id,
                run_id=state.run_id,
                user_text=text,
                emit=emit,
                approve=approve,
                stats=stats,
            )
        except asyncio.CancelledError:
            stats.status, stats.error = "cancelled", "Cancelled by user."
        except anthropic.AuthenticationError:
            stats.status = "error"
            stats.error = "Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY in backend/.env."
            await emit("error", {"message": stats.error})
        except anthropic.RateLimitError:
            stats.status, stats.error = "error", "Rate limited by the Anthropic API. Try again shortly."
            await emit("error", {"message": stats.error})
        except anthropic.APIConnectionError:
            stats.status, stats.error = "error", "Could not reach the Anthropic API (network error)."
            await emit("error", {"message": stats.error})
        except anthropic.APIStatusError as e:
            stats.status, stats.error = "error", f"Anthropic API error {e.status_code}: {e.message}"
            await emit("error", {"message": stats.error})
        except Exception as e:  # last resort: surface it, never leave the run hanging
            stats.status = "error"
            if "Could not resolve authentication method" in str(e):  # SDK raises TypeError when no key is set
                stats.error = "No Anthropic API key found. Set ANTHROPIC_API_KEY in backend/.env and restart."
            else:
                log.exception("run %s crashed", state.run_id)
                stats.error = f"Unexpected error: {type(e).__name__}: {e}"
            await emit("error", {"message": stats.error})
        finally:
            if stats.status == "running":
                stats.status = "error"
            self.db.finish_run(state.run_id, stats.status, stats.as_dict(), stats.error)
            await emit("run_finished", {"status": stats.status, **stats.as_dict()})
            state.done = True
            async with state.cond:
                state.cond.notify_all()
            asyncio.get_running_loop().call_later(KEEP_FINISHED_S, self.active.pop, state.run_id, None)

    # ------------------------------------------------------------------
    async def stream(self, run_id: str, user_id: str, after: int = -1) -> AsyncIterator[str]:
        """SSE frames for a run: tails the live run if in memory, otherwise replays from SQLite."""
        state = self.active.get(run_id)
        if state is None:
            if self.db.get_run(run_id, user_id) is None:
                return
            for ev in self.db.get_events(run_id, after):
                yield sse(ev)
            return
        idx = after + 1
        while True:
            while idx < len(state.events):
                yield sse(state.events[idx])
                idx += 1
            if state.done:
                return
            try:
                async with state.cond:
                    # re-check under the lock so a notify between the loop above and here is not missed
                    if idx >= len(state.events) and not state.done:
                        await asyncio.wait_for(state.cond.wait(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # keep proxies (Cloudflare, Next) from closing an idle stream
