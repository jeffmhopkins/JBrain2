"""Guards on the producer key the whole-note settle scopes by
(`jbrain.analysis.settle_owner`).

Neither is about a behaviour — they are about a mistake a future writer can make.
`settle_owners` decides who may retract a row, so a producer that never records its
claim is either silently swept by a job that never wrote it (the shipped bug this key
closes) or never swept at all.

The pipeline seams take `settle_owner` as a required keyword, so a producer that writes
through them and forgets is a pyright error. These cover the holes a type checker cannot
see: SQLAlchemy's declarative constructors take `**kw: Any`, so `Fact(...)` and
`EntityMention(...)` type-check happily with no stamp at all, and hand-written SQL is
invisible to it entirely.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from jbrain.agent.graphwritetools import NoteGraphWriter
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.settle_owner import ANALYZER, CONVERSATION, EMR, SETTLE_OWNERS
from jbrain.ingest.emr.integrate import EXTRACTOR as EMR_EXTRACTOR
from jbrain.models.analysis import ReviewItem

_SRC = Path(__file__).resolve().parents[2] / "src" / "jbrain"


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


#: Raw SQL. Case-insensitive and schema-agnostic because neither is load-bearing to
#: Postgres: `insert into facts` reaches the same table as `INSERT INTO app.facts`, and
#: a guard that only reads the shouty qualified spelling is a guard an author defeats by
#: typing in lower case.
_RAW_SQL = re.compile(r"INSERT\s+INTO\s+(?:\w+\.)?(?:facts|entity_mentions)\b", re.IGNORECASE)

#: The declarative constructor, `Fact(...)`. Built per-module rather than fixed, so an
#: import alias counts as the name it aliases — see `_row_class_names`.
#:
#: The negative lookbehinds keep `ExtractedFact(`, `IntentFact(`, `FactWrite(` and
#: friends — none of which touch the table — out, while `models.Fact(` stays in, and
#: skip the `class Fact(Base)` definition itself.
_CONSTRUCTED = r"(?<!class )(?<![A-Za-z_])(?:{names})\("

#: `Fact` or `EntityMention` renamed on import. This module already carries
#: `ExtractedFact` / `IntentFact` / `FactWrite`, so the name pressure that pushes an
#: author to `import Fact as FactRow` is real and present, not hypothetical.
_ALIAS = re.compile(r"\b(?:Fact|EntityMention)\s+as\s+([A-Za-z_]\w*)")

#: Core inserts and the ORM's bulk helpers. `pg_insert` is spelled out because the
#: lookbehind that stops `my_insert(` would otherwise stop it too, and `pg_insert(Fact)`
#: is a write.
_INSERTISH = re.compile(
    r"(?<![A-Za-z])(?:pg_)?insert\s*\(|(?<![A-Za-z])bulk_(?:insert_mappings|save_objects)\s*\("
)

#: `x.y.insert(` — the receiver is where `Fact.__table__.insert()` says which table it
#: means, since the call itself is empty.
_RECEIVER = re.compile(r"([A-Za-z_][\w.]*)\.\s*$")

#: A method chained onto the previous call's result: `.values(`, `.on_conflict_do_*(`.
_CHAINED = re.compile(r"\s*\.\s*\w+\s*\(")


def _row_class_names(source: str) -> set[str]:
    """`Fact` / `EntityMention` plus whatever this module renamed them to on import."""
    return {"Fact", "EntityMention", *_ALIAS.findall(source)}


def _call_text(source: str, open_paren: int) -> str:
    """The text of the call whose `(` is at `open_paren`, to its matching `)`.

    A window of N characters would be a guess — the widest `Fact(...)` in the pipeline
    runs past 900 — and guessing wrong here means the guard passes on an unstamped
    write, which is the one outcome it must never do."""
    depth = 0
    for i in range(open_paren, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren : i + 1]
    return source[open_paren:]


def _chain_text(source: str, open_paren: int) -> str:
    """`_call_text` plus every call chained onto its result.

    `Fact.__table__.insert().values(...)` carries the row in `.values(...)`; reading the
    matched `insert()` alone sees `()` and concludes nothing is wrong.
    """
    out = _call_text(source, open_paren)
    i = open_paren + len(out)
    while (link := _CHAINED.match(source, i)) is not None:
        nxt = _call_text(source, link.end() - 1)
        out += source[i : link.end() - 1] + nxt
        i = link.end() - 1 + len(nxt)
    return out


def test_no_production_writer_inserts_an_owned_row_without_stamping_it() -> None:
    """Every `app.facts` / `app.entity_mentions` row in `src/` is written through
    `AnalysisPipeline.commit_facts`, which requires the producer key — and every write
    site names `settle_owners` within sight of itself.

    The DB column carries `DEFAULT ARRAY['analyzer']` (migration 0196, and its docstring
    says why), so an unstamped write does not fail — it quietly joins the ANALYZER's
    claim and is retracted by a producer that never wrote it. That is the exact failure
    the key exists to stop, and pyright cannot see it: `Fact(...)` and
    `EntityMention(...)` are declarative constructors typed `**kw: Any`, so a missing
    keyword there is not a type error. This notices instead.

    **What it looks at**: raw SQL in any case and any schema; the declarative
    constructor under its own name, under a dotted path, or under an import alias; and
    `insert(...)` / `pg_insert(...)` / `Fact.__table__.insert()...` / the ORM's bulk
    helpers, read across the whole chained expression so a stamp in `.values(...)`
    counts.

    **What it still cannot see** — stated plainly, because a guard trusted past its
    reach is worse than one with a known edge:

    - A table name it cannot read as a literal: `text(f"INSERT INTO app.{table} …")`, or
      a statement assembled from fragments.
    - A row handed to a bulk helper from far away. `bulk_insert_mappings(Fact, rows)`
      DOES trip this (the call names the table and carries no stamp), but the guard is
      insisting the stamp be visible at the call — it cannot follow `rows` to check.
    - Correctness of the stamp. It reads presence of the string, not whether the
      producer named is the one doing the writing.
    - An unbalanced `(` inside a string literal, which makes the paren scan run PAST the
      call — an over-wide window errs toward passing, not failing.
    - Anything outside `src/jbrain`: migrations, and the ~50 raw fixture inserts in the
      integration tests that lean on the column default on purpose.
    """
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == "settle_owner.py":
            continue  # its docstring quotes the shapes this looks for
        source = path.read_text()
        names = _row_class_names(source)
        constructed = re.compile(_CONSTRUCTED.format(names="|".join(sorted(names))))
        named = re.compile(r"\b(?:" + "|".join(sorted(names)) + r")\b")
        for match in constructed.finditer(source):
            call = _call_text(source, source.index("(", match.start()))
            if "settle_owners" not in call:
                offenders.append(f"{path.relative_to(_SRC)}:{_line(source, match.start())}")
        for match in _INSERTISH.finditer(source):
            call = _chain_text(source, match.end() - 1)
            receiver = _RECEIVER.search(source, 0, match.start())
            # `list.insert(0, x)` and `insert(SomeOtherTable)` are not our business; the
            # table can be named in the call OR in the receiver `Fact.__table__.`.
            if not named.search(call + " " + (receiver.group(1) if receiver else "")):
                continue
            if "settle_owners" not in call:
                offenders.append(f"{path.relative_to(_SRC)}:{_line(source, match.start())}")
        for match in _RAW_SQL.finditer(source):
            # A statement written as adjacent string literals: 900 chars past the head
            # covers the widest one in the repo's style.
            if "settle_owners" not in source[match.start() : match.start() + 900]:
                offenders.append(f"{path.relative_to(_SRC)}:{_line(source, match.start())}")
    assert not offenders, (
        "a write to an owner-scoped table with no settle_owners stamp in sight: "
        f"{offenders}. Write through commit_facts, or stamp the row explicitly."
    )


def test_the_pipeline_seams_require_the_key_rather_than_defaulting_it() -> None:
    """`settle_owner` has no default on any settle-bearing seam.

    A default is what would let a new producer inherit someone else's sweep without
    saying so; pyright refuses the call instead. Asserted here so that a later
    "convenience" default has to argue with a test.
    """
    # `apply_intent` was the fourth until R4 deleted it with its only caller.
    for name in ("commit_facts", "commit_intent", "settle_note"):
        param = inspect.signature(getattr(AnalysisPipeline, name)).parameters["settle_owner"]
        assert param.default is inspect.Parameter.empty, name
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name


def test_the_conversations_two_extractors_are_one_producer() -> None:
    """`note_ingest` (unattended pass) and `note_ingest_reply` (the owner's reply turn)
    are two runs of ONE writer, so the key cannot be the extractor string.

    `NoteGraphWriter` is the only thing that writes either, and it stamps `conversation`
    whatever extractor it was built with — that is what makes the grouping structural
    rather than a naming convention someone has to remember."""
    default = inspect.signature(NoteGraphWriter.__init__).parameters["extractor"].default
    assert default == "note_ingest"
    source = inspect.getsource(NoteGraphWriter)
    assert source.count("settle_owner=CONVERSATION") == 2  # resolve_entity + assert_fact
    assert "settle_owner=self._extractor" not in source


def test_a_claim_is_joined_and_never_taken_over() -> None:
    """The in-place paths ADD this producer to the row's claim set; none of them
    replaces it.

    Sharp-edged enough to be worth a static check: `_claimed_by` is the one expression
    allowed to write `settle_owners` on a row that already exists, and it is
    remove-then-append, so re-running a producer cannot duplicate its own claim (which
    would make the emptiness test that triggers retraction unreachable).
    """
    source = inspect.getsource(AnalysisPipeline)
    claimed = source.count("self._claimed_by(settle_owner)")
    assert claimed == 4, claimed  # held refresh, close, refresh, inverse refresh
    assert "array_append" in inspect.getsource(AnalysisPipeline._claimed_by)
    assert "array_remove" in inspect.getsource(AnalysisPipeline._claimed_by)
    # The one legitimate overwrite: adoption re-homes a shadow onto THIS note, so the
    # foreign note's claims do not come with it.
    assert source.count('values["settle_owners"] = [settle_owner]') == 1


def test_the_vocabulary_is_closed_and_the_emr_extractor_is_not_the_analyzers() -> None:
    assert {ANALYZER, CONVERSATION, EMR} == SETTLE_OWNERS
    # `emr:deterministic` is `provider:model`-shaped, which is why classifying the
    # analyzer's bucket by the string's SHAPE was never an option.
    assert ":" in EMR_EXTRACTOR


#: The two review-card kinds the whole-note settle sweeps. Every other kind carries a
#: NULL filer on purpose (migration 0197) — nobody's settle may retire those.
_SWEPT_KINDS = ("ambiguous_mention", "extraction_truncated")

#: A destructive statement over the card table. Same case-insensitive, schema-agnostic
#: reading as `_RAW_SQL`, and for the same reason.
_CARD_SQL = re.compile(r"(?:DELETE\s+FROM|UPDATE)\s+(?:\w+\.)?review_items\b", re.IGNORECASE)


def test_no_card_filer_or_sweep_of_a_swept_kind_forgets_the_filer() -> None:
    """Every `ReviewItem(...)` of a swept kind names `settle_owner`, and every
    destructive statement that names a swept kind is scoped by it.

    The column is nullable with no default, so a forgotten stamp does not silently join
    the analyzer's claim the way an unstamped `Fact` does — it files a card no sweep can
    retire, which the owner then has to dismiss by hand. Milder than the fact case and
    still wrong, and pyright cannot see either: `ReviewItem(...)` is a declarative
    constructor typed `**kw: Any`, and the sweeps' predicates are hand-written SQL.

    The destructive half is the one that actually cost the owner something: an unscoped
    DELETE is how an EMR settle removed the analyzer's "the tail of your medical records
    was dropped" card on every run (`analysis/settle_owner.py`).

    Same known edges as the guard above: a kind assembled from a variable, a stamp it
    cannot follow out of the call, and the correctness of the producer named. One more
    of its own: an ORM `update(ReviewItem)` names no kind, so it is invisible here —
    `_sync_truncation_review`'s refresh is scoped by the SELECT that found the id, and
    that SELECT is what this reads.
    """
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == "settle_owner.py":
            continue  # its docstring quotes the shapes this looks for
        source = path.read_text()
        for match in re.finditer(r"(?<![A-Za-z_])ReviewItem\s*\(", source):
            call = _call_text(source, source.index("(", match.start()))
            if any(k in call for k in _SWEPT_KINDS) and "settle_owner" not in call:
                offenders.append(f"{path.relative_to(_SRC)}:{_line(source, match.start())}")
        for match in _CARD_SQL.finditer(source):
            # 200 chars, not the 900 the fact guard uses: these statements are short,
            # and a wide window here reads the NEXT statement's scoping as this one's —
            # the two truncation statements sit back to back.
            window = source[match.start() : match.start() + 200]
            if any(k in window for k in _SWEPT_KINDS) and "settle_owner" not in window:
                offenders.append(f"{path.relative_to(_SRC)}:{_line(source, match.start())}")
    assert not offenders, (
        "a swept review card written or deleted with no settle_owner in sight: "
        f"{offenders}. A card is retired by its FILER and nobody else."
    )


def test_the_card_seams_require_the_filer_and_take_it_singular() -> None:
    """`settle_owner` has no default on any card seam either, and it is a STRING.

    Singular is the design claim, not a shortcut: a fact states something about the
    world, so two producers land one row and the column has to be a claim set; a card
    states something about a READING, and a reading has one reader
    (`analysis/settle_owner.py`). Asserted here so a later "make it consistent with
    facts" edit has to argue with a test rather than quietly widen the model.
    """
    for name in (
        "_resolve_entities",
        "_file_ambiguous_review",
        "_sweep_stale_ambiguous",
        "_sync_truncation_review",
    ):
        param = inspect.signature(getattr(AnalysisPipeline, name)).parameters["settle_owner"]
        assert param.default is inspect.Parameter.empty, name
        assert param.annotation is str, name
    column = ReviewItem.__table__.c["settle_owner"]
    assert column.nullable is True
    assert column.server_default is None
