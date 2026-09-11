"""The three tools that write the graph, at the level that needs no database.

The write behaviour itself is `tests/integration/test_note_graph_write_pg.py` — it goes
through `commit_facts` and `decide()`, and faking those would test the fake. What is
here is everything that is decided BEFORE a row is written and everything the registry
enforces around it:

- the SIDECARS, because the schema is the reliability contract with the model: the batch
  shape that was measured, every load-bearing field `required` (TOOL_SURFACE R3), and no
  JSON-Schema `enum` anywhere in the union the persona is offered (plan constraint 8 —
  an enum in this persona's tool set segfaults gpt-oss's harmony grammar);
- the WIRING, because D8 and D16 are properties of which handlers are bound, never of
  the prompt (plan constraint 9): the allowlist, `NEVER_DEFAULT`, and which registries
  bind these two — the worker's per-note one and, for the reply turn, the chat one;
- the argument reading, because a batch is where a well-formed call quietly becomes a
  dropped fact.
"""

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, cast

import pytest

from jbrain.agent import graphwritetools as gw
from jbrain.agent.agents import AGENTS, NOTE_INGEST_UNATTENDED_TOOLS, agent_for
from jbrain.agent.asktools import ASK_OWNER_TOOL
from jbrain.agent.readtools import NOTE_GRAPH_TOOLS
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import NEVER_DEFAULT
from jbrain.analysis.converse import NOTE_READ_TOOLS
from jbrain.analysis.pipeline import ALREADY, HELD, REPLACED, STILL_HELD, WRITTEN, FactWrite

_TOOLS = Path(gw.__file__).parent / "tools"


def _spec(name: str):  # noqa: ANN202
    return load_tool(_TOOLS / f"{name}.tool").spec


# --- the sidecars -------------------------------------------------------------


def test_both_tools_take_the_measured_batch_shape() -> None:
    """W2 probed the live model rather than guessing: batched arrays-of-objects came back
    20/20 well-formed at 7.6 facts and 8.9 entities per turn, against 1.0 for the flat
    one-per-call fallback (TOOL_SURFACE cut 5). A regression to flat scalars would be
    eight round trips per note on a serial GPU with the owner waiting."""
    for name in ("assert_fact", "close_reading"):
        facts = _spec(name).params["properties"]["facts"]
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
    ever has a value.

    `when_end` is required on the same terms, carrying the explicit empty-string escape
    `when` does. What the model cannot be trusted with is WHEN to fill it, which is
    `_close_interval`'s three refusals, not the schema's job.

    The two sidecars now carry the SAME item, and that is R1b: `assert_fact` v4 dropped
    `confidence` to match the reading (§3.3, decided by R0 — 106 live runs on three
    illegible values produced exactly one silently-committed guess, and it came from the
    arm that HAS the field). The engine's half of the guard is untouched —
    `self_confidence` is still the span check — so what was deleted is a channel measured
    never to carry anything, and one whose cost rose under one channel: a spurious low
    number now parks a true fact behind a hold nobody but the agent will ever see.
    Neither carries a recurrence field of any spelling: 0 parseable RRULEs in 228 values
    across two spellings, so recurrence is read from the `quote` in the handler (§3.2)."""
    item = _spec("assert_fact").params["properties"]["facts"]["items"]
    assert set(item["required"]) == {
        "subject",
        "predicate",
        "object",
        "statement",
        "when",
        "when_end",
        "quote",
    }
    assert set(item["properties"]) == set(item["required"])

    surface = _spec("resolve_entity").params["properties"]["entities"]["items"]
    required = {"surface", "kind", "distinguish"}
    assert set(surface["required"]) == required == set(surface["properties"])

    reading = _spec("close_reading").params["properties"]["facts"]["items"]
    assert set(reading["required"]) == {
        "subject",
        "predicate",
        "object",
        "statement",
        "when",
        "when_end",
        "quote",
    }
    assert set(reading["properties"]) == set(reading["required"])
    assert set(_spec("close_reading").params["required"]) == {"title", "tags", "facts"}


def test_the_schemas_the_model_must_never_be_offered_a_domain_or_an_enum() -> None:
    """Two rules that are not cuttable under any pressure.

    No `domain` / `sensitive` / `inferred` / `supersedes` / `correction` field: the
    firewall red-team rule is that a per-fact domain never comes from the model
    (`arbiter.py:163-165`), and the other three are recomputed or owned by `decide()`.

    No JSON-Schema `enum` anywhere: the GBNF grammar is built over the WHOLE tool union
    offered that turn, so one enum in one sidecar is enough (plan constraint 8). The
    enumerated values live in the descriptions and are validated in the handler."""
    for name in ("assert_fact", "resolve_entity", "close_reading"):
        blob = json.dumps(_spec(name).params)
        assert '"enum"' not in blob, f"{name} carries an enum — the harmony grammar segfault"
        for banned in ("domain", "sensitive", "inferred", "supersedes", "correction", "note_id"):
            assert f'"{banned}"' not in blob, f"{name} offers a `{banned}` field"
    # And the two fields R0 measured OFF the reading, by name, so a later wave re-adding
    # either has to argue with the measurement rather than with a comment.
    # And the fields R0 measured OFF the write verbs, by name, so a later wave re-adding
    # any of them has to argue with the measurement rather than with a comment.
    # `confidence` is on BOTH lists since R1b took it off `assert_fact` as well.
    for name in ("close_reading", "assert_fact"):
        blob = json.dumps(_spec(name).params)
        for measured_off in ("repeats", "recurrence", "rrule", "confidence", "certainty"):
            assert f'"{measured_off}"' not in blob, f"{name} offers `{measured_off}`"


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


def test_every_kind_hint_is_a_registry_type() -> None:
    """The `Drug` bug, pinned by its VALUE rather than by key presence — which is all the
    coverage above asserts, and is why a hint spelled `Drug` (a type the registry does not
    declare) passed for as long as it did. A kind that matches no registered type makes
    `_fact_kind` fall through to `attribute` for every fact about that entity, silently.

    `Thing` is the one deliberate exception and is asserted as such: it is the fallback
    for a word nobody described, and its consequence — the cautious `attribute` — is the
    intended landing place, not an accident."""
    registry = gw.get_registry()
    unknown = {
        word: kind
        for word, kind in gw._KIND_HINTS.items()
        if kind != gw._DEFAULT_KIND and kind not in registry.by_kind
    }
    assert not unknown, f"kind hints naming no registered type: {unknown}"
    assert gw._KIND_HINTS["drug"] == "Medication"
    assert gw._DEFAULT_KIND not in registry.by_kind
    assert gw._fact_kind(registry, gw._DEFAULT_KIND, "wibble", object_present=False) == "attribute"


def test_the_registrys_route_to_a_measurement_is_nameable() -> None:
    """`Observation.default_fact_kind` is `measurement`, and it is the only default that
    is — so with no hint word for it, `kind: measurement` was unreachable for any
    undeclared predicate and every scenario asserting a reading time-series was
    structurally doomed. Named now, so the model can say it."""
    registry = gw.get_registry()
    assert gw._KIND_HINTS["observation"] == "Observation"
    assert gw._fact_kind(registry, "Observation", "wibble", object_present=False) == "measurement"
    # Every type the registry declares is nameable by at least one hint word, so no
    # declared behaviour is unreachable purely because nothing spells it.
    named = set(gw._KIND_HINTS.values())
    missing = {k for k in registry.by_kind if k[:1].isupper()} - named
    assert not missing, f"registry types no word reaches: {sorted(missing)}"


def test_all_three_tools_declare_themselves_writes() -> None:
    """`permission` is documentation rather than a gate here (`outcome_for` is never
    called by the loop), but the loop DOES read `mutating`/`side_effecting` to decide a
    turn mutated — which drives the reflexion critique — and the roster gate reads the
    class."""
    for name in ("assert_fact", "resolve_entity", "close_reading"):
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


def test_the_chat_registry_binds_them_for_the_reply_turn_and_no_one_else() -> None:
    """The chat registry USED to drop both sidecars, on the ground that a handler is
    bound to one note. That also made them unreachable on the reply turn D8 allowlists
    them for — an allowlisted name with no sidecar is never offered and cannot dispatch —
    which left `correct_fact`, whose empty-address path PINS, as a reply turn's only
    write verb.

    So they are bound (`replytools`, addressed through the conversation row, exactly as
    `ask_owner` is) and the locks that were doing the real work are asserted here: the
    curator's wildcard cannot absorb them, and no OTHER persona allowlists them. The
    binding itself is `test_agent_replytools.py`, next to the fakes it needs."""
    assert NOTE_GRAPH_TOOLS == gw.GRAPH_WRITE_TOOLS
    assert gw.GRAPH_WRITE_TOOLS <= NEVER_DEFAULT
    for name, profile in AGENTS.items():
        if name == "note_ingest":
            continue
        allowed = profile.tools or frozenset()
        assert not (gw.GRAPH_WRITE_TOOLS & (allowed | profile.extra_tools)), name


def test_the_allowlist_and_the_bound_registry_are_the_same_set() -> None:
    """A tool in the allowlist with no handler is a call that dies in dispatch; a bound
    handler outside it is one the model is never offered. Either way the persona's real
    surface is not the one anybody wrote down.

    Three sets make up the allowlist, and only two of them live here: the note-BOUND
    graph writes and the inherited reads. `ask_owner` is the third — it is admitted by
    the same allowlist but binds to no note (it finds its conversation through
    `ToolContext.agent_session_id`), so it is in neither set this module owns. Spelled
    out rather than folded into one of them: a sibling task adding a tool must show up
    as a change here, which is exactly how this assertion earned its keep."""
    profile = agent_for("note_ingest")
    assert profile.tools == NOTE_INGEST_UNATTENDED_TOOLS
    # `GRAPH_WRITE_TOOLS` is what this module IMPLEMENTS, and since R3 that is one verb
    # more than the unattended pass binds: `assert_fact` is reached only through the chat
    # registry's session-addressed copy, on the owner's reply turn, because a second fact
    # verb beside the closing reading would let a pass write a fact its own reading omits
    # and the settle's sweep then retract it.
    unattended_writes = gw.GRAPH_WRITE_TOOLS - {gw.ASSERT_FACT}
    assert unattended_writes | NOTE_READ_TOOLS | {ASK_OWNER_TOOL} == NOTE_INGEST_UNATTENDED_TOOLS
    # The three are disjoint — no tool is bound twice, by two different builders.
    assert not (gw.GRAPH_WRITE_TOOLS & NOTE_READ_TOOLS)
    assert ASK_OWNER_TOOL not in gw.GRAPH_WRITE_TOOLS | NOTE_READ_TOOLS


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
        assert banned not in NOTE_INGEST_UNATTENDED_TOOLS


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
    items, clamped = gw._batch({"entities": ["Dana", "  ", 7]}, ("entities",), 12)
    assert items == [{"surface": "Dana", "subject": "Dana"}]
    # The two elements that could NOT be lifted are a loss, and the clamp is what says
    # so — see below.
    assert clamped is True


def test_an_element_this_cannot_read_reports_as_a_clamp() -> None:
    """The silent loss the clamp signal CAN carry. `facts: [{…}, null, {…}]` used to
    report two facts recorded and no truncation, because the flag was computed AFTER the
    unreadable element had been dropped — so a reading claimed to be the whole note while
    missing a fact the model had written. The flag is now computed against what the model
    SENT.

    (The plan's O13 — the fact the model never writes at all — this cannot carry, and the
    populations really are disjoint: one is about elements that arrived.)"""
    items, clamped = gw._batch(
        {"facts": [{"subject": "e1"}, None, {"subject": "e2"}]}, ("facts",), 8
    )
    assert len(items) == 2
    assert clamped is True
    # A batch that arrived whole is still not truncated, or the flag says nothing.
    _clean, ok = gw._batch({"facts": [{"subject": "e1"}]}, ("facts",), 8)
    assert ok is False


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


def test_a_number_never_backtracks_a_unit_out_of_its_own_tail() -> None:
    """The shipped bug, and the two literals that distinguish the fix from the old
    `[^\\d\\s]` unit class — every other input behaved the same under both. "80.0" parsed
    as 80 + unit ".0", so a weight restated as "80.0" compared UNEQUAL to the same weight
    written "80" and filed a conflict; the ISO date "1986-03-19" parsed as 1986 + unit
    "-03-19", storing a birth date as a quantity."""
    assert gw._quantity_value("80.0") == {"value": 80.0}
    assert gw._quantity_value("1986-03-19") == {"value": "1986-03-19"}


def test_a_slashed_or_exponent_literal_is_not_a_quantity() -> None:
    """Same shape as the `.0` bug, one character further on: a `/` unit start let the
    number half eat a fraction or a slashed date. `120/80` is this module's own blood
    pressure example, and reading it as 120 mmHg-of-something is a clinical value
    silently halved. A bare exponent is the numeric twin."""
    assert gw._quantity_value("1/2") == {"value": "1/2"}
    assert gw._quantity_value("120/80") == {"value": "120/80"}
    assert gw._quantity_value("120/80 mmHg") == {"value": "120/80 mmHg"}
    assert gw._quantity_value("03/19/1986") == {"value": "03/19/1986"}
    assert gw._quantity_value("1e3") == {"value": "1e3"}
    # A unit that merely CONTAINS a slash still parses — it starts with a letter.
    assert gw._quantity_value("95 mg/dL") == {"value": 95, "unit": "mg/dL"}


def test_a_leading_zero_is_a_spelling_and_not_a_number() -> None:
    """`int("01234")` is 1234, so a zip code stored as a number comes back a digit
    short. Redundant leading zeros fall through to the verbatim `{value}` string."""
    assert gw._quantity_value("01234") == {"value": "01234"}
    assert gw._quantity_value("0") == {"value": 0}
    assert gw._quantity_value("0.5 mg") == {"value": 0.5, "unit": "mg"}


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


@pytest.mark.parametrize("reason", ["fact_conflict", "attribute_collision", "low_confidence"])
def test_a_held_fact_says_it_is_not_live_and_names_decides_own_reason(reason: str) -> None:
    """R1b, and the half of its acceptance matrix that does not need a database: EVERY
    `review_kind` `decide()` can emit reaches the model here, naming the reason and the
    statement it clashes with.

    The card that used to be filed beside this line is gone, so the line carries the
    obligation rather than advice: nothing else raises a held row, and a pass that leaves
    one standing has left an inert fact nobody will ever look at. `decide()` still owns
    what LANDS (constraint 5) — this changes only who is told."""
    held = FactWrite(
        gw.uuid.uuid4(),
        HELD,
        "health",
        "Me weighs 178 lb",
        hold_reason=reason,
        conflicting="Me weighs 182 lb",
    )
    line = gw._write_line(5, "Me", "bodyWeight", "178 lb", held, [])
    assert line.startswith("held  Me.bodyWeight → 178 lb")
    assert reason in line and "Me weighs 182 lb" in line
    assert "NOT live" in line
    # The obligation, in both of its arms: re-read the note, or ask.
    assert "nothing else will raise it" in line
    assert "Re-read the note" in line and "ask the owner" in line


def test_a_collision_that_held_the_other_side_too_says_so() -> None:
    """The one `decide()` branch that changes state the model never named: an attribute
    collision holds BOTH birthdays. A result that reported only the row it was handed
    would under-report the write — the agent would think the value on file was still
    live, and would not know it had just parked the owner's existing answer."""
    both = FactWrite(
        gw.uuid.uuid4(),
        HELD,
        "general",
        "Cleo was born November 12, 1985",
        hold_reason="attribute_collision",
        conflicting="Cleo was born March 3, 1990",
        also_held=("Cleo was born March 3, 1990",),
    )
    line = gw._write_line(0, "Cleo", "birthDate", "1985-11-12", both, [])
    assert "was held too, so neither is live" in line


def test_a_correction_that_landed_live_still_names_the_rows_it_parked() -> None:
    """`also_held` is rendered for EVERY outcome, not only HELD, and the case that makes
    that necessary is `correct_fact`.

    `decide()` sets `hold_ids` on two branches. The collision above holds the candidate
    too, so the report rides the HELD line. The CORRECTION branch does not: an owner
    correction on a single-head key inserts ACTIVE and PINNED while parking every
    `pending_review` head it out-argues, so the write lands `replaced`/`written` and the
    rows it moved would have dropped out of the result entirely — the exact
    under-reporting the field was added to stop, in the one case where the field is the
    only witness there is."""
    corrected = FactWrite(
        gw.uuid.uuid4(),
        REPLACED,
        "general",
        "Cleo was born July 9, 1988",
        replaced=("Cleo was born March 3, 1990",),
        also_held=("Cleo was born November 12, 1985",),
    )
    line = gw._write_line(0, "Cleo", "birthDate", "1988-07-09", corrected, [])
    assert line.startswith("ok  Cleo.birthDate → 1988-07-09")
    assert "replaced Cleo was born March 3, 1990, kept as history" in line
    assert "Cleo was born November 12, 1985 was held" in line
    # It LANDED, so it must not read as the collision's "neither is live".
    assert "neither is live" not in line
    assert "this value is the live one now" in line


def test_a_restatement_of_a_held_row_does_not_read_as_a_fresh_clash() -> None:
    """The refresh path's own line (`STILL_HELD`). It must say three things the generic
    HELD line does not: the row was already held, this write changed nothing, and
    re-reading the note will not settle it — the model has already done that, and the
    only move left belongs to the owner. And it must never render as `ok`."""
    again = FactWrite(
        gw.uuid.uuid4(),
        HELD,
        "general",
        "Cleo was born November 12, 1985",
        hold_reason=STILL_HELD,
        conflicting="Cleo was born March 3, 1990",
    )
    line = gw._write_line(0, "Cleo", "birthDate", "1985-11-12", again, [])
    assert line.startswith("held  Cleo.birthDate")
    assert "already recorded, and STILL NOT LIVE" in line
    assert "It still clashes with Cleo was born March 3, 1990." in line
    assert "Re-reading will not settle this; ask the owner which is right" in line
    # The generic hold's advice would send it round a loop it has already run.
    assert "Re-read the note" not in line


def test_a_supersede_that_landed_live_still_names_why_it_was_not_clean() -> None:
    """`decide()`'s one card site whose row goes ACTIVE: a same-instant supersede, or a
    preference. It filed a card that was pure notification of a thing that had already
    happened; under one channel it is a clause on the result and nothing else."""
    live = FactWrite(
        gw.uuid.uuid4(),
        REPLACED,
        "general",
        "Me lives at 412 Oak St",
        replaced=("Me lives at 118 Pine Ave",),
        hold_reason="fact_conflict",
    )
    line = gw._write_line(0, "Me", "homeLocation", "412 Oak St", live, [])
    assert line.startswith("ok  ")
    assert "replaced Me lives at 118 Pine Ave, kept as history" in line
    assert "not a clean update (fact_conflict)" in line
    # And a CLEAN supersede (Lever B) says nothing of the sort.
    clean = FactWrite(
        gw.uuid.uuid4(),
        REPLACED,
        "general",
        "Me lives at 412 Oak St",
        replaced=("Me lives at 118 Pine Ave",),
    )
    assert "not a clean" not in gw._write_line(0, "Me", "homeLocation", "412 Oak St", clean, [])


def test_a_reciprocal_refused_in_favour_of_a_primary_is_reported_on_its_source() -> None:
    """`_materialize_inverse`'s derived-defers-to-primary hold. The reciprocal is a row
    the model never asked for and cannot address, so it has no result line of its own —
    it rides the fact whose reciprocal it is."""
    write = FactWrite(
        gw.uuid.uuid4(),
        WRITTEN,
        "general",
        "Ada's spouse is Bo",
        reciprocal_held="Bo's spouse is Cy.",
    )
    line = gw._write_line(0, "Ada", "spouse", "Bo", write, [])
    assert "the reciprocal edge was recorded but NOT live" in line
    # The statement's own full stop is trimmed: it is embedded in this line's prose,
    # and no other clause on the line terminates either (`_trim_stop`).
    assert line.endswith("it clashes with Bo's spouse is Cy")


def test_a_write_that_left_the_notes_domain_says_where_it_landed() -> None:
    """D3 wants each write's domain named IN WORDS, never colour alone — and this is
    also the model's only sight of the floor having fired."""
    floored = FactWrite(gw.uuid.uuid4(), WRITTEN, "health", "Dana is allergic to shellfish")
    assert "filed under health" in gw._write_line(0, "Dana", "allergy", "shellfish", floored, [])
    ordinary = FactWrite(gw.uuid.uuid4(), WRITTEN, "general", "Dana works at Everlane")
    assert "filed under" not in gw._write_line(0, "Dana", "worksAt", "Everlane", ordinary, [])


# --- v3: the interval end (TOOL_SURFACE gap 4) --------------------------------


def test_an_interval_end_closes_the_temporal_it_is_given() -> None:
    """The whole of gap 4: a note that states a closed interval in ONE sentence no
    longer needs a later note to close it."""
    anchor = gw.datetime(2026, 3, 14, tzinfo=gw.UTC)
    temporal = gw._temporal("2019", anchor, None)
    closed, refused = gw._close_interval(temporal, "2023", None)
    assert refused is None
    assert closed is not None and closed.resolved_end is not None
    assert closed.resolved_start == temporal.resolved_start


def test_an_end_period_closes_when_the_period_ends_not_when_it_starts() -> None:
    """ "until 2023" ends when 2023 ends. `_temporal` expands a bare year to its FIRST
    day, which is right for a start and a whole period wrong for an end."""
    anchor = gw.datetime(2026, 3, 14, tzinfo=gw.UTC)
    year = gw._close_interval(gw._temporal("2019", anchor, None), "2023", None)[0]
    assert year is not None and year.resolved_end is not None
    assert (year.resolved_end.year, year.resolved_end.month) == (2023, 12)
    month = gw._close_interval(gw._temporal("2023-01", anchor, None), "2026-03", None)[0]
    assert month is not None and month.resolved_end is not None
    assert (month.resolved_end.year, month.resolved_end.month) == (2026, 3)
    # December has no month 13 to borrow from.
    december = gw._close_interval(gw._temporal("2023", anchor, None), "2025-12", None)[0]
    assert december is not None and december.resolved_end is not None
    assert (december.resolved_end.year, december.resolved_end.month) == (2025, 12)


def test_the_three_ends_the_measurement_says_the_model_invents_are_refused() -> None:
    """`evals/shape_probe.py` put `when_end` in front of the live model on a note with
    exactly one closed interval: it closed that one every time AND stamped an end on
    nearly every other fact, in three shapes. Each is refused here, and each refusal is
    a result line rather than a dropped fact — the fact commits with the start it had."""
    anchor = gw.datetime(2026, 3, 14, tzinfo=gw.UTC)
    started = gw._temporal("2019", anchor, None)

    # A phrase that is not a date at all ("present", "last week").
    same, refused = gw._close_interval(started, "present", None)
    assert same is started and refused is not None and "not a date" in refused

    # An end on a fact that never had a start.
    none_temporal, refused = gw._close_interval(None, "2023", None)
    assert none_temporal is None and refused is not None and "no `when`" in refused

    # Today's date on a fact the note dated today — the shape the model produced most
    # often. The comparison is period against period, so naming the same day, month or
    # year as the start is a restatement rather than a close.
    today = gw._temporal("2026-09-09", anchor, None)
    same, refused = gw._close_interval(today, "2026-09-09", None)
    assert same is today and refused is not None and "not after" in refused
    same, refused = gw._close_interval(started, "2019", None)
    assert same is started and refused is not None and "not after" in refused
    # A finer end INSIDE the start's period is the same restatement one level down.
    same, refused = gw._close_interval(started, "2019-06", None)
    assert same is started and refused is not None and "not after" in refused


def test_no_end_is_the_common_case_and_changes_nothing() -> None:
    """The empty-string escape: `when_end` is required so the grammar fills it, and
    empty is the answer for nearly every fact."""
    anchor = gw.datetime(2026, 3, 14, tzinfo=gw.UTC)
    started = gw._temporal("2019", anchor, None)
    assert gw._close_interval(started, "", None) == (started, None)
    assert gw._close_interval(None, "", None) == (None, None)


# --- v3: a declared value predicate never takes a name as an edge (gap 5) -----


def test_a_declared_value_predicate_takes_its_object_literally() -> None:
    """The Sammy bug. `object` is one string doing two jobs and the model writes a name
    for both, so a literal that happened to equal a resolved surface silently became an
    edge — an entity's own nickname became a self-edge, and the display projection then
    had no name fact to read. The registry already knows which predicates take an edge:
    `value_shape: ref`."""
    registry = gw.get_registry()
    assert not gw._takes_entity_object(registry, "Person", "name.nickname")
    assert not gw._takes_entity_object(registry, "Person", "name.full")
    # A drift spelling normalizes first, so the fix cannot be dodged by spelling.
    assert not gw._takes_entity_object(registry, "Person", "legalName")


def test_an_undeclared_predicate_keeps_the_permissive_link() -> None:
    """Tier-2 is most of the graph and the registry has no opinion there. Refusing to
    link an undeclared predicate's object would break far more edges than it fixed."""
    registry = gw.get_registry()
    assert gw._takes_entity_object(registry, "Person", "hangsOutWith")
    assert gw._takes_entity_object(registry, "Sasquatch", "name.nickname")


def test_a_handle_still_addresses_an_entity_under_a_value_predicate() -> None:
    """`lookup` checks the handle table first in every case: the model writing `e2` is
    an unambiguous statement that it means the entity, and the registry never overrides
    that."""
    writer = object.__new__(gw.NoteGraphWriter)
    writer._by_handle = {}
    writer._by_surface = {}
    handle = gw.Handle(
        handle="e1",
        entity=gw.ResolvedEntity(id=gw.uuid.uuid4(), subject_id=None),
        surface="Sammy",
        kind="Person",
        name="Celine Kitina Hopkins",
        domain="general",
        visible=True,
    )
    writer._remember(handle)
    assert writer.lookup("e1", by_name=False) is handle
    assert writer.lookup("Sammy", by_name=False) is None
    assert writer.lookup("Sammy", by_name=True) is handle


# --- the correction-note elevation --------------------------------------------
#
# W5's stated precondition. `PHASE6_WIKI_PLAN.md` §4 names `plan_intent(correction=True)`
# as the wiki correction loop's shipped exit criterion; its only bridge was
# `integrate_note` reading `provenance == 'owner_correction'`, which W5a deletes. The
# rule now lives on `NoteTarget`, and these are the parts of it that need no database.


def _target(provenance: str) -> gw.NoteTarget:
    return gw.NoteTarget(
        note_id=gw.uuid.uuid4(),
        domain="general",
        captured_at=gw.datetime(2026, 9, 9, tzinfo=gw.UTC),
        provenance=provenance,
    )


def test_only_an_owner_correction_note_is_a_correction() -> None:
    """The discriminator is the provenance the SERVER stamped, and the closed set of
    spellings matters: `human` is the ordinary capture, `agent` a Proposal enactment,
    `untrusted_origin` a stranger's approved intake body. None of them may force-supersede
    and pin, and a note conversation is opened over all four."""
    assert _target("owner_correction").is_correction is True
    for ordinary in ("human", "agent", "untrusted_origin", "", "OWNER_CORRECTION"):
        assert _target(ordinary).is_correction is False


def test_a_note_is_not_a_correction_unless_it_was_said_to_be() -> None:
    """The default is the safe one. Every `NoteTarget` built without the field — a test,
    a future caller, a path that forgets — is an ordinary note, so the elevation can only
    ever be reached by a caller that read a real note row."""
    bare = gw.NoteTarget(
        note_id=gw.uuid.uuid4(),
        domain="general",
        captured_at=gw.datetime(2026, 9, 9, tzinfo=gw.UTC),
    )
    assert bare.provenance == "human"
    assert bare.is_correction is False


def test_a_stranger_authored_note_can_never_be_a_correction() -> None:
    """The two provenance branches the conversation now takes must not overlap: the body
    a stranger wrote narrows the surface (D10), and the owner's correction widens what a
    write means. `is_third_party` and `is_correction` are answers to the same field, and
    a note that satisfied both would be stranger text force-superseding the graph."""
    from jbrain.analysis.thirdparty import is_third_party

    for provenance in ("human", "agent", "owner_correction", "untrusted_origin", "anything"):
        target = _target(provenance)
        assert not (target.is_correction and is_third_party(provenance))


def test_the_capture_api_cannot_mint_a_correction_note() -> None:
    """The whole safety argument for elevating on a pass the owner is not present for is
    that nothing but an owner-gated server path can set this provenance. `POST /api/notes`
    is the one endpoint an offline client (and, through it, anything that can reach the
    PWA's own capture route) drives, and its request model carries no `provenance` field
    at all — so there is no value to send."""
    from jbrain.api.notes import CreateNoteRequest

    assert "provenance" not in CreateNoteRequest.model_fields


def test_the_sidecar_gained_nothing_the_model_can_set() -> None:
    """Constraint 5, restated as a schema property: the elevation is server-side, so
    `assert_fact` gained nothing the model could claim in order to buy a force-supersede.
    `correct_fact` stays the only place a model asks for one, and it asks by being
    CALLED, not by a field. (The version does move — R1b took `confidence` OFF the item —
    but every version of it has been a strict subset of this list.)"""
    spec = _spec("assert_fact")
    item = spec.params["properties"]["facts"]["items"]
    for forbidden in ("correction", "provenance", "pinned", "domain"):
        assert forbidden not in item["properties"]


# --- the reading (R1 of AGENT_INGEST_REWRITE) ---------------------------------


def test_a_reading_unions_its_calls_and_keeps_the_first_title() -> None:
    """§3.1: "several calls are allowed and are unioned". A long note takes more than one
    call of eight facts, and what the settle will eventually ask is what the note says
    NOW — the union, not the last call.

    The title is the FIRST non-empty one on purpose: a continuation call is the one most
    likely to restate it loosely or blank it, and the call that read the note from the top
    is the one that named it."""
    reading = gw.Reading()
    reading.union(title="Coffee with Dana", tags=["dana"], fact_ids=["f1", "f2"], clamped=True)
    reading.union(
        title="More about Dana", tags=["dana", "work"], fact_ids=["f2", "f3"], clamped=False
    )
    assert reading.title == "Coffee with Dana"
    assert reading.tags == ("dana", "work")
    # Order-preserving and deduplicated: this becomes `sweep_note(touched=…)`.
    assert reading.fact_ids == ("f1", "f2", "f3")
    assert reading.calls == 2
    # And the clamp LATCHES. A reading whose first call was truncated is a prefix of the
    # note however clean the rest of the pass looks, which is the whole reason the settle's
    # gate will read this field rather than the last call's result.
    assert reading.clamped is True


def test_tags_are_normalized_deduplicated_and_capped() -> None:
    """`tagconsolidate` normalizes tag drift ACROSS notes; this is the same tag twice in
    one call, which no later pass would ever reconcile. A non-string element is dropped
    rather than stringified — `3` is not a tag."""
    assert gw._tags({"tags": ["Dana", " dana ", "Work", 3, None, "work"]}) == ("dana", "work")
    assert gw._tags({"tags": "solo"}) == ("solo",)
    assert gw._tags({"tags": [f"t{i}" for i in range(20)]}) == tuple(f"t{i}" for i in range(8))
    assert gw._tags({}) == ()


def _candidate(
    name: str, kind: str = "Person", summary: str = "", domain: str = "general"
) -> gw.Candidate:
    import uuid as _uuid

    return gw.Candidate(
        id=_uuid.uuid4(), subject_id=None, name=name, kind=kind, summary=summary, domain=domain
    )


def test_distinguish_narrows_to_one_candidate_or_refuses() -> None:
    """§3.4's matcher, and the experiment that section named as a unit test rather than a
    probe. It runs over exactly what `_file_ambiguous_review` put on the card the result
    is replacing — name, kind, summary — because that is all the agent is shown and all
    it can answer from.

    It refuses in both directions that matter, and the tie is the sharp one: two
    candidates that fit equally well are the ambiguity restated, and picking one is how a
    fact lands on the wrong person for good."""
    dana_w = _candidate("Dana Whitfield", summary="staff engineer at Everlane")
    dana_r = _candidate("Dana Reyes", summary="cardiologist in Boulder")
    both = [dana_w, dana_r]

    assert gw._distinguish(both, "Dana Whitfield") is dana_w
    assert gw._distinguish(both, "the one in Boulder") is dana_r
    assert gw._distinguish(both, "her cardiologist") is dana_r
    # Nothing in the phrase separates them.
    assert gw._distinguish(both, "the one from the note") is None
    # A word both candidates carry is a tie, not a winner.
    assert gw._distinguish(both, "Dana") is None
    # Stopwords alone say nothing, and an empty phrase is never a choice.
    assert gw._distinguish(both, "the one") is None
    assert gw._distinguish(both, "") is None
    assert gw._distinguish([], "Dana Whitfield") is None


_GENERAL = frozenset({"general"})


def test_the_ambiguity_result_names_the_candidates() -> None:
    """The card named them and the tool result did not (§2, `ambiguous_mention`), so the
    agent was refused an answer it was never given the means to give. The names, kinds and
    summaries are what `distinguish` is matched against, so the result has to show all
    three."""
    line = gw._candidate_note(
        [
            _candidate("Dana Whitfield", summary="staff engineer at Everlane"),
            _candidate("Dana Reyes", summary="cardiologist in Boulder"),
        ],
        _GENERAL,
    )
    assert "Dana Whitfield (Person, staff engineer at Everlane)" in line
    assert "Dana Reyes (Person, cardiologist in Boulder)" in line
    # Bounded: past a handful the list stops being a question anyone can answer.
    many = gw._candidate_note([_candidate(f"Dana {i}") for i in range(9)], _GENERAL)
    assert many.count(";") == gw.MAX_CANDIDATES
    assert f"{9 - gw.MAX_CANDIDATES} more" in many
    assert gw._candidate_note([], _GENERAL) == ""


def test_a_candidate_outside_the_conversations_scopes_is_counted_never_named() -> None:
    """The branch `Handle.visible` does not cover, and it renders exactly what that check
    exists to withhold: an ambiguity result printing "Dr. Anjali Renwick (Person,
    oncologist at Kaiser)" discloses through the FAILED resolution what the successful one
    is careful never to say.

    Counted rather than dropped, because the count is not the disclosure: the same surface
    resolving cleanly already tells the thread a handle is "already known" without saying
    to what, and the agent needs to know `distinguish` has something to choose from."""
    line = gw._candidate_note(
        [
            _candidate("Dana Whitfield", summary="staff engineer at Everlane"),
            _candidate("Dr. Anjali Renwick", summary="oncologist at Kaiser", domain="health"),
        ],
        _GENERAL,
    )
    assert "Dana Whitfield (Person, staff engineer at Everlane)" in line
    assert "Anjali" not in line and "oncologist" not in line
    assert "1 in a domain this note cannot see" in line
    # And when EVERY candidate is out of scope, the COUNT still comes back: it says
    # nothing about which domain or whose row, and it is the difference between "ask the
    # owner, I cannot see them" and a bare refusal the agent would try to answer.
    both_hidden = gw._candidate_note(
        [_candidate("Renwick", domain="health"), _candidate("Renwick", domain="health")],
        _GENERAL,
    )
    assert both_hidden == " It could be: 2 in a domain this note cannot see."


# --- the elevation's safety gate ---------------------------------------------


def test_the_elevation_gate_defaults_to_the_safe_direction() -> None:
    """R3's fourth review, finding 5. `words_reached_note` turns the correction-note
    ELEVATION on, and the elevation commits a row active + PINNED at confidence 1.0 —
    which `sweep_note` spares, no later note supersedes and no correction note reaches.

    It defaulted True, so the unsafe answer was the one a caller who never heard of the
    split got, and the unattended registry was relying on that default. The repo's
    convention for exactly this class is the opposite (`agents.narrow_for_third_party_note`:
    the SAFE answer is the default, precisely so forgetting cannot elevate). Pinned as a
    property of the SIGNATURES rather than of one call, because what makes the failure
    unrepresentable is that a new caller who says nothing cannot pin."""
    for fn in (gw.NoteGraphWriter.close_reading, gw.NoteGraphWriter._close_reading):
        param = inspect.signature(fn).parameters["words_reached_note"]
        assert param.default is False, f"{fn.__name__} elevates by default"
    # And the writer beneath them, which is where the flag actually reaches `decide()`.
    inner = inspect.signature(gw.NoteGraphWriter._assert_one).parameters["words_reached_note"]
    assert inner.default is False


def test_the_unattended_pass_says_its_words_are_the_note() -> None:
    """The one caller that legitimately elevates, and it now says so out loud rather than
    inheriting it. An unattended pass reads the note's own text — there is no owner turn
    behind it, so there are no words that could have failed to reach the note — which is
    what keeps the correction-note elevation sound on an `owner_correction` note."""
    seen: dict[str, Any] = {}

    class _Spy:
        async def close_reading(self, arguments: dict, ctx: object, **kw: object) -> str:
            seen.update(kw)
            return "ok"

        async def resolve_entity(self, arguments: dict, ctx: object) -> str:
            return "ok"

    handlers = gw.NoteToolset(writer=cast(gw.NoteGraphWriter, _Spy())).handlers()

    async def call() -> None:
        await handlers[gw.CLOSE_READING]({}, None)

    asyncio.run(call())
    assert seen == {"words_reached_note": True}


def test_a_parameter_correction_cannot_pin_past_the_same_gate() -> None:
    """R3's fourth review, finding 10. `correction` reaches `decide()`'s pinning branch
    two ways — as a PARAMETER (`correct_fact`) and by the elevation — and the flag used to
    govern only the second. A caller passing `correction=True` with `words_reached_note`
    left at its default was told "NOT as a pinned correction" by the elevation's own
    else-arm and pinned regardless.

    Unreachable today: the one parameter-caller passes True and is gated in `replytools`
    by the empty-address arm instead. Pinned because the next caller into this path is
    R3f/R4's, and a downgrade to the ordinary capped path is what a caller who says
    nothing should get."""
    src = inspect.getsource(gw.NoteGraphWriter._assert_one)
    assert "if correction and not words_reached_note:" in src
    assert src.index("if correction and not words_reached_note:") < src.index(
        "attested, signals = True, _ATTESTED"
    ), "the downgrade must come before the branch that trusts the flag"
