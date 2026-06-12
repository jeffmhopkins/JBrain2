"""Shared agent contracts — the wire/sidecar shapes the tracks build against.

Defined once (docs/ASSISTANT_PLAN.md, Wave 0) so the agent loop, the `.tool`
registry, the chat stream, and the PWA agree on a fixed surface: tool permission
classes and the session policy, the `.tool` sidecar frontmatter, citation refs and
tool-result views, and the streaming chat events. Serializable Pydantic models —
several cross the wire to the phone.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Tool permission classes & session policy ------------------------------

PermissionClass = Literal["read", "mutate", "external", "sensitive"]
"""How consequential a tool is; the session policy maps each class to an outcome
(docs/ASSISTANT.md "Session capabilities")."""

PolicyOutcome = Literal["direct", "staged", "denied"]
"""What a session does with a tool of a given class: run it now, stage it as a
Proposal for owner approval, or refuse it."""

CostClass = Literal["cheap", "standard", "expensive"]

# The default owner policy: reads run within the session's scope; writes and
# sensitive actions stage a Proposal; every off-box call stages an egress Proposal
# (the owner approves the exact payload before it leaves the box — invariant #9);
# nothing is denied outright for the owner.
DEFAULT_OWNER_POLICY: dict[PermissionClass, PolicyOutcome] = {
    "read": "direct",
    "mutate": "staged",
    "sensitive": "staged",
    "external": "staged",
}

# --- .tool sidecar frontmatter ---------------------------------------------

ResponseFormat = Literal["concise", "detailed"]


class ToolSpec(BaseModel):
    """The frontmatter of a `.tool` sidecar (docs/ASSISTANT.md "Tools as .tool
    sidecars"). The prose body — the model-facing description — is loaded beside
    it and is not part of this schema. `version` is CI-guarded and stamped on
    every run the tool participates in, so a behavior change is a deliberate
    migration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = Field(ge=1)
    # JSON Schema for the tool's arguments, surfaced to the model as the tool's
    # input schema (the same shape the adapter's LlmTool carries).
    params: dict[str, Any]
    # Scopes that may see the tool; empty means every scope. The session offers
    # only in-scope tools (visibility), but RLS at the DB layer is the boundary.
    domains: list[str] = Field(default_factory=list)
    permission: PermissionClass
    mutating: bool = False
    side_effecting: bool = False
    cost_class: CostClass = "cheap"
    response_format: ResponseFormat = "concise"


# --- Citations & tool-result views -----------------------------------------

Domain = Literal["general", "health", "finance", "location"]
Surface = Literal["inline", "sheet", "dialog"]


class FactRef(BaseModel):
    """A pointer-not-copy reference to a fact: a bare id plus a denormalized label
    for render; the hover-card fetches the live row (docs/ASSISTANT.md memory)."""

    kind: Literal["fact"] = "fact"
    fact_id: str
    label: str


class EntityRef(BaseModel):
    kind: Literal["entity"] = "entity"
    entity_id: str
    label: str
    domain: Domain


class NoteRef(BaseModel):
    kind: Literal["note"] = "note"
    note_id: str
    label: str


CitationRef = Annotated[FactRef | EntityRef | NoteRef, Field(discriminator="kind")]


class NoteSource(BaseModel):
    """A note a read tool surfaced this turn, for the response's source cards:
    enough to render (a domain dot + the snippet) and to open it (note_id). Sent
    alongside the tool result so the PWA renders structured cards instead of
    parsing the summary text."""

    note_id: str
    domain: str
    snippet: str


class ProposalRef(BaseModel):
    """A Proposal a tool staged this turn, for a tappable "Review proposal" chip —
    so the model never has to paste the id into its prose (it surfaces as a
    control routed to the review inbox)."""

    proposal_id: str
    kind: str


class ViewPayload(BaseModel):
    """A tool result's rich UI: a registered first-party component plus data-only
    typed slots (docs/DESIGN.md "Agent tool views"). Never model-authored markup;
    the named component is rendered from a fixed registry, or nothing is."""

    view: str
    surface: Surface = "inline"
    # Validated against the named component's schema downstream (P4.2); a payload
    # that fails its component schema is rejected, not rendered.
    data: dict[str, Any]
    refs: list[CitationRef] = Field(default_factory=list)


# --- Streaming chat events --------------------------------------------------
#
# The `/chat` endpoint emits these to the PWA (P4.5). A discriminated union on
# `type` so the client can switch without guessing.


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultEvent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    ok: bool
    summary: str
    # Structured notes the tool surfaced (search hits, the note read), for the
    # response's source cards; empty for tools that cite nothing.
    sources: list[NoteSource] = Field(default_factory=list)
    # A Proposal the tool staged this turn, surfaced as a "Review proposal" chip.
    proposal: ProposalRef | None = None
    # Entities a tool resolved this turn (find_entity), surfaced as tappable chips.
    entities: list[EntityRef] = Field(default_factory=list)


class ToolViewEvent(BaseModel):
    type: Literal["tool_view"] = "tool_view"
    tool_call_id: str
    view: ViewPayload


class JobEnqueuedEvent(BaseModel):
    """A long-running tool deferred to the job queue; the turn never blocks."""

    type: Literal["job_enqueued"] = "job_enqueued"
    job_id: str
    summary: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    stop_reason: str


ChatEvent = Annotated[
    TextDelta | ToolCallEvent | ToolResultEvent | ToolViewEvent | JobEnqueuedEvent | DoneEvent,
    Field(discriminator="type"),
]
