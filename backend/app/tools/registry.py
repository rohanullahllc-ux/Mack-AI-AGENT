"""Tool registry.

A tool is a pydantic input model + a function. Drop a new module in `app/tools/` that uses
`@tool(...)` and it is auto-registered; no core changes needed. MCP-backed tools plug into the
same registry later by registering `Tool` objects with a different `fn`.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from app.config import Settings

Tier = Literal["read", "write", "destructive"]
MAX_RESULT_CHARS = 20_000


@dataclass
class ToolContext:
    settings: Settings
    user_id: str
    run_id: str


@dataclass
class ToolResult:
    output: str
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[..., Any]
    tier: Tier = "read"

    def api_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _clean_schema(self.input_model.model_json_schema()),
            # stream large inputs as they are generated; we validate before running (see execute)
            "eager_input_streaming": True,
        }


def _clean_schema(node: Any) -> Any:
    """Drop pydantic's auto-generated 'title' strings (keeps properties literally named 'title')."""
    if isinstance(node, dict):
        return {
            k: _clean_schema(v)
            for k, v in node.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, t: Tool) -> Tool:
        if t.name in self._tools:
            raise ValueError(f"duplicate tool name: {t.name}")
        self._tools[t.name] = t
        return t

    def get(self, name: str) -> Tool | None:
        if name in self._tools:
            return self._tools[name]
        lowered = [t for n, t in self._tools.items() if n.lower() == name.lower()]
        return lowered[0] if len(lowered) == 1 else None  # tolerate case-only mismatches

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def api_tools(self, settings: Settings) -> list[dict[str, Any]]:
        """Deterministic order (sorted, server tools last) so the prompt-cache prefix stays stable."""
        defs: list[dict[str, Any]] = [t.api_definition() for t in self.all()]
        if settings.web_search_enabled:
            defs.append(
                {
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "max_uses": settings.web_search_max_uses,
                }
            )
        return defs

    async def execute(self, name: str, raw_input: Any, ctx: ToolContext) -> ToolResult:
        t = self.get(name)
        if t is None:
            known = ", ".join(x.name for x in self.all())
            return ToolResult(f"Unknown tool '{name}'. Available tools: {known}", True)
        try:
            args = t.input_model.model_validate(raw_input)
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            return ToolResult(f"Invalid input for {t.name}: {errs}", True)
        try:
            if inspect.iscoroutinefunction(t.fn):
                coro = t.fn(args, ctx)
            else:
                coro = asyncio.to_thread(t.fn, args, ctx)
            result = await asyncio.wait_for(coro, timeout=ctx.settings.tool_timeout_s)
        except asyncio.TimeoutError:
            return ToolResult(f"{t.name} timed out after {ctx.settings.tool_timeout_s:.0f}s", True)
        except ToolError as e:
            return ToolResult(str(e), True)
        except Exception as e:  # tool bugs must not kill the run; the model sees and can adapt
            return ToolResult(f"{t.name} failed: {type(e).__name__}: {e}", True)
        text = result if isinstance(result, str) else str(result)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n...[truncated, {len(text) - MAX_RESULT_CHARS} more chars]"
        return ToolResult(text)


class ToolError(Exception):
    """Raise from a tool for an expected failure; the message is shown to the model verbatim."""


REGISTRY = ToolRegistry()


def tool(
    *, name: str, description: str, input_model: type[BaseModel], tier: Tier = "read"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY.register(Tool(name, description, input_model, fn, tier))
        return fn

    return decorator


def load_builtin_tools() -> ToolRegistry:
    """Import every module in app.tools so its @tool decorators run (idempotent)."""
    import app.tools as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name != "registry":
            importlib.import_module(f"app.tools.{mod.name}")
    return REGISTRY
