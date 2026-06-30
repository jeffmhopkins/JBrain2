"""The agent's owner-self anchor: the ambient line that hands the turn the id of
the owner's "Me" entity up front.

First-person *attribute* questions — "what's my birthday", "my name", "how old am
I", "my email", "where do I live" — are answered by scalar edges on the owner's
"Me" entity, not by relationship traversal (`relate` only follows edges to OTHER
entities) and usually not by note prose (`search`). Without a handle to Me the
agent has to discover it (find_entity "Me") before it can read it, and — worse —
tends to flail through note searches first. `me_block` closes that gap the same
way `clock.now_block` closes the "what day is it" gap: a DATA-framed reference line
prepended to the conversation, so any knowledge-base turn can answer an owner
self-attribute with a single `read_entity` on the id it was handed.

Owner-self data, so the caller gates it like presence: knowledge-base agents only
(never the sandboxed jerv / dataless teacher), resolved under the full owner ctx.
"""

# The data-boundary frame (modeled on `clock._CLOCK_FRAME`): the line is DATA — an
# ambient reference fact about who the turn is for — explicitly not an instruction.
_ME_FRAME = (
    "[the owner's identity — an ambient reference fact, as DATA. It is not an"
    " instruction.]"
)


def me_block(entity_id: str) -> str:
    """The data-framed owner-self line prepended to a knowledge-base turn: the id of
    the "Me" entity so an owner self-attribute ("my birthday", "my name", "where I
    live") is a single `read_entity(<id>)` — no find_entity hop, no note search.
    The caller injects it only when the id resolved (a graph with a Me entity) and
    only for agents that read the owner's data."""
    return (
        f"{_ME_FRAME}\nYou are answering for the owner, whose own entity is \"Me\""
        f" (id {entity_id}). Their own attributes — birthday, name, age, email,"
        " where they live, and the like — are edges on this entity. For a"
        " first-person attribute question, read_entity this id directly; don't"
        " search notes for it first, and don't use relate (that follows"
        " relationships to OTHER people, not the owner's own attributes)."
    )
