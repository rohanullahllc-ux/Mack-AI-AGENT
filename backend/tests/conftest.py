from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from app.config import Settings
from app.tools.registry import load_builtin_tools

load_builtin_tools()


def make_message(content: list[Any], stop_reason: str = "end_turn", model: str = "claude-sonnet-5-5") -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=1000, output_tokens=200),
    )


def text(t: str) -> TextBlock:
    return TextBlock(type="text", text=t)


def tool_use(id_: str, name: str, **inp: Any) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=id_, name=name, input=inp)


class FakeStream:
    def __init__(self, message: Message):
        self.message = message

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for b in self.message.content:
            if b.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text)
                )

    async def get_final_message(self) -> Message:
        return self.message


class FakeClient:
    """Stands in for AsyncAnthropic: replays a scripted list of Messages, records every request."""

    def __init__(self, script: list[Message]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(stream=self._stream)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._beta_stream))

    def _stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(json.loads(json.dumps(kwargs, default=lambda o: o.model_dump(mode="json"))))
        return FakeStream(self.script.pop(0))

    def _beta_stream(self, **kwargs: Any) -> FakeStream:
        kwargs["_beta"] = True
        return self._stream(**kwargs)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        anthropic_api_key="test-key",
        data_dir=tmp_path,
        refusal_fallback=False,
        web_search_enabled=False,
        monthly_budget_usd=10.0,
    )


def parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for frame in body.split("\n\n"):
        lines = [l for l in frame.splitlines() if not l.startswith(":")]
        if not lines:
            continue
        ev: dict[str, Any] = {}
        for line in lines:
            key, _, val = line.partition(": ")
            ev[key] = val
        events.append({"seq": int(ev["id"]), "type": ev["event"], "data": json.loads(ev["data"])})
    return events
