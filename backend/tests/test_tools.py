from __future__ import annotations

import pytest

from app.tools.registry import REGISTRY, ToolContext


@pytest.fixture
def ctx(settings):
    return ToolContext(settings=settings, user_id="local", run_id="r1")


def test_api_tools_deterministic_and_eager(settings):
    defs = REGISTRY.api_tools(settings)
    names = [d["name"] for d in defs]
    assert names == sorted(names)  # stable order keeps the prompt-cache prefix stable
    assert all(d["eager_input_streaming"] is True for d in defs)
    assert {"get_current_time", "list_notes", "read_note", "write_note"} <= set(names)


def test_web_search_appended_last_when_enabled(settings):
    settings.web_search_enabled = True
    defs = REGISTRY.api_tools(settings)
    assert defs[-1] == {"type": "web_search_20260209", "name": "web_search", "max_uses": settings.web_search_max_uses}


def test_schema_titles_stripped_but_title_property_kept(settings):
    schema = next(d for d in REGISTRY.api_tools(settings) if d["name"] == "write_note")["input_schema"]
    assert "title" not in schema  # pydantic's auto "title": "WriteNoteArgs"
    assert "title" in schema["properties"]  # the real `title` parameter survives
    assert schema["additionalProperties"] is False


async def test_unknown_tool_lists_available(ctx):
    r = await REGISTRY.execute("nope", {}, ctx)
    assert r.is_error and "get_current_time" in r.output


async def test_case_insensitive_tool_name(ctx):
    r = await REGISTRY.execute("GET_CURRENT_TIME", {}, ctx)
    assert not r.is_error


async def test_invalid_input_is_reported_not_raised(ctx):
    r = await REGISTRY.execute("write_note", {"title": "x"}, ctx)  # content missing
    assert r.is_error and "content" in r.output
    r = await REGISTRY.execute("write_note", {"title": "x", "content": "y", "bogus": 1}, ctx)
    assert r.is_error


async def test_time_tool(ctx):
    ok = await REGISTRY.execute("get_current_time", {"timezone": "Asia/Karachi"}, ctx)
    assert not ok.is_error and "PKT" in ok.output
    bad = await REGISTRY.execute("get_current_time", {"timezone": "Mars/Base"}, ctx)
    assert bad.is_error and "Unknown timezone" in bad.output


async def test_notes_roundtrip_and_append(ctx):
    r = await REGISTRY.execute("write_note", {"title": "Ideas", "content": "first"}, ctx)
    assert "Saved note 'ideas'" in r.output
    await REGISTRY.execute("write_note", {"title": "Ideas", "content": "second", "mode": "append"}, ctx)
    assert (await REGISTRY.execute("read_note", {"title": "ideas"}, ctx)).output == "first\n\nsecond"
    assert "ideas" in (await REGISTRY.execute("list_notes", {}, ctx)).output


async def test_notes_cannot_escape_sandbox(ctx, settings):
    r = await REGISTRY.execute("write_note", {"title": "../../../evil", "content": "x"}, ctx)
    assert not r.is_error  # slugged into a harmless filename...
    written = list(settings.data_dir.rglob("*.md"))
    assert written and all(str(p.resolve()).startswith(str(settings.sandbox_dir.resolve())) for p in written)
    assert (await REGISTRY.execute("write_note", {"title": "../..", "content": "x"}, ctx)).is_error  # empty slug


async def test_read_missing_note(ctx):
    r = await REGISTRY.execute("read_note", {"title": "ghost"}, ctx)
    assert r.is_error and "list_notes" in r.output
