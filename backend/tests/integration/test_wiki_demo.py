"""A live wiki-build demo: real notes -> real graph -> the REAL WikiBuilder against real
Postgres, with the LLM router monkey-patched to an adaptive fake that synthesizes the
article from the exact claims the builder hands it (so citations are correct by
construction). Run it to watch the pipeline turn notes into a cited article:

    uv run pytest tests/integration/test_wiki_demo.py -s -q

Nothing here is a unit assertion's worth fussing over — it PRINTS the article the
builder persisted (title, lead, per-domain sections, and each clause's citation back to
its note), to see the firewall + citation machinery work end to end.
"""

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import LlmResult, LlmUsage
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import SqlSettingsStore
from jbrain.wiki.builder import WikiBuilder
from jbrain.wiki.rewriter import LlmRewriter
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The subject and the notes captured about them, by domain. Each (domain, sentence) is a
# real note + chunk + fact in the graph — the structured knowledge "extraction" produced.
SUBJECT = "Maya Okafor"
NOTES: list[tuple[str, str]] = [
    ("general", "Maya Okafor is a cardiologist."),
    ("general", "She founded the Riverside Heart Clinic in 2019."),
    ("general", "Maya lives in Portland, Oregon."),
    ("general", "She earned her medical degree at Johns Hopkins University."),
    ("general", "Okafor chairs the regional cardiology society."),
    ("health", "Dr. Okafor specializes in the management of cardiac arrhythmias."),
    ("health", "She runs a weekly clinic for post-surgical cardiac patients."),
]

_HEADING = {"general": "Overview", "health": "Clinical practice", "finance": "Finances"}
_CLAIM_RE = re.compile(r"^\[(\d+)\] \(domain=([^)]+)\) (.*)$")


class ClaudeAsTheModel:
    """The monkey-patched LLM. On a `wiki.rewrite` call it reads the enumerated claims the
    builder handed it and writes the article — grouping claims into one section per domain
    and citing each claim by its id (so the citation firewall always passes). On a
    `wiki.ground` call it affirms every clause. This is me standing in for the model: the
    synthesis logic that turns grounded claims into an article."""

    async def complete(
        self, *, model: str, system: str, user_text: str, json_schema: dict | None = None, **_: Any
    ) -> LlmResult:
        props = (json_schema or {}).get("properties", {})
        if "verdicts" in props:  # the grounding pass — affirm each clause
            n = len(re.findall(r"^\[(\d+)\] ", user_text, re.MULTILINE))
            return self._json({"verdicts": [{"index": i, "supported": True} for i in range(n)]})
        # the rewrite pass — synthesize the article from the claims
        by_domain: dict[str, list[tuple[int, str]]] = {}
        lead = ""
        for line in user_text.splitlines():
            m = _CLAIM_RE.match(line.strip())
            if not m:
                continue
            idx, domain, statement = int(m.group(1)), m.group(2), m.group(3)
            by_domain.setdefault(domain, []).append((idx, statement))
            if not lead and domain == "general":
                lead = statement
        sections = [
            {
                "heading": _HEADING.get(domain, domain.title()),
                "domain": domain,
                "clauses": [{"text": stmt, "claim_ids": [i]} for i, stmt in claims],
            }
            for domain, claims in by_domain.items()
        ]
        return self._json({"lead_summary": lead or f"{SUBJECT} is a person.", "sections": sections})

    @staticmethod
    def _json(obj: dict) -> LlmResult:
        import json

        return LlmResult(text=json.dumps(obj), parsed=obj, usage=LlmUsage(1, 1))


class FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_subject(maker: async_sessionmaker) -> str:
    """One entity + a note/chunk/fact per captured sentence — the graph extraction built."""
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:i, 'Person', :n, 'general')"
            ),
            {"i": eid, "n": SUBJECT},
        )
        for domain, sentence in NOTES:
            note, chunk, fact = (str(uuid.uuid4()) for _ in range(3))
            await s.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (:i, :c, :d, :b)"
                ),
                {"i": note, "c": note[:12], "d": domain, "b": sentence},
            )
            await s.execute(
                text(
                    "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                    " VALUES (:i, :n, :d, 'paragraph', 0, :b)"
                ),
                {"i": chunk, "n": note, "d": domain, "b": sentence},
            )
            await s.execute(
                text(
                    "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                    " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                    " VALUES (:i, :e, 'p', 'state', :st, 'asserted', '2026-01-01T00:00:00Z',"
                    " :n, :c, 'demo', 'v1', :d)"
                ),
                {"i": fact, "e": eid, "st": sentence, "n": note, "c": chunk, "d": domain},
            )
    return eid


async def test_build_an_article_from_notes(maker: async_sessionmaker) -> None:
    eid = await _seed_subject(maker)

    router = LlmRouter(
        {"x": ClaudeAsTheModel()},  # type: ignore[dict-item]  # only complete() is exercised
        {"wiki.rewrite": ("x", "m"), "wiki.ground": ("x", "m")},
    )
    rewriter = LlmRewriter(router, settings=SqlSettingsStore(maker), ctx=SYSTEM_CTX)
    builder = WikiBuilder(maker, embed=FakeEmbed(), rewriter=rewriter, embedding_model="demo-embed")
    processed = await builder.refresh()  # the REAL dirty-bit build

    # --- read back what the builder actually persisted, and print it ---
    async with scoped_session(maker, OWNER) as s:
        art = (
            await s.execute(
                text(
                    "SELECT id, title, lead_summary, status FROM app.wiki_articles"
                    " WHERE entity_ref = :e"
                ),
                {"e": eid},
            )
        ).first()
        assert art is not None, "the builder produced no article"
        secs = (
            await s.execute(
                text(
                    "SELECT s.heading, s.domain_code, coalesce(r.body, '') AS body, r.id AS rev"
                    " FROM app.wiki_sections s"
                    " LEFT JOIN app.wiki_revisions r ON r.id = s.current_revision_id"
                    " WHERE s.article_id = :a ORDER BY s.seq"
                ),
                {"a": art.id},
            )
        ).all()
        lines = [
            "",
            "=" * 72,
            f"  ARTICLE: {art.title}    [{art.status}]",
            "=" * 72,
            f"  {art.lead_summary}",
            "",
        ]
        total_cites = 0
        for sec in secs:
            lines.append(f"  ## {sec.heading}   ({sec.domain_code})")
            for para in sec.body.split("\n"):
                if para.strip():
                    lines.append(f"     {para}")
            cites = (
                (
                    await s.execute(
                        text(
                            "SELECT n.body FROM app.wiki_citations c"
                            " JOIN app.notes n ON n.id = c.note_id"
                            " WHERE c.revision_id = :r ORDER BY c.id"
                        ),
                        {"r": sec.rev},
                    )
                )
                .scalars()
                .all()
            )
            total_cites += len(cites)
            for body in cites:
                lines.append(f"       ↳ cites note: “{body}”")
            lines.append("")
        lines.append(
            f"  entities processed: {processed}   sections: {len(secs)}   citations: {total_cites}"
        )
        lines.append("=" * 72)
    print("\n".join(lines))

    assert len(secs) >= 2  # an Overview (general) + a Clinical practice (health) section
    assert total_cites == len(NOTES)  # every clause cited its note, through the firewall
