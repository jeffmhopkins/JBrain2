"""A live, FULL-pipeline wiki demo against real Postgres — raw note text in, cited article
out, then a correction that rewrites it. Nothing is hand-seeded into the graph: the notes
are real text, and the REAL ingest + analysis pipeline (note.extract -> integrate.note ->
apply) builds the entity + facts, with the LLM router monkey-patched so *I* play both the
extractor (raw text -> structured facts) and the article writer (claims -> prose). Then an
owner-correction note force-supersedes a fact, the article is rebuilt, and the before/after
is printed.

    uv run pytest tests/integration/test_wiki_full_demo.py -s -q

It asserts the correction actually changed the persisted article (Portland -> Seattle) and
appended a new revision — but its point is the printed story.
"""

import json
from typing import Any

import pytest
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import SqlSettingsStore
from jbrain.wiki.builder import WikiBuilder
from jbrain.wiki.rewriter import LlmRewriter
from tests.conftest import docker_available

# Reuse the proven note/ingest helpers and the "me as the article writer" fake.
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import OWNER, database_url  # noqa: F401
from tests.integration.test_wiki_demo import ClaudeAsTheModel, FakeEmbed

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

EXTRACT_TASKS = {"note.extract": ("x", "m"), "integrate.note": ("x", "m")}


def _extract(facts: list[dict]) -> str:
    """The note.extract response — me, as the extractor, reading the note's prose."""
    return json.dumps(
        {
            "title": "Maya Okafor",
            "tags": ["doctor", "cardiology"],
            "mentions": [{"name": "Maya Okafor", "kind": "Person", "surface_text": "Maya Okafor"}],
            "facts": [
                {
                    "entity_ref": "Maya Okafor",
                    "predicate": f["predicate"],
                    "qualifier": "",
                    "kind": "attribute",
                    "statement": f["statement"],
                    "value_json": None,
                    "assertion": "asserted",
                    "object_entity_ref": None,
                    "domain": "general",
                    "temporal": None,
                }
                for f in facts
            ],
            "temporal_tokens": [],
        }
    )


def _intent(resolution: dict, facts: list[dict]) -> str:
    """The integrate.note response — me, as the integrator, resolving the mention and
    committing each surface-attested fact."""
    return json.dumps(
        {
            "resolutions": [resolution],
            "facts": [
                {
                    "entity_ref": "Maya Okafor",
                    "predicate": f["predicate"],
                    "qualifier": "",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": f["statement"],
                    "self_confidence": 0.95,
                    "chunk_id": "x",
                    "surface": f["surface"],  # a substring of the note body → committed
                }
                for f in facts
            ],
        }
    )


def _pipeline(maker: Any, extract: str, intent: str) -> AnalysisPipeline:  # noqa: F811
    fake = FakeLlmClient(responses=[extract, intent])
    return AnalysisPipeline(maker, LlmRouter({"x": fake}, EXTRACT_TASKS))


def _builder(maker: Any) -> WikiBuilder:  # noqa: F811
    clients: dict[str, Any] = {"x": ClaudeAsTheModel()}
    router = LlmRouter(clients, {"wiki.rewrite": ("x", "m"), "wiki.ground": ("x", "m")})
    rewriter = LlmRewriter(router, settings=SqlSettingsStore(maker), ctx=SYSTEM_CTX)
    return WikiBuilder(maker, embed=FakeEmbed(), rewriter=rewriter, embedding_model="demo-embed")


async def _entity_id(maker: Any) -> str:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text("SELECT id FROM app.entities WHERE canonical_name = 'Maya Okafor'")
                )
            ).scalar_one()
        )


async def _read_article(maker: Any) -> tuple[str, list[str], list[str]]:  # noqa: F811
    """(lead, section bodies, active fact statements) — what the reader would see."""
    async with scoped_session(maker, OWNER) as s:
        art = (
            await s.execute(
                text(
                    "SELECT id, lead_summary FROM app.wiki_articles WHERE entity_ref ="
                    " (SELECT id FROM app.entities WHERE canonical_name = 'Maya Okafor')"
                )
            )
        ).first()
        bodies = list(
            (
                await s.execute(
                    text(
                        "SELECT coalesce(r.body, '') FROM app.wiki_sections s"
                        " LEFT JOIN app.wiki_revisions r ON r.id = s.current_revision_id"
                        " WHERE s.article_id = :a ORDER BY s.seq"
                    ),
                    {"a": art.id if art else None},
                )
            ).scalars()
        )
        facts = list(
            (
                await s.execute(
                    text(
                        "SELECT statement FROM app.facts WHERE status = 'active' AND entity_id ="
                        " (SELECT id FROM app.entities WHERE canonical_name = 'Maya Okafor')"
                        " ORDER BY predicate"
                    )
                )
            ).scalars()
        )
    return (art.lead_summary if art else ""), bodies, facts


async def test_full_pipeline_with_correction(maker: Any, tmp_path: Any) -> None:  # noqa: F811
    out: list[str] = []

    # 1) A real note, ingested and run through the REAL extract->integrate->apply pipeline.
    note1 = await make_note(
        maker,
        domain="general",
        body=(
            "Maya Okafor is a cardiologist. She founded the Riverside Heart Clinic in 2019."
            " Maya lives in Portland, Oregon. She earned her medical degree at Johns Hopkins"
            " University."
        ),
    )
    await ingest(maker, note1, tmp_path)
    facts1 = [
        {
            "predicate": "occupation",
            "statement": "Maya Okafor is a cardiologist.",
            "surface": "cardiologist",
        },
        {
            "predicate": "founder",
            "statement": "Maya Okafor founded the Riverside Heart Clinic.",
            "surface": "Riverside Heart Clinic",
        },
        {
            "predicate": "residence",
            "statement": "Maya Okafor lives in Portland, Oregon.",
            "surface": "Portland",
        },
        {
            "predicate": "education",
            "statement": "Maya Okafor studied medicine at Johns Hopkins.",
            "surface": "Johns Hopkins",
        },
    ]
    resolution = {
        "mention_ref": "Maya Okafor",
        "mode": "new",
        "new_kind": "Person",
        "new_name": "Maya Okafor",
    }
    await _pipeline(maker, _extract(facts1), _intent(resolution, facts1)).integrate_note(
        {"note_id": note1}
    )

    # 2) Build the article (me as the writer).
    await _builder(maker).refresh()
    lead, bodies, _ = await _read_article(maker)
    out += ["", "=" * 72, "  v1 — built from the note", "=" * 72, f"  {lead}"]
    out += [f"  {b}" for b in bodies]

    # 3) The owner files a CORRECTION note — Maya moved. It out-argues the graph.
    eid = await _entity_id(maker)
    note2 = await make_note(
        maker, domain="general", body="Correction: Maya Okafor now lives in Seattle, Washington."
    )
    await ingest(maker, note2, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text("UPDATE app.notes SET provenance = 'owner_correction' WHERE id = :n"),
            {"n": note2},
        )
    corr = [
        {
            "predicate": "residence",
            "statement": "Maya Okafor lives in Seattle, Washington.",
            "surface": "Seattle",
        }
    ]
    existing = {
        "mention_ref": "Maya Okafor",
        "mode": "existing",
        "entity_id": eid,
        "attested_span": {"chunk_id": "x", "surface": "Maya Okafor"},
    }
    await _pipeline(maker, _extract(corr), _intent(existing, corr)).integrate_note(
        {"note_id": note2}
    )

    # 4) Rebuild — the superseded Portland fact is gone; Seattle is the active head.
    art_id = await _read_article_id(maker)
    await _builder(maker).rebuild(art_id)
    lead2, bodies2, active = await _read_article(maker)
    out += [
        "",
        "=" * 72,
        "  v2 — after the owner correction (note out-argues the wiki)",
        "=" * 72,
        f"  {lead2}",
    ]
    out += [f"  {b}" for b in bodies2]
    out += [
        "",
        "  active residence fact now: " + next(f for f in active if "lives in" in f),
        "=" * 72,
    ]
    print("\n".join(out))

    joined1 = " ".join(bodies)
    joined2 = " ".join(bodies2)
    assert "Portland" in joined1 and "Seattle" not in joined1  # v1 said Portland
    assert "Seattle" in joined2 and "Portland" not in joined2  # the correction rewrote it
    # a new revision was appended for the rebuilt section (history preserved)
    async with scoped_session(maker, OWNER) as s:
        revs = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_revisions r JOIN app.wiki_sections s"
                    " ON s.id = r.section_id WHERE s.article_id = :a"
                ),
                {"a": art_id},
            )
        ).scalar()
    assert (revs or 0) >= 2


async def _read_article_id(maker: Any) -> str:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text(
                        "SELECT id FROM app.wiki_articles WHERE entity_ref ="
                        " (SELECT id FROM app.entities WHERE canonical_name = 'Maya Okafor')"
                    )
                )
            ).scalar_one()
        )
