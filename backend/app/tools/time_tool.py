from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import ToolContext, ToolError, tool


class TimeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(
        default=None,
        description="IANA timezone name such as 'Asia/Karachi' or 'America/New_York'. "
        "Defaults to the server's local timezone.",
    )


@tool(
    name="get_current_time",
    description="Get the current date, day of week and time. Use this for any question that "
    "depends on today's date or the current time.",
    input_model=TimeArgs,
    tier="read",
)
def get_current_time(args: TimeArgs, ctx: ToolContext) -> str:
    if args.timezone:
        try:
            now = datetime.now(ZoneInfo(args.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            raise ToolError(f"Unknown timezone '{args.timezone}'. Use an IANA name like 'Europe/London'.")
    else:
        now = datetime.now().astimezone()
    return f"{now:%A, %d %B %Y, %H:%M:%S} ({now.tzname()}, UTC{now:%z}) — ISO: {now.isoformat(timespec='seconds')}"
