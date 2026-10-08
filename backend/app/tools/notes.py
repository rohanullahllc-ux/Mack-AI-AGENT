"""Notes stored as markdown files inside the sandbox directory (and nowhere else)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import ToolContext, ToolError, tool

MAX_NOTE_BYTES = 100_000
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _notes_dir(ctx: ToolContext) -> Path:
    d = ctx.settings.sandbox_dir / "notes" / ctx.user_id
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _note_path(ctx: ToolContext, title: str) -> Path:
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")[:80]
    if not slug:
        raise ToolError("Note title must contain letters or numbers.")
    root = _notes_dir(ctx)
    path = (root / f"{slug}.md").resolve()
    if not path.is_relative_to(root):  # slug is already sanitised; this is defence in depth
        raise ToolError("Invalid note title.")
    return path


class ListNotesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Title of the note, as returned by list_notes.")


class WriteNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Short note title, e.g. 'Meeting with Ali'.")
    content: str = Field(description="Markdown body of the note.")
    mode: Literal["overwrite", "append"] = Field(
        default="overwrite", description="'append' adds to the end of an existing note."
    )


@tool(
    name="list_notes",
    description="List the user's saved notes (titles and sizes).",
    input_model=ListNotesArgs,
    tier="read",
)
def list_notes(args: ListNotesArgs, ctx: ToolContext) -> str:
    files = sorted(_notes_dir(ctx).glob("*.md"))
    if not files:
        return "No notes saved yet."
    return "\n".join(f"- {f.stem} ({f.stat().st_size} bytes)" for f in files)


@tool(
    name="read_note",
    description="Read the full text of a saved note.",
    input_model=ReadNoteArgs,
    tier="read",
)
def read_note(args: ReadNoteArgs, ctx: ToolContext) -> str:
    path = _note_path(ctx, args.title)
    if not path.exists():
        raise ToolError(f"No note titled '{args.title}'. Use list_notes to see what exists.")
    return path.read_text(encoding="utf-8")


@tool(
    name="write_note",
    description="Create, overwrite or append to a note. Use this to save anything the user asks "
    "you to remember or write down.",
    input_model=WriteNoteArgs,
    tier="write",
)
def write_note(args: WriteNoteArgs, ctx: ToolContext) -> str:
    path = _note_path(ctx, args.title)
    existing = path.read_text(encoding="utf-8") if (args.mode == "append" and path.exists()) else ""
    text = f"{existing.rstrip()}\n\n{args.content}".lstrip() if existing else args.content
    if len(text.encode("utf-8")) > MAX_NOTE_BYTES:
        raise ToolError(f"Note too large (limit {MAX_NOTE_BYTES} bytes).")
    path.write_text(text, encoding="utf-8")
    verb = "Appended to" if existing else "Saved"
    return f"{verb} note '{path.stem}' ({len(text.encode('utf-8'))} bytes)."
