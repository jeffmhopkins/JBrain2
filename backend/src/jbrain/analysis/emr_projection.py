"""Project EMR graph entities into the typed lab/encounter read-models
(docs/plans/EMR_IMPORT_PLAN.md §4). Not sources of truth (#7) — a denormalized
view re-derived from ACTIVE (and, for corrected labs, superseded) facts after a
note's facts settle, inside the caller's RLS-scoped transaction, idempotent on
re-analysis. Mirrors `appointment_projection`.

`report_status` is DERIVED from each `value` fact's lifecycle + supersession
chain (§4.1), never a stored fact: a lone active reading is `final`; an active
head with a superseded predecessor at its qualifier is `corrected` (rendered as a
second `is_current=false` row for the predecessor); a `pending_review` reading is
`preliminary`; a retracted reading is dropped. Encounters materialize BEFORE
observations each call so a lab row's soft `encounter_id` resolves against a
present entity (§4).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.models.analysis import Entity, Fact

_EMR_KINDS = frozenset({"observation", "encounter"})
_ORDERING_ROLE = "ordering"


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v: Any) -> str | None:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        for k in ("value", "text", "label", "name", "code"):
            c = v.get(k)
            if isinstance(c, str) and c.strip():
                return c.strip()
    return None


async def project_emr(session: AsyncSession, entity_ids: set[uuid.UUID]) -> None:
    """Re-derive the lab/encounter projection for each EMR entity in the set.
    Non-EMR entities are filtered out by one up-front kind SELECT (the cost guard,
    §4), so passing the whole touched set costs one empty query on a non-EMR note."""
    if not entity_ids:
        return
    ents = (
        (
            await session.execute(
                select(Entity).where(
                    Entity.id.in_(entity_ids), func.lower(Entity.kind).in_(_EMR_KINDS)
                )
            )
        )
        .scalars()
        .all()
    )
    encounters = [e for e in ents if e.kind.lower() == "encounter"]
    observations = [e for e in ents if e.kind.lower() == "observation"]
    for ent in encounters:  # parents before children (§4)
        await _project_encounter(session, ent)
    for ent in observations:
        await _project_observation(session, ent)


async def _active_facts(session: AsyncSession, eid: uuid.UUID, predicate: str) -> list[Fact]:
    return list(
        (
            await session.execute(
                select(Fact).where(
                    Fact.entity_id == eid,
                    Fact.predicate == predicate,
                    Fact.status == "active",
                    Fact.valid_to.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def _name_of(session: AsyncSession, eid: uuid.UUID | None) -> str | None:
    if eid is None:
        return None
    return (
        await session.execute(select(Entity.canonical_name).where(Entity.id == eid))
    ).scalar_one_or_none()


# --- observations -> lab_results ------------------------------------------


async def _project_observation(session: AsyncSession, ent: Entity) -> None:
    eid = ent.id
    await session.execute(
        text("DELETE FROM app.lab_results WHERE entity_id = :eid"), {"eid": str(eid)}
    )

    loinc = None
    for f in await _active_facts(session, eid, "identifier"):
        if f.qualifier == "loinc":
            loinc = _text(f.value_json)

    # All readings of this analyte, retracted excluded (§4.1). Group by qualifier
    # (the draw); a qualifier holds a superseded predecessor + its active head only
    # when corrected (the sole chain producer on this path).
    readings = list(
        (
            await session.execute(
                select(Fact).where(
                    Fact.entity_id == eid,
                    Fact.predicate == "value",
                    Fact.status.in_(("active", "pending_review", "superseded")),
                )
            )
        )
        .scalars()
        .all()
    )
    by_qual: dict[str, list[Fact]] = {}
    for f in readings:
        by_qual.setdefault(f.qualifier, []).append(f)

    for qual, facts in by_qual.items():
        specimen = qual.split("|", 1)[1] if "|" in qual else ""
        active = [f for f in facts if f.status == "active"]
        superseded = [f for f in facts if f.status == "superseded"]
        pending = [f for f in facts if f.status == "pending_review"]
        head_row_by_fact: dict[uuid.UUID, str] = {}

        # The current head first (so a predecessor's superseded_by_id resolves).
        for f in active:
            status = "corrected" if superseded else "final"
            row_id = await _insert_lab_row(
                session, ent, f, specimen, loinc, status, is_current=True, superseded_by=None
            )
            head_row_by_fact[f.id] = row_id
        for f in pending:
            await _insert_lab_row(
                session,
                ent,
                f,
                specimen,
                loinc,
                "preliminary",
                is_current=False,
                superseded_by=None,
            )
        for f in superseded:
            sup_by = head_row_by_fact.get(f.superseded_by) if f.superseded_by else None
            await _insert_lab_row(
                session, ent, f, specimen, loinc, "final", is_current=False, superseded_by=sup_by
            )


async def _insert_lab_row(
    session: AsyncSession,
    ent: Entity,
    fact: Fact,
    specimen: str,
    loinc: str | None,
    report_status: str,
    *,
    is_current: bool,
    superseded_by: str | None,
) -> str:
    vj = fact.value_json or {}
    ref = await _one_at_qualifier(session, ent.id, "referenceRange", fact.qualifier)
    interp = await _one_at_qualifier(session, ent.id, "interpretation", fact.qualifier)
    ref_low = ref_high = None
    if isinstance(ref, dict):
        ref_low = _num((ref.get("low") or {}).get("value"))
        ref_high = _num((ref.get("high") or {}).get("value"))
    lab = await _one_ref_name(session, ent.id, "performer", fact.qualifier)
    encounter_id, orderer = await _encounter_and_orderer(session, ent.id, fact.qualifier)
    row_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO app.lab_results"
            " (id, entity_id, analyte, loinc, value_num, unit, ref_low, ref_high,"
            "  interpretation, collected_at, specimen_id, performing_lab, orderer, encounter_id,"
            "  report_status, is_current, superseded_by_id, source_note_id, source_fact_id,"
            "  domain_code)"
            " VALUES (:id, :eid, :analyte, :loinc, :value_num, :unit, :ref_low, :ref_high,"
            "  :interp, :collected_at, :specimen, :lab, :orderer, :encounter_id,"
            "  :report_status, :is_current, :superseded_by, :note_id, :fact_id, :domain)"
        ),
        {
            "id": row_id,
            "eid": str(ent.id),
            "analyte": ent.canonical_name,
            "loinc": loinc,
            "value_num": _num(vj.get("value")),
            "unit": _text(vj.get("unit")),
            "ref_low": ref_low,
            "ref_high": ref_high,
            "interp": _text(interp) if interp else None,
            "collected_at": fact.valid_from,
            "specimen": specimen,
            "lab": lab,
            "orderer": orderer,
            "encounter_id": str(encounter_id) if encounter_id else None,
            "report_status": report_status,
            "is_current": is_current,
            "superseded_by": superseded_by,
            "note_id": str(fact.note_id),
            "fact_id": str(fact.id),
            "domain": fact.domain_code,
        },
    )
    return row_id


async def _one_at_qualifier(
    session: AsyncSession, eid: uuid.UUID, predicate: str, qualifier: str
) -> Any:
    row = (
        await session.execute(
            select(Fact.value_json).where(
                Fact.entity_id == eid,
                Fact.predicate == predicate,
                Fact.qualifier == qualifier,
                Fact.status == "active",
            )
        )
    ).first()
    return row[0] if row else None


async def _one_ref_name(
    session: AsyncSession, eid: uuid.UUID, predicate: str, qualifier: str
) -> str | None:
    row = (
        await session.execute(
            select(Fact.object_entity_id).where(
                Fact.entity_id == eid,
                Fact.predicate == predicate,
                Fact.qualifier == qualifier,
                Fact.status == "active",
            )
        )
    ).first()
    return await _name_of(session, row[0]) if row and row[0] else None


async def _encounter_and_orderer(
    session: AsyncSession, obs_id: uuid.UUID, qualifier: str
) -> tuple[uuid.UUID | None, str | None]:
    """The enclosing Encounter (via the hasObservation edge at this draw) and its
    ordering provider; NULL for an orphan portal lab with no encounter (§3.4)."""
    row = (
        await session.execute(
            select(Fact.entity_id).where(
                Fact.predicate == "hasObservation",
                Fact.object_entity_id == obs_id,
                Fact.qualifier == qualifier,
                Fact.status == "active",
            )
        )
    ).first()
    if not row:
        return None, None
    enc_id = row[0]
    orderer_row = (
        await session.execute(
            select(Fact.object_entity_id).where(
                Fact.entity_id == enc_id,
                Fact.predicate == "attender",
                Fact.qualifier == _ORDERING_ROLE,
                Fact.status == "active",
            )
        )
    ).first()
    orderer = await _name_of(session, orderer_row[0]) if orderer_row and orderer_row[0] else None
    return enc_id, orderer


# --- encounters -> encounters (+ sidecars) --------------------------------


async def _project_encounter(session: AsyncSession, ent: Entity) -> None:
    eid = ent.id
    # Re-derive: drop the row (sidecars cascade) then rebuild.
    await session.execute(
        text("DELETE FROM app.encounters WHERE entity_id = :eid"), {"eid": str(eid)}
    )

    # The period is a CLOSED SCD-2 interval (valid_to = discharge), so it must NOT
    # be filtered on `valid_to IS NULL` — that is the current stay, not history.
    period = (
        (
            await session.execute(
                select(Fact)
                .where(Fact.entity_id == eid, Fact.predicate == "period", Fact.status == "active")
                .order_by(Fact.valid_from.desc().nullslast())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    admitted = period[0].valid_from if period else None
    discharged = period[0].valid_to if period else None
    enc_class = _text(period[0].value_json) if period else None
    if enc_class is None:
        cls = await _active_facts(session, eid, "class")
        enc_class = _text(cls[0].value_json) if cls else None
    care_unit = _first_text(await _active_facts(session, eid, "careUnit"))
    disposition = _first_text(await _active_facts(session, eid, "disposition"))
    facility = await _one_ref_name_any(session, eid, "serviceProvider")
    part_of = await _one_object_any(session, eid, "partOfEncounter")
    los = (
        (discharged - admitted).days
        if isinstance(admitted, datetime) and isinstance(discharged, datetime)
        else None
    )
    note_id = period[0].note_id if period else _any_note(await _active_facts(session, eid, "class"))
    await session.execute(
        text(
            "INSERT INTO app.encounters"
            " (entity_id, class, facility, care_unit, admitted_at, discharged_at, los_days,"
            "  disposition, part_of_id, source_note_id, domain_code)"
            " VALUES (:eid, :cls, :facility, :care_unit, :admitted, :discharged, :los,"
            "  :disposition, :part_of, :note_id, :domain)"
        ),
        {
            "eid": str(eid),
            "cls": enc_class,
            "facility": facility,
            "care_unit": care_unit,
            "admitted": admitted,
            "discharged": discharged,
            "los": los,
            "disposition": disposition,
            "part_of": str(part_of) if part_of else None,
            "note_id": str(note_id) if note_id else None,
            "domain": ent.domain_code,
        },
    )
    for f in await _active_facts(session, eid, "attender"):
        name = await _name_of(session, f.object_entity_id)
        if name:
            await session.execute(
                text(
                    "INSERT INTO app.encounter_providers"
                    " (id, encounter_id, provider_id, provider_name, role, domain_code)"
                    " VALUES (gen_random_uuid(), :enc, :pid, :name, :role, :domain)"
                    " ON CONFLICT (encounter_id, provider_id, role) DO NOTHING"
                ),
                {
                    "enc": str(eid),
                    "pid": str(f.object_entity_id),
                    "name": name,
                    "role": f.qualifier or None,
                    "domain": f.domain_code,
                },
            )
    for f in await _active_facts(session, eid, "encounterDiagnosis"):
        label = await _name_of(session, f.object_entity_id)
        if label:
            await session.execute(
                text(
                    "INSERT INTO app.encounter_diagnoses"
                    " (id, encounter_id, condition_id, icd10, label, domain_code)"
                    " VALUES (gen_random_uuid(), :enc, :cid, :icd10, :label, :domain)"
                    " ON CONFLICT (encounter_id, condition_id) DO NOTHING"
                ),
                {
                    "enc": str(eid),
                    "cid": str(f.object_entity_id),
                    "icd10": f.qualifier or None,
                    "label": label,
                    "domain": f.domain_code,
                },
            )


def _first_text(facts: list[Fact]) -> str | None:
    return _text(facts[0].value_json) if facts else None


def _any_note(facts: list[Fact]) -> uuid.UUID | None:
    return facts[0].note_id if facts else None


async def _one_ref_name_any(session: AsyncSession, eid: uuid.UUID, predicate: str) -> str | None:
    facts = await _active_facts(session, eid, predicate)
    return await _name_of(session, facts[0].object_entity_id) if facts else None


async def _one_object_any(
    session: AsyncSession, eid: uuid.UUID, predicate: str
) -> uuid.UUID | None:
    facts = await _active_facts(session, eid, predicate)
    return facts[0].object_entity_id if facts else None
