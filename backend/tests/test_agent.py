from __future__ import annotations

from app.agent.loop import AgentRunner, ApprovalDecision, RunStats
from app.agent.pricing import estimate_cost
from app.config import SINGLE_USER_ID
from app.db import Database
from app.tools.registry import REGISTRY

from .conftest import FakeClient, make_message, text, tool_use


class Collector:
    def __init__(self):
        self.events: list[tuple[str, dict, bool]] = []

    async def __call__(self, type_, data, persist=True):
        self.events.append((type_, data, persist))

    def types(self, persisted_only=True):
        return [t for t, _, p in self.events if p or not persisted_only]


class Approver:
    """Stands in for the user: answers every approval request with a fixed decision and records the asks."""

    def __init__(self, approved: bool = True, reason: str = ""):
        self.decision = ApprovalDecision(approved, reason)
        self.asked: list[tuple[str, str, dict, str]] = []

    async def __call__(self, tool_use_id, name, tool_input, tier):
        self.asked.append((tool_use_id, name, tool_input, tier))
        return self.decision


_DEFAULT = object()


async def run_once(settings, script, text_in="hi", approve=_DEFAULT):
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    client = FakeClient(script)
    runner = AgentRunner(settings, db, REGISTRY, client)
    emit = Collector()
    stats = await runner.run(
        conversation_id=conv["id"],
        user_id=SINGLE_USER_ID,
        run_id="run1",
        user_text=text_in,
        emit=emit,
        approve=Approver() if approve is _DEFAULT else approve,
    )
    return db, conv, client, emit, stats


async def test_simple_answer(settings):
    db, conv, client, emit, stats = await run_once(settings, [make_message([text("Hello!")])])
    assert stats.status == "completed" and stats.steps == 1
    assert emit.types() == ["usage", "text"]
    assert ("text_delta", {"text": "Hello!"}, False) in emit.events  # live-only delta
    msgs = db.get_messages(conv["id"], SINGLE_USER_ID)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == [{"type": "text", "text": "Hello!"}]
    assert stats.cost_usd > 0


async def test_request_shape(settings):
    _, _, client, _, _ = await run_once(settings, [make_message([text("ok")])])
    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"] == {"effort": "medium"}
    assert call["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in call and "tool_choice" not in call  # both rejected on this model
    assert "_beta" not in call


async def test_refusal_fallback_uses_beta_endpoint(settings):
    settings.refusal_fallback = True
    _, _, client, _, _ = await run_once(settings, [make_message([text("ok")])])
    assert client.calls[0]["_beta"] is True
    assert client.calls[0]["fallbacks"] == "default"
    assert client.calls[0]["betas"] == ["server-side-fallback-2026-07-01"]


async def test_tool_loop(settings):
    script = [
        make_message([text("Saving."), tool_use("t1", "write_note", title="Todo", content="milk")], "tool_use"),
        make_message([text("Done, saved 'todo'.")]),
    ]
    db, conv, client, emit, stats = await run_once(settings, script, "remember milk")
    assert stats.status == "completed" and stats.steps == 2
    types = emit.types()
    assert types.index("tool_call") < types.index("tool_result") < len(types) - 1
    result = next(d for t, d, _ in emit.events if t == "tool_result")
    assert result["name"] == "write_note" and not result["is_error"]

    msgs = db.get_messages(conv["id"], SINGLE_USER_ID)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    # tool_result must answer the exact tool_use id, and the 2nd request must carry both
    assert msgs[2]["content"][0]["tool_use_id"] == "t1"
    assert len(client.calls[1]["messages"]) == 3


async def test_parallel_tool_calls_return_one_user_message(settings):
    script = [
        make_message(
            [tool_use("a", "get_current_time"), tool_use("b", "list_notes")], "tool_use"
        ),
        make_message([text("done")]),
    ]
    db, conv, client, _, stats = await run_once(settings, script)
    assert stats.status == "completed"
    msgs = db.get_messages(conv["id"], SINGLE_USER_ID)
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["a", "b"]


async def test_tool_error_is_returned_to_model(settings):
    script = [
        make_message([tool_use("t1", "read_note", title="ghost")], "tool_use"),
        make_message([text("No such note.")]),
    ]
    db, conv, _, _, stats = await run_once(settings, script)
    assert stats.status == "completed"
    block = db.get_messages(conv["id"], SINGLE_USER_ID)[2]["content"][0]
    assert block["is_error"] is True


async def test_history_carried_into_next_turn(settings):
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    client = FakeClient([make_message([text("one")]), make_message([text("two")])])
    runner = AgentRunner(settings, db, REGISTRY, client)
    for i, t in enumerate(["first", "second"]):
        await runner.run(
            conversation_id=conv["id"], user_id=SINGLE_USER_ID, run_id=f"r{i}", user_text=t, emit=Collector()
        )
    sent = client.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [{"type": "text", "text": "one"}]


async def test_step_limit(settings):
    settings.max_steps = 2
    loop_forever = [make_message([tool_use(f"t{i}", "get_current_time")], "tool_use") for i in range(5)]
    _, _, _, emit, stats = await run_once(settings, loop_forever)
    assert stats.status == "limit" and stats.steps == 2
    assert "error" in emit.types()


async def test_cost_limit(settings):
    settings.max_run_cost_usd = 0.0001
    script = [make_message([tool_use("t1", "get_current_time")], "tool_use"), make_message([text("x")])]
    _, _, client, _, stats = await run_once(settings, script)
    assert stats.status == "limit" and stats.steps == 1
    assert len(client.calls) == 1  # stopped before a second paid call


async def test_refusal_is_reported_and_not_stored(settings):
    class Details:
        category = "cyber"

    msg = make_message([text("")], "refusal")
    object.__setattr__(msg, "stop_details", Details())
    db, conv, _, emit, stats = await run_once(settings, [msg])
    assert stats.status == "refused" and "cyber" in stats.error
    assert [m["role"] for m in db.get_messages(conv["id"], SINGLE_USER_ID)] == ["user"]


async def test_truncated_tool_call_is_not_executed(settings):
    msg = make_message([tool_use("t1", "write_note", title="x", content="partial")], "max_tokens")
    db, conv, _, emit, stats = await run_once(settings, [msg])
    assert stats.status == "error" and "cut off" in stats.error
    assert "tool_result" not in emit.types()


def test_cost_estimate():
    u = make_message([text("x")]).usage  # 1000 in / 200 out
    assert round(estimate_cost("claude-sonnet-5-5", u), 6) == round((1000 * 2 + 200 * 10) / 1e6, 6)
    assert estimate_cost("unknown-model", u) >= estimate_cost("claude-sonnet-5-5", u)


# ---- approvals --------------------------------------------------------------


async def test_write_tool_waits_for_approval_and_runs_when_approved(settings, tmp_path):
    approver = Approver(True)
    script = [
        make_message([tool_use("t1", "write_note", title="Todo", content="milk")], "tool_use"),
        make_message([text("saved")]),
    ]
    _, _, _, emit, stats = await run_once(settings, script, approve=approver)
    assert stats.status == "completed"
    assert approver.asked == [("t1", "write_note", {"title": "Todo", "content": "milk"}, "write")]
    result = next(d for t, d, _ in emit.events if t == "tool_result")
    assert result["denied"] is False and not result["is_error"]
    assert (settings.sandbox_dir / "notes" / SINGLE_USER_ID / "todo.md").read_text() == "milk"


async def test_denied_tool_is_not_run_and_model_is_told(settings):
    approver = Approver(False, "the user denied this action")
    script = [
        make_message([tool_use("t1", "write_note", title="Todo", content="milk")], "tool_use"),
        make_message([text("ok, skipped")]),
    ]
    db, conv, client, emit, stats = await run_once(settings, script, approve=approver)
    assert stats.status == "completed"
    assert not list(settings.data_dir.rglob("*.md"))  # nothing was written
    result = next(d for t, d, _ in emit.events if t == "tool_result")
    assert result["denied"] is True and result["is_error"] is True
    block = db.get_messages(conv["id"], SINGLE_USER_ID)[2]["content"][0]
    assert block["is_error"] is True and "Not run" in block["content"] and "Do not retry" in block["content"]
    assert client.calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "t1"  # model saw the denial


async def test_no_approver_fails_closed(settings):
    script = [
        make_message([tool_use("t1", "write_note", title="Todo", content="milk")], "tool_use"),
        make_message([text("could not save")]),
    ]
    _, _, _, emit, stats = await run_once(settings, script, approve=None)
    assert stats.status == "completed"
    assert not list(settings.data_dir.rglob("*.md"))
    assert next(d for t, d, _ in emit.events if t == "tool_result")["denied"] is True


async def test_read_tools_never_ask(settings):
    approver = Approver(False)
    script = [make_message([tool_use("t1", "get_current_time")], "tool_use"), make_message([text("done")])]
    _, _, _, emit, _ = await run_once(settings, script, approve=approver)
    assert approver.asked == []
    assert next(d for t, d, _ in emit.events if t == "tool_result")["denied"] is False


async def test_malformed_call_is_not_put_to_the_user(settings):
    approver = Approver(True)
    script = [make_message([tool_use("t1", "write_note", title="x")], "tool_use"), make_message([text("oops")])]
    _, _, _, emit, _ = await run_once(settings, script, approve=approver)  # `content` is missing
    assert approver.asked == []
    result = next(d for t, d, _ in emit.events if t == "tool_result")
    assert result["is_error"] and not result["denied"] and "content" in result["output"]


async def test_approval_level_controls_which_tiers_ask(settings):
    script = lambda: [  # noqa: E731
        make_message([tool_use("t1", "write_note", title="a", content="b")], "tool_use"),
        make_message([text("done")]),
    ]
    settings.approval_level = "never"
    approver = Approver(False)
    await run_once(settings, script(), approve=approver)
    assert approver.asked == []
    assert list(settings.data_dir.rglob("*.md"))  # ran without asking

    settings.approval_level = "destructive"  # write_note is only a "write" tool
    approver = Approver(False)
    await run_once(settings, script(), approve=approver)
    assert approver.asked == []


async def test_parallel_writes_each_get_their_own_approval(settings):
    approver = Approver(True)
    script = [
        make_message(
            [tool_use("a", "write_note", title="one", content="1"), tool_use("b", "write_note", title="two", content="2")],
            "tool_use",
        ),
        make_message([text("done")]),
    ]
    db, conv, _, _, stats = await run_once(settings, script, approve=approver)
    assert stats.status == "completed"
    assert sorted(a[0] for a in approver.asked) == ["a", "b"]
    assert [b["tool_use_id"] for b in db.get_messages(conv["id"], SINGLE_USER_ID)[2]["content"]] == ["a", "b"]


# ---- cost survives failure --------------------------------------------------


async def test_caller_owned_stats_keep_cost_when_a_later_step_raises(settings):
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    client = FakeClient([make_message([tool_use("t1", "get_current_time")], "tool_use")])  # 2nd call has no script
    runner = AgentRunner(settings, db, REGISTRY, client)
    stats = RunStats()
    try:
        await runner.run(
            conversation_id=conv["id"], user_id=SINGLE_USER_ID, run_id="r", user_text="hi",
            emit=Collector(), stats=stats,
        )
    except IndexError:  # FakeClient ran out of script on the 2nd model call
        pass
    assert stats.steps == 1 and stats.cost_usd > 0  # the first call was paid for and is still on the books
