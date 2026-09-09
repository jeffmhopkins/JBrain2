"""The two tools that write the graph, at the level that needs no database.

The write behaviour itself is `tests/integration/test_note_graph_write_pg.py` — it goes
through `commit_facts` and `decide()`, and faking those would test the fake. What is
here is everything that is decided BEFORE a row is written and everything the registry
enforces around it:

- the SIDECARS, because the schema is the reliability contract with the model: the batch
  shape that was measured, every load-bearing field `required` (TOOL_SURFACE R3), and no
  JSON-Schema `enum` anywhere in the union the persona is offered (plan constraint 8 —
  an enum in this persona's tool set segfaults gpt-oss's harmony grammar);
- the WIRING, because D8 and D16 are properties of which handlers are bound, never of
  the prompt (plan constraint 9): the allowlist, `NEVER_DEFAULT`, and the fact that the
  chat registry cannot serve these tools at all;
- the argument reading, because a batch is where a well-formed call quietly becomes a
  dropped fact.
"""

import json
from pathlib import Path

from jbrain.agent import graphwritetools as gw
from jbrain.agent.agents import NOTE_INGEST_TOOLS, agent_for
from jbrain.agent.readtools import OPTIONAL_NOTE_GRAPH_TOOLS
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import NEVER_DEFAULT
from jbrain.analysis.converse import NOTE_READ_TOOLS
from jbrain.analysis.pipeline import ALREADY, HELD, REPLACED, WRITTEN, FactWrite

_TOOLS = Path(gw.__file__).parent / "tools"


def _spec(name: str):  # noqa: ANN202
    return load_tool(_TOOLS / f"{name}.tool").spec


# --- the sidecars -------------------------------------------------------------


def test_both_tools_take_the_measured_batch_shape() -> None:
    """W2 probed the live model rather than guessing: batched arrays-of-objects came back
    20/20 well-formed at 7.6 facts and 8.9 entities per turn, against 1.0 for the flat
    one-per-call fallback (TOOL_SURFACE cut 5). A regression to flat scalars would be
    eight round trips per note on a serial GPU with the owner waiting."""
    facts = _spec("assert_fact").params["properties"]["facts"]
    assert facts["type"] == "array"
    assert facts["maxItems"] == gw.MAX_FACTS == 8
    assert facts["items"]["type"] == "object"

    entities = _spec("resolve_entity").params["properties"]["entities"]
    assert entities["type"] == "array"
    assert entities["maxItems"] == gw.MAX_ENTITIES == 12
    assert entities["items"]["type"] == "object"


def test_every_field_the_write_needs_is_required() -> None:
    """R3: required fields are the only reliable fields — across 85 consecutive
    `scratch_write` calls the model filled the required one every time and the optional
    one never once, and llama.cpp compiles `required` into the tool grammar. An optional
    `quote` would mean no fact is ever attested; an optional `object` would mean no fact
    ever has a value."""
    item = _spec("assert_fact").params["properties"]["facts"]["items"]
    assert set(item["required"]) == {
        "subject",
        "predicate",
        "object",
        "statement",
        "when",
        "quote",
    }
    assert set(item["properties"]) == set(item["required"])

    surface = _spec("resolve_entity").params["properties"]["entities"]["items"]
    assert set(surface["required"]) == {"surface", "kind"} == set(surface["properties"])


def test_the_schemas_the_model_must_never_be_offered_a_domain_or_an_enum() -> None:
    """Two rules that are not cuttable under any pressure.

    No `domain` / `sensitive` / `inferred` / `supersedes` / `correction` field: the
    firewall red-team rule is that a per-fact domain never comes from the model
    (`arbiter.py:163-165`), and the other three are recomputed or owned by `decide()`.

    No JSON-Schema `enum` anywhere: the GBNF grammar is built over the WHOLE tool union
    offered that turn, so one enum in one sidecar is enough (plan constraint 8). The
    enumerated values live in the descriptions and are validated in the handler."""
    for name in ("assert_fact", "resolve_entity"):
        blob = json.dumps(_spec(name).params)
        assert '"enum"' not in blob, f"{name} carries an enum — the harmony grammar segfault"
        for banned in ("domain", "sensitive", "inferred", "supersedes", "correction", "note_id"):
            assert f'"{banned}"' not in blob, f"{name} offers a `{banned}` field"


def test_the_kinds_are_described_not_enumerated_in_the_schema() -> None:
    """The closed set is real — it is just enforced in the handler. An unrecognised word
    degrades to the resolver's own default rather than failing the element, because a
    dropped surface is a dropped fact."""
    described = load_tool(_TOOLS / "resolve_entity.tool").spec.params["properties"]["entities"][
        "items"
    ]["properties"]["kind"]["description"]
    for word in ("person", "organization", "place", "event", "condition", "medication"):
        assert word in described
        assert word in gw._KIND_HINTS
    assert gw._KIND_HINTS.get("wombat") is None


def test_both_tools_declare_themselves_writes() -> None:
    """`permission` is documentation rather than a gate here (`outcome_for` is never
    called by the loop), but the loop DOES read `mutating`/`side_effecting` to decide a
    turn mutated — which drives the reflexion critique — and the roster gate reads the
    class."""
    for name in ("assert_fact", "resolve_entity"):
        spec = _spec(name)
        assert spec.permission == "mutate"
        assert spec.mutating and spec.side_effecting
        # Visible in every scope: the conversation runs narrowed to (note_domain,
        # 'general'), and a health note's thread must still be able to call them.
        assert spec.domains == []


# --- the wiring ---------------------------------------------------------------


def test_the_write_tools_are_never_absorbed_by_the_curator_wildcard() -> None:
    """Plan constraint 9. `allow=None` (the curator) admits every in-scope tool except
    `web` and `NEVER_DEFAULT`, so without this the note persona's graph writes would be
    handed to the curator on every ordinary chat turn."""
    assert gw.GRAPH_WRITE_TOOLS <= NEVER_DEFAULT


def test_the_chat_registry_cannot_serve_them_at_all() -> None:
    """The outermost lock, and the reason it is unconditional rather than feature-gated:
    a handler is bound to ONE note (its id, domain, chunks and handle table live in the
    writer), so a chat session has nothing to bind. `build_registry` drops both sidecars
    instead of demanding handlers for them."""
    assert OPTIONAL_NOTE_GRAPH_TOOLS == gw.GRAPH_WRITE_TOOLS


def test_the_allowlist_and_the_bound_registry_are_the_same_set() -> None:
    """A tool in the allowlist with no handler is a call that dies in dispatch; a bound
    handler outside it is one the model is never offered. Either way the persona's real
    surface is not the one anybody wrote down."""
    profile = agent_for("note_ingest")
    assert profile.tools == NOTE_INGEST_TOOLS
    assert NOTE_INGEST_TOOLS == gw.GRAPH_WRITE_TOOLS | NOTE_READ_TOOLS


def test_nothing_outward_facing_is_in_the_unattended_set() -> None:
    """D8: nothing outward-facing runs while the owner is asleep. The trifecta this
    keeps apart is untrusted note text + private data + egress."""
    for banned in (
        "web_search",
        "web_fetch",
        "file_correction",
        "propose_correction",
        "add_source_exclusion",
        "make_intake_link",
        "remember",
        "neighborhood",
        "analyze_image",
    ):
        assert banned not in NOTE_INGEST_TOOLS


def test_a_note_registry_holds_exactly_what_it_was_asked_for() -> None:
    """Not `load_registry`, which globs the directory and demands a handler for all 116
    sidecars. Building from names is what makes "this persona reaches nothing else"
    structural."""

    async def _noop(arguments: dict, ctx: object) -> str:  # noqa: ANN401
        return ""

    registry = gw.note_registry(_TOOLS, {"assert_fact": _noop, "find_entity": _noop})
    assert registry.names() == {"assert_fact", "find_entity"}
    # And the allowlist still narrows within it (D16 is a second lock over the same set).
    assert registry.allowed_names(("general",), allow=frozenset({"find_entity"})) == {"find_entity"}


# --- reading a batch ----------------------------------------------------------


def test_the_batch_is_clamped_by_the_handler_not_by_max_items() -> None:
    """`maxItems` is validated for the json_schema path but llama.cpp builds its own
    grammar for tool calling and the two do not compose, so the handler is the only real
    ceiling — and it reports the clamp rather than silently truncating."""
    items, clamped = gw._batch({"facts": [{"subject": f"e{i}"} for i in range(20)]}, ("facts",), 8)
    assert len(items) == 8
    assert clamped is True


def test_a_bare_string_element_is_lifted_rather_than_dropped() -> None:
    """A model that sends `["Dana"]` for a two-field shape has named a real surface. The
    schema asks for objects; dropping the element loses the entity outright."""
    items, _ = gw._batch({"entities": ["Dana", "  ", 7]}, ("entities",), 12)
    assert items == [{"surface": "Dana", "subject": "Dana"}]


def test_no_batch_key_reads_as_an_empty_batch_never_a_crash() -> None:
    assert gw._batch({"nonsense": 1}, ("facts",), 8) == ([], False)


def test_a_near_miss_field_name_still_carries_its_value() -> None:
    """Deliberate laxity in ONE direction: the schema names one key and the grammar
    fills it, but a synonym arriving as a silently dropped fact costs the owner a fact."""
    assert gw._text({"value": "412 Oak St"}, "object", "value") == "412 Oak St"
    assert gw._text({"object": 178}, "object", "value") == "178"
    assert gw._text({"object": "   "}, "object", "value") == ""


# --- temporal, values, kinds --------------------------------------------------


def test_precision_is_derived_from_the_iso_shape_never_asked_for() -> None:
    """TOOL_SURFACE gap 4: precision derives from the shape, so it is not a field the
    model can get wrong."""
    assert gw._precision("2026") == "year"
    assert gw._precision("2026-03") == "month"
    assert gw._precision("2026-03-14") == "day"
    assert gw._precision("2026-03-14T17:00:00") == "instant"


def test_a_bare_year_or_month_is_expanded_rather_than_rejected() -> None:
    """`fromisoformat` refuses both, and "she started there in 2019" is an ordinary
    thing for a note to say."""
    anchor = gw.datetime(2026, 3, 14, tzinfo=gw.UTC)
    assert gw._temporal("2026", anchor, None).resolved_start.year == 2026  # type: ignore[union-attr]
    month = gw._temporal("2026-03", anchor, None)
    assert (month.resolved_start.year, month.resolved_start.month) == (2026, 3)  # type: ignore[union-attr]
    assert month.precision == "month"


def test_a_bare_clock_time_is_read_in_the_notes_zone_not_utc() -> None:
    """A 5pm-local appointment pinned to 17:00Z is a fact wrong by the owner's offset
    forever — the same rule the note.extract path states in `extraction._naive_tz`."""
    anchor = gw.datetime(2026, 3, 14, tzinfo=gw.UTC)
    local = gw._temporal("2026-03-14T17:00:00", anchor, -420)
    assert local.resolved_start.utcoffset() == gw.timedelta(minutes=-420)  # type: ignore[union-attr]
    # A calendar date carries no clock, so it stays UTC.
    assert gw._temporal("2026-03-14", anchor, -420).resolved_start.utcoffset() == gw.timedelta(0)  # type: ignore[union-attr]


def test_a_date_that_is_not_a_date_raises_for_the_caller_to_report() -> None:
    """The caller records the fact UNDATED and says so in the result. A fact is not
    worth losing over its date."""
    try:
        gw._temporal("last spring", gw.datetime(2026, 3, 14, tzinfo=gw.UTC), None)
    except ValueError:
        return
    raise AssertionError("an unparseable date must not pass silently")


def test_a_quantity_literal_keeps_its_unit_apart_from_its_number() -> None:
    """`supersession.values_equal` converts between units, so 180 lb restated as 81.6 kg
    is a refresh rather than a fact_conflict — but only for the `{value, unit}` shape."""
    assert gw._quantity_value("178 lb") == {"value": 178, "unit": "lb"}
    assert gw._quantity_value("81.6kg") == {"value": 81.6, "unit": "kg"}
    assert gw._quantity_value("412 Oak Street") == {"value": "412 Oak Street"}


def test_an_edge_to_a_resolved_entity_is_a_relationship_whatever_else_it_looks_like() -> None:
    registry = gw.get_registry()
    assert gw._fact_kind(registry, "Person", "anything_at_all", object_present=True) == (
        "relationship"
    )
    # An undeclared (tier-2) predicate on a value falls back to the most cautious kind:
    # an attribute collision goes to review, it never auto-overwrites.
    assert gw._fact_kind(registry, "Nonexistent", "wibble", object_present=False) == "attribute"


def test_a_missing_statement_still_reads_as_a_sentence() -> None:
    """The statement is what the wiki and the review cards print."""
    assert gw._statement("Dana", "worksAt", "Everlane") == "Dana works at: Everlane"
    assert gw._statement("Me", "home_location", "412 Oak St") == "Me home location: 412 Oak St"


# --- the result shape ---------------------------------------------------------


def test_a_result_line_names_what_the_server_did_unasked() -> None:
    """The result shape IS the interface. `decide()` is deterministic and never
    model-facing, so these lines are the model's only window into it: a replaced value, a
    duplicate recognised, a clash parked."""
    replaced = FactWrite(
        gw.uuid.uuid4(),
        REPLACED,
        "general",
        "Me lives at 412 Oak St",
        replaced=("Me lives at 118 Pine Ave",),
    )
    line = gw._write_line(3, "Me", "homeLocation", "412 Oak St", replaced, [])
    assert line.startswith("ok  Me.homeLocation → 412 Oak St")
    assert "replaced Me lives at 118 Pine Ave, kept as history" in line

    already = FactWrite(gw.uuid.uuid4(), ALREADY, "general", "Kaiya is treated by Dr. Patel")
    assert "already recorded" in gw._write_line(1, "Kaiya", "treatedBy", "Dr. Patel", already, [])


def test_a_held_fact_says_it_is_not_live_and_names_decides_own_reason() -> None:
    """`held` is only what `decide()` returns as unresolvable — a fact_conflict, an
    attribute_collision, a pinned head. Never a confidence gate: those are gone under
    Lever A, and a line that implied one would teach the model to hedge."""
    held = FactWrite(
        gw.uuid.uuid4(),
        HELD,
        "health",
        "Me weighs 178 lb",
        hold_reason="fact_conflict",
        conflicting="Me weighs 182 lb",
    )
    line = gw._write_line(5, "Me", "bodyWeight", "178 lb", held, [])
    assert line.startswith("held  Me.bodyWeight → 178 lb")
    assert "fact_conflict" in line and "Me weighs 182 lb" in line
    assert "NOT live" in line and "Ask the owner" in line


def test_a_write_that_left_the_notes_domain_says_where_it_landed() -> None:
    """D3 wants each write's domain named IN WORDS, never colour alone — and this is
    also the model's only sight of the floor having fired."""
    floored = FactWrite(gw.uuid.uuid4(), WRITTEN, "health", "Dana is allergic to shellfish")
    assert "filed under health" in gw._write_line(0, "Dana", "allergy", "shellfish", floored, [])
    ordinary = FactWrite(gw.uuid.uuid4(), WRITTEN, "general", "Dana works at Everlane")
    assert "filed under" not in gw._write_line(0, "Dana", "worksAt", "Everlane", ordinary, [])
