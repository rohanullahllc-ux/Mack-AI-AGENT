"""RunManager + HTTP: the approval round-trip, and accounting when a run dies part-way."""
from __future__ import annotations

import asyncio
import threading
import time

import anthropic
from fastapi.testclient import TestClient

from app.agent.loop import AgentRunner
from app.config import SINGLE_USER_ID
from app.db import Database
from app.main import create_app
from app.runs import RunManager
from app.tools.registry import REGISTRY

from .conftest import FakeClient, make_message, text, tool_use

WRITE = [
    make_message([tool_use("t1", "write_note", title="Todo", content="milk")], "tool_use"),
    make_message([text("done")]),
]


def manager(settings, script):
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    fake = FakeClient(script)
    return RunManager(settings, db, AgentRunner(settings, db, REGISTRY, fake)), db, conv, fake


async def wait_for_event(state, type_, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in state.events:
            if ev["type"] == type_:
                return ev
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {type_} event; saw {[e['type'] for e in state.events]}")


async def test_run_pauses_until_approved(settings):
    mgr, db, conv, _ = manager(settings, WRITE)
    state = mgr.start(conversation_id=conv["id"], user_id=SINGLE_USER_ID, text="save it")
    req = await wait_for_event(state, "approval_request")
    assert req["data"]["id"] == "t1" and req["data"]["name"] == "write_note" and req["data"]["tier"] == "write"
    assert req["data"]["input"] == {"title": "Todo", "content": "milk"}
    await asyncio.sleep(0.05)
    assert not state.done and not list(settings.data_dir.rglob("*.md"))  # still waiting, nothing written

    assert mgr.decide(state.run_id, "t1", True)
    await asyncio.wait_for(state.task, 5)
    types = [e["type"] for e in state.events]
    assert types.index("approval_request") < types.index("approval_decision") < types.index("tool_result")
    assert next(e for e in state.events if e["type"] == "approval_decision")["data"]["approved"] is True
    assert db.get_run(state.run_id, SINGLE_USER_ID)["status"] == "completed"
    assert list(settings.data_dir.rglob("todo.md"))
    # approval events are durable, so a replay of this run shows who allowed what
    assert {"approval_request", "approval_decision"} <= {e["type"] for e in db.get_events(state.run_id)}


async def test_denied_action_is_not_performed(settings):
    mgr, db, conv, _ = manager(settings, WRITE)
    state = mgr.start(conversation_id=conv["id"], user_id=SINGLE_USER_ID, text="save it")
    await wait_for_event(state, "approval_request")
    assert mgr.decide(state.run_id, "t1", False)
    await asyncio.wait_for(state.task, 5)
    assert not list(settings.data_dir.rglob("*.md"))
    result = next(e for e in state.events if e["type"] == "tool_result")["data"]
    assert result["denied"] is True
    assert db.get_run(state.run_id, SINGLE_USER_ID)["status"] == "completed"


async def test_unanswered_approval_times_out_as_denied(settings):
    settings.approval_timeout_s = 0.05
    mgr, db, conv, _ = manager(settings, WRITE)
    state = mgr.start(conversation_id=conv["id"], user_id=SINGLE_USER_ID, text="save it")
    await asyncio.wait_for(state.task, 5)
    decision = next(e for e in state.events if e["type"] == "approval_decision")["data"]
    assert decision["approved"] is False and "no response" in decision["reason"]
    assert not list(settings.data_dir.rglob("*.md"))
    assert not state.approvals  # nothing left dangling


async def test_decide_rejects_unknown_and_repeat_decisions(settings):
    mgr, _, conv, _ = manager(settings, WRITE)
    state = mgr.start(conversation_id=conv["id"], user_id=SINGLE_USER_ID, text="save it")
    await wait_for_event(state, "approval_request")
    assert not mgr.decide(state.run_id, "nope", True)
    assert not mgr.decide("nope", "t1", True)
    assert mgr.decide(state.run_id, "t1", True)
    assert not mgr.decide(state.run_id, "t1", False)  # first answer wins
    await asyncio.wait_for(state.task, 5)


async def test_cancelling_while_waiting_closes_the_run_cleanly(settings):
    mgr, db, conv, _ = manager(settings, WRITE)
    state = mgr.start(conversation_id=conv["id"], user_id=SINGLE_USER_ID, text="save it")
    await wait_for_event(state, "approval_request")
    assert mgr.cancel(state.run_id)
    await asyncio.wait_for(state.task, 5)
    run = db.get_run(state.run_id, SINGLE_USER_ID)
    assert run["status"] == "cancelled" and not state.approvals
    assert run["cost_usd"] > 0  # the model call before the pause was paid for and is recorded
    assert db.month_spend(SINGLE_USER_ID) == run["cost_usd"]


async def test_failed_run_still_records_what_it_spent(settings):
    class Dropped:
        async def __aenter__(self):
            import httpx2 as httpx

            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

        async def __aexit__(self, *a):
            return False

    script = [make_message([tool_use("t1", "get_current_time")], "tool_use")]
    mgr, db, conv, fake = manager(settings, script)
    real = fake.messages.stream
    calls = []

    def flaky(**kw):  # first call works, the second drops the connection
        calls.append(1)
        return real(**kw) if len(calls) == 1 else Dropped()

    fake.messages.stream = flaky
    state = mgr.start(conversation_id=conv["id"], user_id=SINGLE_USER_ID, text="what time is it")
    await asyncio.wait_for(state.task, 5)
    run = db.get_run(state.run_id, SINGLE_USER_ID)
    assert run["status"] == "error" and "network" in run["error"]
    assert run["cost_usd"] > 0 and run["steps"] == 1
    assert db.month_spend(SINGLE_USER_ID) == run["cost_usd"]


# ---- over HTTP ------------------------------------------------------------------


def test_approval_endpoint_validation(settings):
    settings.app_token = "secret"
    auth = {"Authorization": "Bearer secret"}
    with TestClient(create_app(settings, client=FakeClient([make_message([text("hi")])]))) as c:
        conv = c.post("/api/conversations", headers=auth).json()
        run_id = parse_run_id(c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "x"}, headers=auth).text)
        url = f"/api/runs/{run_id}/approvals/t1"
        assert c.post(url, json={"approved": True}).status_code == 401  # needs the token
        assert c.post("/api/runs/nope/approvals/t1", json={"approved": True}, headers=auth).status_code == 404
        assert c.post(url, json={"approved": True}, headers=auth).status_code == 409  # nothing pending
        assert c.post(url, json={}, headers=auth).status_code == 422


def parse_run_id(body: str) -> str:
    from .conftest import parse_sse

    return parse_sse(body)[0]["data"]["run_id"]


def test_approval_round_trip_over_http(settings):
    with TestClient(create_app(settings, client=FakeClient(WRITE))) as c:
        conv = c.post("/api/conversations").json()
        result: dict = {}

        def chat():  # the SSE response only ends when the run does, so drive it from a thread
            result["resp"] = c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "save it"})

        t = threading.Thread(target=chat)
        t.start()
        try:
            run_id = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not run_id:
                run_id = c.get(f"/api/conversations/{conv['id']}").json()["active_run_id"]
                time.sleep(0.02)
            assert run_id, "run never became active"

            url = f"/api/runs/{run_id}/approvals/t1"
            decided = None
            while time.monotonic() < deadline and decided is None:
                r = c.post(url, json={"approved": True})
                decided = r if r.status_code == 200 else None  # 409 until the request is registered
                time.sleep(0.02)
            assert decided is not None and decided.json() == {"decided": True}
        finally:
            t.join(10)
        assert not t.is_alive()
        from .conftest import parse_sse

        events = parse_sse(result["resp"].text)
        assert [e["type"] for e in events if e["type"].startswith("approval")] == [
            "approval_request",
            "approval_decision",
        ]
        assert events[-1]["data"]["status"] == "completed"
        assert list(settings.data_dir.rglob("todo.md"))


def test_config_reports_approval_level(settings):
    with TestClient(create_app(settings, client=FakeClient([]))) as c:
        assert c.get("/api/config").json()["approval_level"] == "write"
