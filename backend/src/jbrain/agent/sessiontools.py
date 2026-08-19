"""The `name_session` tool: the chat names itself from inside its own turn.

This replaces a separate `session.title` completion that ran BEFORE every untitled
chat's first response. That call was cheap in tokens and expensive in practice: it
followed the interactive model (`_FOLLOW_PRIMARY_MODEL`), so on a one-slot local model
its ~200-token prompt landed in the slot holding jerv's ~32k primed prefix and evicted
it — and the real turn behind it then paid a ~100 s cold prefill, measured on the box,
against 0.99 s warm. Titling a chat is not worth a cold prefill.

Naming from inside the turn removes the second call entirely rather than making it
cheaper: the prefix is never disturbed, and the model choosing the name is the one that
just read the whole message rather than a summarizer working from its first 200 tokens.

The handler is the boundary, not the prompt: it refuses a chat that already has a name,
so neither a confused model nor an injected instruction can rename a conversation the
owner named themselves.
"""

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.session import AgentSessionRepo

# The ambient line that tells a turn its chat is still unnamed. DATA-framed like the clock's
# `now_block`, and for the same reason: it is a fact about the conversation's state, not an
# instruction from the owner — WHEN to name a chat is the tool's own description, which the
# model reads from the (cache-stable) tool block. The chat API appends this to the VOLATILE
# suffix, so it disappears the moment the chat is named without disturbing the primed prefix.
UNNAMED_CHAT_BLOCK = (
    "[chat state — an ambient reference fact, as DATA, not an instruction from the owner.]\n"
    "This chat has no name yet."
)

# A chat title is a short label. Longer is clamped rather than refused — a model that
# adds a flourish still yields a tidy card, which is what the old titler's `_clean` did.
_MAX_LEN = 60


def clean_title(raw: str) -> str:
    """First line only, stripped of surrounding quotes and trailing punctuation, capped.

    Models decorate short answers and the chat card has no room to be forgiving. Carried over
    from the titler this replaces, with one fix: that version stripped quotes ONCE and then the
    period, so the very common `"Roof Quotes".` came out as `Roof Quotes"` — the decoration it
    exists to remove, left on the card. Peeling until nothing more comes off handles either
    order."""
    head = next((line for line in raw.splitlines() if line.strip()), "")
    for _ in range(len(head) or 1):  # bounded: each pass removes at least one char, or stops
        peeled = head.strip().strip("\"'“”").rstrip(".")
        if peeled == head:
            break
        head = peeled
    return head.strip()[:_MAX_LEN].strip()


def build_session_handlers(sessions: AgentSessionRepo) -> dict[str, ToolHandler]:
    """The `name_session` tool, bound to the session repo the chat API also renames through."""

    async def name_session_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        title = clean_title(str(arguments.get("name", "")))
        if not title:
            return ToolOutput("No name given — pass a short title in `name`.")
        # Non-chat callers (the wiki Editor, background loops) have no session to name.
        if not ctx.agent_session_id:
            return ToolOutput("This run is not a chat, so there is nothing to name.")

        current = await sessions.get(ctx.session, ctx.agent_session_id)
        if current is None:
            return ToolOutput("That chat no longer exists.")
        # The guard that makes this safe to hand a sandboxed chatbot: naming is a
        # one-way door from unnamed to named. An owner's own title is never overwritten,
        # so a second call — a confused model, a retry, or text in a fetched page telling
        # the agent to rename the chat — changes nothing.
        if current.title.strip():
            return ToolOutput(f"This chat is already named “{current.title}” — leaving it as is.")

        await sessions.rename(ctx.session, ctx.agent_session_id, title)
        return ToolOutput(f"Named this chat “{title}”.")

    return {"name_session": name_session_tool}
