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
  the prompt (plan constraint 9): the allowlist, `NEVER_DEFAULT`, and which registries
  bind these two — the worker's per-note one and, for the reply turn, the chat one;
- the argument reading, because a batch is where a well-formed call quietly becomes a
  dropped fact.
"""

import json
from pathlib import Path

from jbrain.agent import graphwritetools as gw
from jbrain.agent.agents import AGENTS, NOTE_INGEST_UNATTENDED_TOOLS, agent_for
from jbrain.agent.asktools import ASK_OWNER_TOOL
from jbrain.agent.readtools import NOTE_GRAPH_TOOLS
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
    ever has a value.

    v3's two additions are required on the same terms. `when_end` carries the explicit
    empty-string escape `when` does; `confidence` is a NUMBER, and its type is the point
    — `evals/shape_probe.py` measured the string spelling coming back "high"/"low" every
    time, and a JSON type is the only closed vocabulary a tool grammar can enforce
    without an `enum`. What the model cannot be trusted with is WHEN to fill either one,
    which is `_close_interval`'s three refusals and `_self_report`'s min, not the
    schema's job."""
    item = _spec("assert_fact").params["properties"]["facts"]["items"]
    assert set(item["required"]) == {
        "subject",
        "predicate",
        "object",
        "statement",
        "when",
        "when_end",
        "quote",
        "confidence",
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
    assert gw.GRAPH_WRITE_TOOLS | NOTE_READ_TOOLS | {ASK_OWNER_TOOL} == NOTE_INGEST_UNATTENDED_TOOLS
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
