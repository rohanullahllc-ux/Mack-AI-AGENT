"""The agent loop: call Claude, run tools, feed results back, repeat until done or limits hit.

Manual loop (not the SDK tool runner) because we need to: emit every step to the UI as it happens,
enforce per-run cost/step limits, and pause for human approval before risky tools.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import anthropic
from pydantic import ValidationError

from app.agent.pricing import estimate_cost
from app.agent.prompts import SYSTEM_PROMPT
from app.config import Settings
from app.db import Database
from app.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

# emit(type, data, persist): persist=False for high-frequency deltas that only matter live
Emit = Callable[[str, dict[str, Any], bool], Awaitable[None]]


@dataclass
class ApprovalDecision:
    approved: bool
    reason: str = ""  # why it was denied; shown to the model so it can explain or choose another way
    timed_out: bool = False  # a denial because nobody answered in time, not because the user said no

    @property
    def outcome(self) -> str:
        return "approved" if self.approved else "timed_out" if self.timed_out else "denied"


# approve(tool_use_id, tool_name, tool_input, tier) -> the user's decision
Approver = Callable[[str, str, dict[str, Any], str], Awaitable[ApprovalDecision]]

_TIER_RANK = {"read": 0, "write": 1, "destructive": 2}
_APPROVAL_THRESHOLD = {"write": 1, "destructive": 2, "never": 99}

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_JSON_RETRIES = 2


@dataclass
class RunStats:
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    status: str = "running"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


def block_to_param(block: Any) -> dict[str, Any]:
    """Serialise a response block exactly the way the SDK does when you pass it back as a param
    (thinking signatures, server-tool blocks etc. must round-trip unchanged)."""
    return block.model_dump(
        exclude_unset=True, mode="json", exclude=getattr(block, "__api_exclude__", None)
    )


class AgentRunner:
    def __init__(self, settings: Settings, db: Database, registry: ToolRegistry, client: Any = None):
        self.settings = settings
        self.db = db
        self.registry = registry
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.settings.anthropic_api_key or None)
        return self._client

    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        conversation_id: str,
        user_id: str,
        run_id: str,
        user_text: str,
        emit: Emit,
        approve: Approver | None = None,
        stats: RunStats | None = None,
    ) -> RunStats:
        """`stats` may be supplied by the caller so that what was already spent survives an exception
        or cancellation mid-run. `approve` answers approval requests; without one, any tool that needs
        approval is denied (fail closed)."""
        s = self.settings
        stats = stats if stats is not None else RunStats()
        messages = self.db.get_messages(conversation_id, user_id)
        messages.append({"role": "user", "content": user_text})
        self.db.add_messages(conversation_id, user_id, [("user", user_text)], run_id)

        tools = self.registry.api_tools(s)
        ctx = ToolContext(settings=s, user_id=user_id, run_id=run_id)

        for _ in range(s.max_steps):
            if stats.cost_usd >= s.max_run_cost_usd:
                stats.status = "limit"
                stats.error = f"Run stopped: reached the ${s.max_run_cost_usd:.2f} per-run cost limit."
                await emit("error", {"message": stats.error}, True)
                return stats

            final = await self._call_model(messages, tools, emit)
            stats.steps += 1
            step_cost = self._account(final, stats)
            await emit(
                "usage",
                {
                    "step": stats.steps,
                    "model": final.model,
                    "input_tokens": final.usage.input_tokens,
                    "output_tokens": final.usage.output_tokens,
                    "cost_usd": round(step_cost, 6),
                    "run_cost_usd": round(stats.cost_usd, 6),
                },
                True,
            )
            await self._emit_blocks(final, emit)

            reason = final.stop_reason
            assistant_param = [block_to_param(b) for b in final.content]

            if reason == "refusal":
                details = getattr(final, "stop_details", None)
                category = getattr(details, "category", None)
                stats.status = "refused"
                stats.error = "The request was declined by a safety classifier" + (
                    f" ({category})." if category else "."
                )
                await emit("error", {"message": stats.error, "category": category}, True)
                return stats

            tool_uses = [b for b in final.content if b.type == "tool_use"]

            if reason == "max_tokens" and tool_uses:
                # a truncated tool input parses as a valid partial object; never run it
                stats.status = "error"
                stats.error = "Response was cut off mid tool call (max_tokens). Try a shorter request."
                await emit("error", {"message": stats.error}, True)
                return stats

            if reason == "pause_turn":
                self.db.add_messages(conversation_id, user_id, [("assistant", assistant_param)], run_id)
                messages.append({"role": "assistant", "content": assistant_param})
                continue

            if not tool_uses:  # end_turn, stop_sequence, or a truncated text answer
                self.db.add_messages(conversation_id, user_id, [("assistant", assistant_param)], run_id)
                stats.status = "completed"
                if reason == "max_tokens":
                    await emit("notice", {"message": "The answer was cut off (max_tokens)."}, True)
                return stats

            results = await asyncio.gather(*(self._run_tool(b, ctx, emit, approve) for b in tool_uses))
            # commit assistant+results together: history must never end on a dangling tool_use
            self.db.add_messages(
                conversation_id, user_id, [("assistant", assistant_param), ("user", list(results))], run_id
            )
            messages.append({"role": "assistant", "content": assistant_param})
            messages.append({"role": "user", "content": list(results)})

        stats.status = "limit"
        stats.error = f"Run stopped after {s.max_steps} steps without finishing."
        await emit("error", {"message": stats.error}, True)
        return stats

    # ------------------------------------------------------------------
    def _open_stream(self, kwargs: dict[str, Any]) -> Any:
        if self.settings.refusal_fallback:
            return self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.stream(**kwargs)

    async def _call_model(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], emit: Emit) -> Any:
        s = self.settings
        kwargs: dict[str, Any] = dict(
            model=s.model_main,
            max_tokens=s.max_tokens,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": s.effort},
            cache_control={"type": "ephemeral"},
        )
        attempts = 0
        while True:
            try:
                async with self._open_stream(kwargs) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            d = event.delta
                            if d.type == "text_delta":
                                await emit("text_delta", {"text": d.text}, False)
                            elif d.type == "thinking_delta":
                                await emit("thinking_delta", {"text": d.thinking}, False)
                    return await stream.get_final_message()
            except ValueError:
                # eager tool-input streaming produced JSON the SDK could not parse at all;
                # re-issue the turn (bounded). API errors are not ValueError and propagate.
                attempts += 1
                if attempts > MAX_JSON_RETRIES:
                    raise
                await emit("reset", {}, False)

    def _account(self, final: Any, stats: RunStats) -> float:
        u = final.usage
        cost = estimate_cost(final.model, u)
        stats.cost_usd += cost
        stats.input_tokens += u.input_tokens or 0
        stats.output_tokens += u.output_tokens or 0
        stats.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        stats.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        return cost

    async def _emit_blocks(self, final: Any, emit: Emit) -> None:
        for b in final.content:
            t = b.type
            if t == "thinking":
                if getattr(b, "thinking", ""):
                    await emit("thinking", {"text": b.thinking}, True)
            elif t == "text":
                if b.text:
                    await emit("text", {"text": b.text}, True)
            elif t == "tool_use":
                tool = self.registry.get(b.name)
                await emit(
                    "tool_call",
                    {"id": b.id, "name": b.name, "input": b.input, "source": "client",
                     "tier": tool.tier if tool else None},
                    True,
                )
            elif t == "server_tool_use":
                await emit(
                    "tool_call",
                    {"id": b.id, "name": b.name, "input": b.input, "source": "server", "tier": "read"},
                    True,
                )
            elif t == "web_search_tool_result":
                content = b.content
                if isinstance(content, list):
                    output = "\n".join(f"- {r.title} — {r.url}" for r in content) or "No results."
                    is_error = False
                else:
                    output = f"web_search error: {getattr(content, 'error_code', 'unknown')}"
                    is_error = True
                await emit(
                    "tool_result",
                    {"id": b.tool_use_id, "name": "web_search", "output": output,
                     "is_error": is_error, "source": "server"},
                    True,
                )
            elif t == "fallback":
                await emit(
                    "notice",
                    {"message": f"Request was re-run on {b.to.model} after {b.from_.model} declined it."},
                    True,
                )

    @staticmethod
    def _is_well_formed(tool: Tool, tool_input: Any) -> bool:
        try:
            tool.input_model.model_validate(tool_input)
        except ValidationError:
            return False
        return True

    def _needs_approval(self, tool: Tool, tool_input: Any) -> bool:
        if _TIER_RANK[tool.tier] < _APPROVAL_THRESHOLD[self.settings.approval_level]:
            return False
        # a malformed call is not put to the user: execute() reports the error to the model
        return self._is_well_formed(tool, tool_input)

    async def _run_tool(
        self, block: Any, ctx: ToolContext, emit: Emit, approve: Approver | None
    ) -> dict[str, Any]:
        tool = self.registry.get(block.name)
        needs_approval = tool is not None and self._needs_approval(tool, block.input)
        # every state-changing call that will really be considered is logged, whether or not we had to ask;
        # reads and malformed calls change nothing, so they are not
        audit_id: int | None = None
        if tool is not None and tool.tier != "read" and self._is_well_formed(tool, block.input):
            audit_id = self.db.audit_open(
                user_id=ctx.user_id,
                run_id=ctx.run_id,
                tool_use_id=block.id,
                tool=tool.name,
                tier=tool.tier,
                tool_input=block.input,
                outcome="pending" if needs_approval else "auto",
            )

        denied = False
        if needs_approval:  # implies audit_id is set: only well-formed write/destructive calls get here
            if approve is None:
                decision = ApprovalDecision(False, "no approval channel is available")
            else:
                try:
                    decision = await approve(block.id, tool.name, block.input, tool.tier)
                except asyncio.CancelledError:
                    self.db.audit_decide(audit_id, "cancelled", "the run was cancelled while waiting")
                    raise
            self.db.audit_decide(audit_id, decision.outcome, decision.reason)
            denied = not decision.approved
        started = time.monotonic()
        if denied:
            result = ToolResult(
                f"Not run ({decision.reason or 'the user denied this action'}). "
                "Do not retry it unless the user asks; tell them what was skipped.",
                True,
            )
        else:
            try:
                result = await self.registry.execute(block.name, block.input, ctx)
            except asyncio.CancelledError:
                # a sync tool keeps running in its worker thread after the await is cancelled, so whether it
                # took effect is unknown
                if audit_id is not None:
                    self.db.audit_result(audit_id, "interrupted")
                raise
            if audit_id is not None:
                self.db.audit_result(audit_id, "error" if result.is_error else "ok")
        await emit(
            "tool_result",
            {
                "id": block.id,
                "name": block.name,
                "output": result.output[:4000],
                "is_error": result.is_error,
                "denied": denied,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "source": "client",
            },
            True,
        )
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": result.output}
        if result.is_error:
            out["is_error"] = True
        return out
