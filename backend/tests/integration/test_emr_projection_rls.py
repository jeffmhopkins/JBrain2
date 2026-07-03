"""RLS isolation for the EMR projection tables (migrations 0116/0117) and the
CHECK-widening safety of 0115/0118, against real Postgres (CLAUDE.md rule 3,
EMR import §5). Every domain-scoped projection row is invisible without the
health scope, the WITH CHECK mirror blocks a cross-firewall write, and a sidecar
row obeys the firewall via its own domain_code (the EXISTS-join denial). 0115
must keep admitting `video_analysis` (not narrow to the 0079 set); 0118 must
admit `shape_mismatch` alongside all twelve prior kinds.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_analysis_rls import (
    GENERAL_ONLY,
    HEALTH_ONLY,
    seed_health_graph,
)
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed_projections(maker: async_sessionmaker) -> dict[str, str]:
    """Seed the analysis graph + one row in each EMR projection table (health)."""
    ids = await seed_health_graph(maker)
    ids["encounter_entity"] = str(uuid.uuid4())
    ids["lab"] = str(uuid.uuid4())
    ids["provider_row"] = str(uuid.uuid4())
    ids["diagnosis_row"] = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'encounter', 'MICU stay 2026-01', 'health')"
            ),
            {"id": ids["encounter_entity"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.lab_results"
                " (id, entity_id, analyte, collected_at, source_note_id, source_fact_id,"
                "  domain_code) VALUES (:id, :eid, 'Platelet count', '2026-02-01T06:14:00Z',"
                "  :nid, :fid, 'health')"
            ),
            {"id": ids["lab"], "eid": ids["entity"], "nid": ids["note"], "fid": ids["fact"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.encounters (entity_id, class, source_note_id, domain_code)"
                " VALUES (:eid, 'inpatient', :nid, 'health')"
            ),
            {"eid": ids["encounter_entity"], "nid": ids["note"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.encounter_providers"
                " (id, encounter_id, provider_id, provider_name, role, domain_code)"
                " VALUES (:id, :enc, :pid, 'Chen, Sarah MD', 'attending', 'health')"
            ),
            {"id": ids["provider_row"], "enc": ids["encounter_entity"], "pid": ids["entity"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.encounter_diagnoses"
                " (id, encounter_id, condition_id, icd10, label, domain_code)"
                " VALUES (:id, :enc, :cid, 'D69.6', 'Thrombocytopenia', 'health')"
            ),
            {"id": ids["diagnosis_row"], "enc": ids["encounter_entity"], "cid": ids["entity_b"]},
        )
    return ids


async def count_visible(
    maker: async_sessionmaker, ctx: SessionContext, table: str, id_col: str, row_id: str
) -> int:
    async with scoped_session(maker, ctx) as s:
        result = await s.execute(
            text(f"SELECT count(*) FROM app.{table} WHERE {id_col} = :id"), {"id": row_id}
        )
        return result.scalar_one()


@pytest.mark.parametrize(
    ("table", "id_col", "id_key"),
    [
        ("lab_results", "id", "lab"),
        ("encounters", "entity_id", "encounter_entity"),
        ("encounter_providers", "id", "provider_row"),
        ("encounter_diagnoses", "id", "diagnosis_row"),
    ],
)
async def test_projection_tables_enforce_domain_firewall(
    maker: async_sessionmaker, table: str, id_col: str, id_key: str
) -> None:
    ids = await seed_projections(maker)
    assert await count_visible(maker, HEALTH_ONLY, table, id_col, ids[id_key]) == 1
    assert await count_visible(maker, OWNER, table, id_col, ids[id_key]) == 1
    assert await count_visible(maker, GENERAL_ONLY, table, id_col, ids[id_key]) == 0
    assert await count_visible(maker, UNSCOPED, table, id_col, ids[id_key]) == 0


async def test_sidecar_row_invisible_when_parent_encounter_out_of_scope(
    maker: async_sessionmaker,
) -> None:
    """A provider/diagnosis sidecar carries its own health domain_code, so a
    general-only scope reading through the encounter join sees zero (§5.5)."""
    ids = await seed_projections(maker)
    async with scoped_session(maker, GENERAL_ONLY) as s:
        joined = await s.execute(
            text(
                "SELECT count(*) FROM app.encounter_providers p"
                " JOIN app.encounters e ON e.entity_id = p.encounter_id"
                " WHERE p.id = :id"
            ),
            {"id": ids["provider_row"]},
        )
        assert joined.scalar_one() == 0


async def test_with_check_blocks_cross_firewall_projection_write(
    maker: async_sessionmaker,
) -> None:
    ids = await seed_projections(maker)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.lab_results"
                    " (entity_id, analyte, collected_at, source_note_id, source_fact_id,"
                    "  domain_code) VALUES (:eid, 'Sneaky', now(), :nid, :fid, 'health')"
                ),
                {"eid": ids["entity"], "nid": ids["note"], "fid": ids["fact"]},
            )


async def test_current_draw_partial_unique_index_enforced(maker: async_sessionmaker) -> None:
    """Exactly one CURRENT reading per (entity, collected_at, specimen)."""
    ids = await seed_projections(maker)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with scoped_session(maker, OWNER) as s:
            # Same draw, a DIFFERENT source_fact (so the full UNIQUE key differs) but
            # both current -> the partial unique index rejects the second current row.
            await s.execute(
                text(
                    "INSERT INTO app.lab_results"
                    " (entity_id, analyte, collected_at, source_note_id, source_fact_id,"
                    "  is_current, domain_code) VALUES (:eid, 'Platelet count',"
                    "  '2026-02-01T06:14:00Z', :nid, :fid, true, 'health')"
                ),
                {"eid": ids["entity"], "nid": ids["note"], "fid": ids["entity_b"]},
            )


# --- CHECK-widening safety (0115 / 0118) ---------------------------------


@pytest.mark.parametrize("kind", ["ocr", "caption", "transcript", "video_analysis", "emr_parse"])
async def test_attachment_extract_kind_admits_emr_parse_and_keeps_video(
    maker: async_sessionmaker, kind: str
) -> None:
    """0115 widened the FOUR-value set with emr_parse — video_analysis must survive."""
    ids = await seed_health_graph(maker)
    att = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.attachments"
                " (id, note_id, domain_code, sha256, filename, media_type, size_bytes)"
                " VALUES (:id, :nid, 'health', :sha, 'records.pdf', 'application/pdf', 1024)"
            ),
            {"id": att, "nid": ids["note"], "sha": uuid.uuid4().hex},
        )
        await s.execute(
            text(
                "INSERT INTO app.attachment_extracts"
                " (id, attachment_id, kind, tool, text, domain_code)"
                " VALUES (gen_random_uuid(), :aid, :kind, 'fake:model', 'x', 'health')"
            ),
            {"aid": att, "kind": kind},
        )


@pytest.mark.parametrize(
    "kind",
    ["fact_conflict", "attribute_collision", "merge_proposal", "ambiguous_mention",
     "domain_promotion", "low_confidence", "split_proposal", "inverse_proposal",
     "extraction_truncated", "low_confidence_inference", "new_predicate", "confirm_entity",
     "shape_mismatch"],
)
async def test_review_item_kind_admits_shape_mismatch_and_all_prior(
    maker: async_sessionmaker, kind: str
) -> None:
    """0118 admits shape_mismatch without dropping any of the twelve prior kinds."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, domain_code)"
                " VALUES (gen_random_uuid(), :kind, 'health')"
            ),
            {"kind": kind},
        )
