from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import FakeClient, make_message, parse_sse, text, tool_use


def make_client(settings, script):
    fake = FakeClient(script)
    return TestClient(create_app(settings, client=fake)), fake


def test_health_is_public(settings):
    settings.app_token = "secret"
    with make_client(settings, [])[0] as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_auth_required_when_token_set(settings):
    settings.app_token = "secret"
    with make_client(settings, [])[0] as c:
        assert c.get("/api/conversations").status_code == 401
        assert c.get("/api/conversations", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/conversations", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_chat_stream_end_to_end(settings):
    script = [
        make_message([text("Checking."), tool_use("t1", "get_current_time")], "tool_use"),
        make_message([text("It is a day.")]),
    ]
    c, _ = make_client(settings, script)
    with c:
        conv = c.post("/api/conversations").json()
        r = c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "what time is it?"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        types = [e["type"] for e in events]
        assert types[0] == "run_started" and types[-1] == "run_finished"
        assert "tool_call" in types and "tool_result" in types and "text_delta" in types
        assert events[-1]["data"]["status"] == "completed"
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

        # history view: runs with their persisted (non-delta) events, auto-titled conversation
        detail = c.get(f"/api/conversations/{conv['id']}").json()
        assert detail["title"] == "what time is it?"
        run = detail["runs"][0]
        assert run["status"] == "completed" and run["cost_usd"] > 0 and run["steps"] == 2
        assert "text_delta" not in [e["type"] for e in run["events"]]
        assert any(e["type"] == "text" and e["data"]["text"] == "It is a day." for e in run["events"])

        # activity list and replay-after-finish (served from SQLite)
        assert c.get("/api/runs").json()[0]["id"] == run["id"]
        replay = parse_sse(c.get(f"/api/runs/{run['id']}/events").text)
        assert replay[-1]["type"] == "run_finished"
        tail = parse_sse(c.get(f"/api/runs/{run['id']}/events?after={replay[-2]['seq']}").text)
        assert [e["type"] for e in tail] == ["run_finished"]


def test_monthly_budget_blocks_new_runs(settings):
    settings.monthly_budget_usd = 0.00001
    c, fake = make_client(settings, [make_message([text("a")]), make_message([text("b")])])
    with c:
        conv = c.post("/api/conversations").json()
        assert c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "one"}).status_code == 200
        blocked = c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "two"})
        assert blocked.status_code == 402 and "budget" in blocked.json()["detail"].lower()
        assert len(fake.calls) == 1  # no paid call after the cap


def test_missing_api_key_is_a_clean_error_event(settings):
    import anthropic

    class BrokenStream:
        async def __aenter__(self):
            import httpx2 as httpx

            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError(
                "bad key", response=httpx.Response(401, request=req), body=None
            )

        async def __aexit__(self, *a):
            return False

    fake = FakeClient([])
    fake.messages.stream = lambda **kw: BrokenStream()
    with TestClient(create_app(settings, client=fake)) as c:
        conv = c.post("/api/conversations").json()
        events = parse_sse(c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"}).text)
        err = next(e for e in events if e["type"] == "error")
        assert "ANTHROPIC_API_KEY" in err["data"]["message"]
        assert events[-1]["data"]["status"] == "error"


def test_unknown_conversation_404_and_isolation(settings):
    with make_client(settings, [])[0] as c:
        assert c.get("/api/conversations/nope").status_code == 404
        assert c.post("/api/conversations/nope/messages", json={"text": "x"}).status_code == 404
        assert c.get("/api/runs/nope").status_code == 404


def test_config_endpoint_reports_state(settings):
    with make_client(settings, [])[0] as c:
        cfg = c.get("/api/config").json()
        assert cfg["model"] == "claude-sonnet-5-5" and cfg["api_key_configured"] is True
        assert cfg["budget"]["monthly_usd"] == 10.0
        names = {t["name"] for t in c.get("/api/tools").json()}
        assert {"get_current_time", "write_note"} <= names
