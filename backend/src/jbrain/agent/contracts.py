"""Shared agent contracts — the wire/sidecar shapes the tracks build against.

Defined once (docs/archive/ASSISTANT_PLAN.md, Wave 0) so the agent loop, the `.tool`
registry, the chat stream, and the PWA agree on a fixed surface: tool permission
classes and the session policy, the `.tool` sidecar frontmatter, citation refs and
tool-result views, and the streaming chat events. Serializable Pydantic models —
several cross the wire to the phone.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Tool permission classes & session policy ------------------------------

PermissionClass = Literal["read", "mutate", "external", "sensitive", "web"]
"""How consequential a tool is; the session policy maps each class to an outcome
(docs/reference/ASSISTANT.md "Session capabilities").

`web` is the jerv sandbox's opt-in, direct-exec class: a tool that runs DIRECTLY
(no egress Proposal) and is reserved for the `jerv` chatbot agent. Most members are
off-box internet reads (`web_search`, `web_fetch`) — jerv holds no knowledge-base
tools, so there is nothing personal in its context to exfiltrate into a query
(docs/reference/ASSISTANT.md "Agent selection", the deliberate, owner-approved exception to
invariant #9). The one non-internet member is `current_location`: it names the live
position the owner's app shared THIS turn — by default an OFFLINE nearest-city lookup
(no egress, no read of the firewalled location domain: no saved places, no device
stack), escalating to the owner-configured external reverse-geocoder ONLY for an
explicitly requested street address (a direct lookup of just that coordinate). It is
in this class purely for its gate (owner-approved, jerv-only). A `web` tool is opt-in:
the registry never offers it to an agent that did not explicitly allowlist it, so the
Full Brain `curator` never gains web access or this location tool."""

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
    # The sandboxed web class runs directly — the jerv chatbot's only egress, with
    # no owner data in its context (docs/reference/ASSISTANT.md "Agent selection").
    "web": "direct",
}

# --- .tool sidecar frontmatter ---------------------------------------------

ResponseFormat = Literal["concise", "detailed"]


class ToolSpec(BaseModel):
    """The frontmatter of a `.tool` sidecar (docs/reference/ASSISTANT.md "Tools as .tool
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
    for render; the hover-card fetches the live row (docs/reference/ASSISTANT.md memory)."""

    kind: Literal["fact"] = "fact"
    fact_id: str
    label: str


class EntityRef(BaseModel):
    kind: Literal["entity"] = "entity"
    entity_id: str
    label: str
    domain: Domain
    # Other surface forms (aka) so the PWA can linkify a name in prose that isn't
    # the canonical label — e.g. "Jeff Hopkins" for an entity canonically "Me".
    aliases: list[str] = Field(default_factory=list)
    # The entity's current-fact statements *as read* (read_entity only) — e.g.
    # "Jeff's birth date is 1986-03-19". Carried so the reflexion grounding verifier
    # can match a claim answered from a fact VALUE ("born in 1986") against the fact
    # text, not just the name/aliases — otherwise an entity-graph answer is falsely
    # flagged "not in your notes". Empty for a find_entity/relate ref (those surface a
    # name only) and for the related-object chips read_entity also returns; the same
    # prose is already in the tool result the PWA shows, so this copies nothing new.
    facts: list[str] = Field(default_factory=list)


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


class WebSource(BaseModel):
    """A web page a jerv internet tool reached this turn (a `web_search` hit or a
    `web_fetch` target), for the response's citation chips. The `url` is captured
    from the actual tool call — SearXNG's result row, or the page's final URL after
    redirects — NEVER parsed from the model's prose, so a chip points at a source
    the tool genuinely reached, not a string the model typed (which is how the old
    `【source: …】` prose leaked: a citation with no followable link).

    The PWA renders each as a tappable favicon that opens the page. The favicon is
    fetched and cached ON-BOX and served from a same-origin route (`/agent/favicon`)
    — agent output triggers no render-time external resource load (invariant #9):
    the client only ever talks to our own API, which does the controlled,
    SSRF-guarded, cached fetch from the source host server-side."""

    url: str
    title: str


class ProposalRef(BaseModel):
    """A Proposal a tool staged this turn, for a tappable "Review proposal" chip —
    so the model never has to paste the id into its prose (it surfaces as a
    control routed to the review inbox)."""

    proposal_id: str
    kind: str


class ViewPayload(BaseModel):
    """A tool result's rich UI: a registered first-party component plus data-only
    typed slots (docs/reference/DESIGN.md "Agent tool views"). Never model-authored markup;
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


class ReasoningDelta(BaseModel):
    """One slice of the model's streamed reasoning trace (gpt-oss/GLM `reasoning_content`).
    The PWA renders these into a collapsible "thinking" disclosure that streams live and
    collapses to "Thought for Ns" once the answer begins. Display/provenance only — never
    part of the answer, the grounding corpus, or exported output."""

    type: Literal["reasoning_delta"] = "reasoning_delta"
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
    # Web pages a jerv internet tool reached this turn, for the response's favicon
    # citation chips; empty for every non-web tool. Kept separate from `sources`:
    # a web page is not an owner note, and it never feeds the notes-grounding
    # reflexion verifier (jerv is a sandbox with no notes to ground against).
    web_sources: list[WebSource] = Field(default_factory=list)
    # A Proposal the tool staged this turn, surfaced as a "Review proposal" chip.
    proposal: ProposalRef | None = None
    # Entities a tool resolved this turn (find_entity), surfaced as tappable chips.
    entities: list[EntityRef] = Field(default_factory=list)


class ToolViewEvent(BaseModel):
    type: Literal["tool_view"] = "tool_view"
    tool_call_id: str
    view: ViewPayload


class ToolProgressEvent(BaseModel):
    """A progress tick a still-running tool emitted mid-execution. Image generation
    streams ComfyUI's sampling progress + a sharpening preview (`step`/`total` are the
    sampler steps; `preview` is a small base-64 data URI the BACKEND authors for the
    ephemeral frame — invariant #9 forbids the *model* authoring a URL, this is
    app-authored, like the view component's <img> src). A multi-phase tool
    (analyze_video) instead sets `label` to a human phase ("Extracting frames…",
    "Analyzing frame 12/30") and may leave `total` 0. Display-only and EPHEMERAL —
    never persisted to the transcript; the final view is the durable artifact."""

    type: Literal["tool_progress"] = "tool_progress"
    tool_call_id: str
    step: int
    total: int
    preview: str | None = None
    # A human-readable phase for a multi-step tool (None for image gen's step bar).
    label: str | None = None


class JobEnqueuedEvent(BaseModel):
    """A long-running tool deferred to the job queue; the turn never blocks."""

    type: Literal["job_enqueued"] = "job_enqueued"
    job_id: str
    summary: str


class UsageEvent(BaseModel):
    """Live context-window accounting, emitted after each model turn (every ReAct
    step) so the PWA can show a "context used" meter that updates as the turn's
    tool chain — and the conversation — grows. `input_tokens` is the prompt the
    model just consumed (the whole conversation + tools fed in this step), so the
    latest event reflects the fullest the context has been; `output_tokens` is what
    that step generated. `context_window` is the resolved model's total window (for
    a local model the gateway's configured `-c`), the meter's denominator. Display
    only — never persisted, never part of the answer."""

    type: Literal["usage"] = "usage"
    input_tokens: int
    output_tokens: int
    context_window: int


class SubagentSpawnedEvent(BaseModel):
    """A sub-agent child launched inside a `spawn_subagent` fan (Wave S2). Emitted as
    the child starts so the in-chat accordion (Wave S3) can render one row per child.
    The BACKEND authors these (the model never does); they are read-only live
    telemetry — EPHEMERAL, never persisted (the durable record is the child
    `agent_session` + its run-log). `tool_call_id` is injected by the loop at emit so
    the row anchors under the spawning tool call; `child_id` keys the row and the
    session-tree surface; `persona` renders as a neutral tag, never a color."""

    type: Literal["subagent_spawned"] = "subagent_spawned"
    tool_call_id: str = ""
    child_id: str
    persona: str
    label: str
    depth: int
    # Which wave of a staged (feeding) fan this child belongs to (0-based; 0 for an
    # ordinary flat fan). Lets the grouped-by-wave surface (F3) bucket rows; the flat
    # fan ignores it (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md).
    wave: int = 0
    # For a wave-2 consumer, the labels of the earlier-wave producers whose summaries
    # were fed into its brief — renders the "← fed by …" edge. Empty for a producer.
    fed_from: list[str] = Field(default_factory=list)


class SubagentProgressEvent(BaseModel):
    """A status tick for a running child (Wave S2). v1 children run non-streaming
    (fan-in model A), so `phase` is a coarse working word ("researching") rather than
    token deltas. `tree_spent`/`tree_budget` are the shared-pool snapshot driving the
    in-chat budget meter (Wave S3), updated at each child lifecycle tick. `tree_budget` is
    the CHILDREN'S pool (tree budget minus the root's synthesis reserve) — the ceiling a
    child actually stops at — so the meter fills as children exhaust, not phantom headroom.
    Ephemeral, never persisted."""

    type: Literal["subagent_progress"] = "subagent_progress"
    tool_call_id: str = ""
    child_id: str
    phase: str
    # The child's ReAct step so far (0 at launch); emitted every step so the row shows
    # live movement ("researching · 4 steps") while a non-streaming child works.
    step: int = 0
    tree_spent: int = 0
    tree_budget: int = 0


class SubagentUsageEvent(BaseModel):
    """A running child's context fill (Wave S3 follow-up): the child loop's per-call usage
    forwarded — tagged by `child_id` — so the fan row shows a live context meter, the
    non-streaming twin of the parent turn's UsageEvent. `used` is the latest model call's
    prompt + output (the fullest the child's context has been); `context_window` is the
    child model's total window. Ephemeral, never persisted."""

    type: Literal["subagent_usage"] = "subagent_usage"
    tool_call_id: str = ""
    child_id: str
    used: int
    context_window: int


class SubagentDeltaEvent(BaseModel):
    """A live token slice from a running child (Wave S3 follow-up): the child's loop
    streams its turns, and each answer/reasoning chunk is forwarded — tagged by
    `child_id` — onto the parent turn's stream, so the in-chat fan can show the child
    *working* (a live mini-transcript) rather than only a step counter. `channel` is
    "answer" (the child's prose) or "reasoning" (its thinking trace). Ephemeral, never
    persisted — the durable record is the child's own transcript + run-log."""

    type: Literal["subagent_delta"] = "subagent_delta"
    tool_call_id: str = ""
    child_id: str
    channel: Literal["answer", "reasoning"] = "answer"
    text: str = ""


class SubagentToolEvent(BaseModel):
    """A tool step a running child took (Wave S3 follow-up): forwarded — tagged by
    `child_id` — onto the parent turn's stream so the fan's child frame shows its work
    like a real session (a live "Worked" list: web_search, web_fetch, …). `arg` is a
    short inline preview (the query / url), `ok` its success. Ephemeral, never
    persisted — the durable record is the child's own run-log."""

    type: Literal["subagent_tool"] = "subagent_tool"
    tool_call_id: str = ""
    child_id: str
    name: str
    arg: str = ""
    ok: bool = True


class SubagentDoneEvent(BaseModel):
    """A child finished (Wave S2): `ok` is a clean, substantive answer; otherwise it
    errored or degraded (max_steps / empty). The accordion flips the row to green ✓ /
    rose ✕ and shows `summary` (the child's answer, or its error/truncation note) on
    expand (Wave S3). `tree_spent`/`tree_budget` refresh the budget meter (`tree_budget` is
    the children's pool — the child ceiling — so the bar fills as children exhaust).
    Ephemeral, never persisted — the durable record is the child run-log."""

    type: Literal["subagent_done"] = "subagent_done"
    tool_call_id: str = ""
    child_id: str
    ok: bool
    stop_reason: str = ""
    summary: str = ""
    tree_spent: int = 0
    tree_budget: int = 0
    # Why a staged consumer never ran, when applicable (feeding waves, F2): an upstream
    # failure ("upstream … unavailable"), a drained pool ("… budget spent by earlier
    # waves"), or the wall-clock deadline. Empty for a child that actually ran — a skip
    # is a cascade/resource event, rendered distinctly from a failure.
    skip_reason: str = ""


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    stop_reason: str


class VerdictEvent(BaseModel):
    """Reflexion's Loop-1 verdict on a critique-worthy turn (docs/reference/ASSISTANT.md
    "Self-improvement loops"). In the default verify-and-annotate mode it rides
    *after* `DoneEvent` — the answer the user already saw stands, annotated: the
    PWA renders an "unverified claims" note when `passed` is false. The score is
    the aggregated verifier score (0..1) and `issues` are the concrete
    ungrounded-claim / out-of-scope-citation strings the verifiers found. A
    passing turn emits no VerdictEvent at all (nothing to annotate); this event
    appears only when the verifiers flagged something. Ephemeral — never persisted
    (Loop 1 writes nothing).

    `ungrounded_claims` is the structured twin of the grounding subset of `issues`:
    the *verbatim* answer sentences that failed grounding, so the PWA can anchor an
    inline "unverified" flag against the exact prose instead of re-parsing the
    prose `issues` prefix. `issues` stays the full human-readable list (it also
    carries any citation/mutation issues, which have no answer-sentence to anchor)."""

    type: Literal["verdict"] = "verdict"
    passed: bool
    score: float
    issues: list[str] = Field(default_factory=list)
    ungrounded_claims: list[str] = Field(default_factory=list)


class GeneralKnowledgeEvent(BaseModel):
    """The neutral provenance label for a turn answered purely from the model's own
    world knowledge — zero retrieval (no note sources, no graph entities) yet a
    substantive claim (docs/reference/ASSISTANT.md). Like `VerdictEvent` it rides *after*
    `DoneEvent`, but it is NOT a warning: it carries no claims and is mutually
    exclusive with the amber verdict (a turn that retrieved nothing can't be
    grounding-flagged; a turn that retrieved evidence is judged by the verifiers
    instead). The PWA renders a calm "from general knowledge — not your notes" chip.
    A greeting / acknowledgement (no substantive claim) emits nothing. Ephemeral —
    never persisted (Loop 1 writes nothing)."""

    type: Literal["general_knowledge"] = "general_knowledge"


ChatEvent = Annotated[
    TextDelta
    | ReasoningDelta
    | ToolCallEvent
    | ToolResultEvent
    | ToolViewEvent
    | ToolProgressEvent
    | JobEnqueuedEvent
    | UsageEvent
    | SubagentSpawnedEvent
    | SubagentProgressEvent
    | SubagentUsageEvent
    | SubagentDeltaEvent
    | SubagentToolEvent
    | SubagentDoneEvent
    | DoneEvent
    | VerdictEvent
    | GeneralKnowledgeEvent,
    Field(discriminator="type"),
]
