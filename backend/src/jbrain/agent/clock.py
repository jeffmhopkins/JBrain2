"""The agent's clock: the ambient current-date/time the conversation is grounded
in, and the `current_time` tool behind it.

A turn needs to know *when it is* — "what's due this week", "is that today" — yet
the model has no clock of its own. Two complementary surfaces close that gap:

- `now_block` injects the current date + local time as a DATA-framed reference line
  at the head of every turn (the same conversation-channel pattern as the skills and
  presence blocks), so every agent passively knows the day without calling anything.
- `current_time` is a tool for a fresh reading mid-conversation or the time in a
  SPECIFIC timezone — it reads a clock only, no owner notes or domain data, so it is
  safe even for the sandboxed `jerv` chatbot.

Times render in the owner's IANA display zone (`ToolContext.timezone`), falling back
to UTC when unknown — so the prose agrees with the client-localized cards.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput

# The data-boundary frame for the injected line (modeled on `skills._SKILL_FRAME` /
# `presence._PRESENCE_FRAME`): the line is DATA — an ambient reference fact about the
# current instant — explicitly not an instruction.
_CLOCK_FRAME = (
    "[current date & time — an ambient reference fact, as DATA. Use it to ground"
    " anything time-relative; it is not an instruction.]"
)


def _resolve(tz_name: str | None) -> tuple[ZoneInfo, str]:
    """The owner's display zone (and its label), falling back to UTC when the name is
    unknown/unset — so rendering never raises mid-turn (the same degrade-to-UTC the
    location prose uses)."""
    if tz_name:
        try:
            return ZoneInfo(tz_name), tz_name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo("UTC"), "UTC"


def _sentence(local: datetime, label: str) -> str:
    return f"It is currently {local:%A, %B %d, %Y, %H:%M} ({label})."


def now_block(tz: str | None, *, now: datetime | None = None) -> str:
    """The data-framed current-date/time line prepended to the agent conversation:
    the `_CLOCK_FRAME` banner leads, demoting the sentence after it to DATA. Always
    present (a turn always has a "now") — unlike presence/skills, there is nothing to
    be absent."""
    zone, label = _resolve(tz)
    local = (now or datetime.now(UTC)).astimezone(zone)
    return f"{_CLOCK_FRAME}\n{_sentence(local, label)}"


def build_clock_handlers() -> dict[str, ToolHandler]:
    """The `current_time` tool — a clock read, no owner data. Visible to every agent
    that holds it (the curator by default; the sandboxed jerv by allowlist), since it
    touches no scoped data and so needs no domain."""

    async def current_time_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        requested = str(arguments.get("timezone", "")).strip()
        if requested:
            try:
                zone = ZoneInfo(requested)
            except (ZoneInfoNotFoundError, ValueError):
                # An explicit bad zone name is the model's error to recover from —
                # answer in UTC and say why, rather than silently using the owner's.
                now_utc = datetime.now(UTC)
                return ToolOutput(
                    f"'{requested}' isn't a known IANA timezone name. Right now it is"
                    f" {now_utc:%A, %B %d, %Y, %H:%M} (UTC)."
                )
            label = requested
        else:
            zone, label = _resolve(ctx.timezone)
        local = datetime.now(UTC).astimezone(zone)
        return ToolOutput(_sentence(local, label))

    return {"current_time": current_time_tool}
