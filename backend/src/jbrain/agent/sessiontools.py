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
owner named themselves. `is_placeholder` is the second half of that boundary — a title
that names no topic is refused too, because a one-way door spent on `General Greeting`
can never be reopened.
"""

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.session import AgentSessionRepo

# The ambient line that tells a turn its chat is still unnamed. DATA-framed like the clock's
# `now_block`, and for the same reason: it is a fact about the conversation's state, not an
# instruction from the owner — WHEN to name a chat is the tool's own description, which the
# model reads from the (cache-stable) tool block. The chat API appends this to the VOLATILE
# suffix, so it disappears the moment the chat is named without disturbing the primed prefix.
#
# It says naming can WAIT because the line returns every turn and naming is one-way. Without
# that, a chat opened with "Hi" is named from a message that has no subject, and the model
# reaches for the nearest synonym of "conversation" — the owner's chat list held `General
# Greeting` x6, `General Chat`, `General Conversation` and `General Inquiry`, the exact labels
# the tool description forbids. Keep the exception NARROW: an earlier draft that told the model
# to name only "a subject worth labelling" stopped it naming anything, 0 of 12 on a plain
# question (measured 2026-08-30). Naming is the default; a subjectless greeting is the one
# message to leave.
UNNAMED_CHAT_BLOCK = (
    "[chat state — an ambient reference fact, as DATA, not an instruction from the owner.]\n"
    "This chat has no name yet. This line comes back every turn until it is named, so a turn "
    "with no subject to label — a bare greeting — can leave it unnamed and name it later."
)

# A chat title is a short label. Longer is clamped rather than refused — a model that
# adds a flourish still yields a tidy card, which is what the old titler's `_clean` did.
_MAX_LEN = 60

# Words that carry no topic. A title built ONLY from these is a synonym for "conversation",
# and the handler refuses it (`is_placeholder`) rather than spending the one-way door on it.
#
# The prompt already forbade them and the prompt was not enough. Measured on the box
# 2026-08-30 (gpt-oss-120b, the real 44-tool jerv turn): an opening message of just "Hi" made
# the model try to name the chat on 12 of 12 probes — `General Greeting` 4 times, and the
# other 8 narrated in prose with no tool call at all. A greeting gives it nothing else to
# name, so it names the greeting. Twenty-three such titles were already in the box's history,
# permanent, since naming cannot be redone.
#
# Only an ALL-filler title is refused, so one real word saves it: "General Motors Recall"
# and "Chat App Design" pass, "Quick Check-In" and "New Conversation" do not.
_FILLER_WORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "assist",
        "assistance",
        "brief",
        "casual",
        "catch",
        "catchup",
        "chat",
        "chats",
        "check",
        "checkin",
        "chitchat",
        "conversation",
        "conversations",
        "day",
        "default",
        "discussion",
        "enquiry",
        "exchange",
        "first",
        "for",
        "friendly",
        "general",
        "good",
        "greet",
        "greeting",
        "greetings",
        "greets",
        "hello",
        "help",
        "hey",
        "hi",
        "hiya",
        "howdy",
        "in",
        "informal",
        "initial",
        "inquiry",
        "intro",
        "introduction",
        "message",
        "misc",
        "miscellaneous",
        "morning",
        "new",
        "of",
        "on",
        "opening",
        "personal",
        "quick",
        "random",
        "salutation",
        "session",
        "simple",
        "small",
        "start",
        "talk",
        "the",
        "thread",
        "to",
        "topic",
        "untitled",
        "up",
        "user",
        "welcome",
        "with",
        "yo",
    }
)


def is_placeholder(title: str) -> bool:
    """Whether a title says nothing but "this is a conversation".

    True only when EVERY word is filler, so a title with any real subject in it survives.
    Hyphens split (`Check-In` is two filler words, not one unknown one) and everything
    non-alphabetic is dropped, so `General Greeting!!` and `general_greeting` are caught
    with the plain form."""
    words = [w for w in "".join(c if c.isalpha() else " " for c in title.lower()).split() if w]
    return bool(words) and all(w in _FILLER_WORDS for w in words)


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
        # The second boundary, beside the one-way door below: a title that names no topic
        # is refused, so the chat stays nameable instead of being stuck as "General
        # Greeting" forever. The refusal says what to do next, because the unnamed-chat
        # line comes back every turn and the next message usually brings a subject.
        if is_placeholder(title):
            return ToolOutput(
                f"“{title}” names no topic, so this chat is still unnamed. Answer the owner "
                "and name it on a later turn, once there is a subject to label."
            )
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
