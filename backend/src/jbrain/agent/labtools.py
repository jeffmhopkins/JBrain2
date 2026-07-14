"""The agent's lab / encounter read tools (docs/plans/EMR_IMPORT_PLAN.md §7.1).

Read-only over the `app.lab_results` / `app.encounters` projections on the
caller's RLS-scoped session, so a non-health scope sees NOTHING by construction
(the firewall is the tooth, not a name check). The prose reports what the record
CONTAINS — numbers, ranges, flags, dates, coded diagnoses — and never a
diagnosis, cause, or recommendation. A superseded reading is marked
"corrected — see current"; a pending/preliminary reading is called out rather
than presented as current.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.chartscale import nice_scale
from jbrain.agent.contracts import CitationRef, FactRef, ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import scoped_session

_LAB_COLS = (
    "id, analyte, value_num, value_text, unit, ref_low, ref_high, ref_text, interpretation,"
    " collected_at, performing_lab, encounter_id, report_status, is_current, superseded_by_id,"
    " source_note_id"
)


def _num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _lab_line(r: Any) -> str:
    value = _num(r["value_num"]) if r["value_num"] is not None else (r["value_text"] or "—")
    unit = f" {r['unit']}" if r["unit"] else ""
    ref = ""
    if r["ref_low"] is not None or r["ref_high"] is not None:
        ref = f" (ref {_num(r['ref_low'])}–{_num(r['ref_high'])})"
    elif r["ref_text"]:
        ref = f" (ref {r['ref_text']})"
    flag = f" [{r['interpretation'].upper()}]" if r["interpretation"] else ""
    when = r["collected_at"].strftime("%Y-%m-%d %H:%M") if r["collected_at"] else "?"
    lab = f" · {r['performing_lab']}" if r["performing_lab"] else ""
    enc = f" · enc:{r['encounter_id']}" if r["encounter_id"] else ""
    status = ""
    if not r["is_current"]:
        if r["report_status"] == "preliminary":
            status = " · PRELIMINARY (not current)"
        elif r["superseded_by_id"]:
            status = f" · corrected — see current ({r['superseded_by_id']})"
        else:
            status = " · not current"
    elif r["report_status"] == "corrected":
        status = " · corrected (current)"
    return (
        f"[{r['id']}] {r['analyte']}: {value}{unit}{ref}{flag} · {when}{lab}{enc}{status}"
        f" · note:{r['source_note_id']}"
    )


def format_labs(rows: list[Any]) -> str:
    if not rows:
        return "No lab results are on record (or none are in the current scope)."
    return "\n".join(_lab_line(r) for r in rows)


def _chart_flag(interp: Any, val: float, lo: float | None, hi: float | None) -> str:
    """Map a draw to the view's closed flag enum (normal|low|high|critical). Prefer the
    record's own interpretation; fall back to the reference band when it is bare."""
    i = str(interp or "").lower()
    if i == "critical":
        return "critical"
    if i in ("high", "low"):
        return i
    if hi is not None and val > hi:
        return "high"
    if lo is not None and val < lo:
        return "low"
    return "normal"


def lab_chart_view(rows: list[Any]) -> ViewPayload | None:
    """Build the `lab_chart` tool-view for a single-analyte trend (DESIGN.md
    "chart & lab_chart tool-views"). Plots only **current, numeric, non-preliminary**
    draws — the trend as it stands now; superseded / preliminary readings stay in the
    text (and the Table), never in the plotted series. Returns None when fewer than two
    points would be plotted (a single reading is not a trend). Data-only (#1/#9): numbers
    plus a closed `flag` enum; the component owns the palette. Health-domain by
    construction — the rows came from a health-scoped read."""
    pts: list[dict[str, Any]] = []
    refs: list[CitationRef] = []
    ref_lo: float | None = None
    ref_hi: float | None = None
    unit = ""
    analyte = ""
    for r in rows:
        if r["value_num"] is None or not r["is_current"] or r["report_status"] == "preliminary":
            continue
        when = r["collected_at"]
        if when is None:
            continue
        val = float(r["value_num"])
        lo = float(r["ref_low"]) if r["ref_low"] is not None else None
        hi = float(r["ref_high"]) if r["ref_high"] is not None else None
        if lo is not None:
            ref_lo = lo
        if hi is not None:
            ref_hi = hi
        unit = r["unit"] or unit
        analyte = r["analyte"] or analyte
        note_id = r["source_note_id"]
        point: dict[str, Any] = {
            "x": int(when.timestamp() * 1000),
            "y": val,
            "flag": _chart_flag(r["interpretation"], val, lo, hi),
        }
        if note_id:
            point["note"] = f"note:{note_id}"
        pts.append(point)
        refs.append(FactRef(fact_id=str(r["id"]), label=f"{analyte} {_num(val)}"))
    if len(pts) < 2:
        return None
    order = sorted(range(len(pts)), key=lambda i: pts[i]["x"])
    pts = [pts[i] for i in order]
    refs = [refs[i] for i in order]
    y_min, y_max, ticks = nice_scale([p["y"] for p in pts], ref_lo)
    data: dict[str, Any] = {
        "domain": "health",
        "unit": unit,
        "title": analyte or "Lab result",
        "x_kind": "time",
        "y": {"min": y_min, "max": y_max, "ticks": ticks},
        "series": [{"label": analyte, "points": pts}],
    }
    if ref_lo is not None and ref_hi is not None:
        data["ref"] = {
            "lo": ref_lo,
            "hi": ref_hi,
            "label": f"reference {_num(ref_lo)}–{_num(ref_hi)}",
        }
    return ViewPayload(view="lab_chart", surface="inline", data=data, refs=refs)


def _encounter_line(r: Any) -> str:
    unit = f" ({r['care_unit']})" if r["care_unit"] else ""
    fac = f" at {r['facility']}" if r["facility"] else ""
    admit = r["admitted_at"].strftime("%Y-%m-%d") if r["admitted_at"] else "?"
    disc = r["discharged_at"].strftime("%Y-%m-%d") if r["discharged_at"] else "ongoing"
    los = f", LOS {r['los_days']}d" if r["los_days"] is not None else ""
    dispo = f", {r['disposition']}" if r["disposition"] else ""
    return f"[{r['entity_id']}] {r['class'] or 'encounter'}{fac}{unit}: {admit}–{disc}{los}{dispo}"


def build_lab_handlers(maker: async_sessionmaker[AsyncSession]) -> dict[str, ToolHandler]:
    async def read_labs_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        conds: list[str] = []
        params: dict[str, Any] = {"limit": int(arguments.get("limit", 20) or 20)}
        analyte = str(arguments.get("analyte", "") or "").strip()
        if analyte:
            conds.append("lower(analyte) LIKE :analyte")
            params["analyte"] = f"%{analyte.lower()}%"
        if arguments.get("since"):
            conds.append("collected_at >= :since")
            params["since"] = str(arguments["since"])
        if arguments.get("until"):
            conds.append("collected_at <= :until")
            params["until"] = str(arguments["until"])
        if arguments.get("abnormal_only"):
            conds.append("interpretation IN ('critical','high','low','abnormal')")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        # trend: for one analyte, oldest-to-newest so the model can describe the move.
        ascending = bool(arguments.get("trend")) and bool(analyte)
        order = "collected_at ASC" if ascending else "collected_at DESC"
        sql = f"SELECT {_LAB_COLS} FROM app.lab_results{where} ORDER BY {order} LIMIT :limit"
        async with scoped_session(maker, ctx.session) as s:
            rows = list((await s.execute(text(sql), params)).mappings().all())
        # A single-analyte trend also renders an interactive lab_chart view; a bare list
        # or a lone reading stays text-only.
        view = lab_chart_view(rows) if ascending else None
        if view is not None:
            return ToolOutput(format_labs(rows), view=view)
        return ToolOutput(format_labs(rows))

    async def read_encounters_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        enc_id = str(arguments.get("encounter_id", "") or "").strip()
        async with scoped_session(maker, ctx.session) as s:
            if enc_id:
                return ToolOutput(await _expand_encounter(s, enc_id))
            conds: list[str] = []
            params: dict[str, Any] = {"limit": int(arguments.get("limit", 20) or 20)}
            if arguments.get("since"):
                conds.append("admitted_at >= :since")
                params["since"] = str(arguments["since"])
            if arguments.get("until"):
                conds.append("admitted_at <= :until")
                params["until"] = str(arguments["until"])
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT entity_id, class, facility, care_unit, admitted_at,"
                            f" discharged_at, los_days, disposition FROM app.encounters{where}"
                            " ORDER BY admitted_at DESC NULLS LAST LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        if not rows:
            return ToolOutput("No encounters are on record (or none are in the current scope).")
        return ToolOutput("\n".join(_encounter_line(r) for r in rows))

    return {"read_labs": read_labs_tool, "read_encounters": read_encounters_tool}


async def _expand_encounter(s: Any, enc_id: str) -> str:
    row = (
        (
            await s.execute(
                text(
                    "SELECT entity_id, class, facility, care_unit, admitted_at, discharged_at,"
                    " los_days, disposition, part_of_id, source_note_id FROM app.encounters"
                    " WHERE entity_id = :id"
                ),
                {"id": enc_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return "No encounter with that id is in scope."
    lines = [_encounter_line(row)]
    if row["part_of_id"]:
        lines.append(f"  part of the same hospitalization as encounter {row['part_of_id']}")
    providers = (
        (
            await s.execute(
                text(
                    "SELECT provider_name, role FROM app.encounter_providers"
                    " WHERE encounter_id = :id"
                ),
                {"id": enc_id},
            )
        )
        .mappings()
        .all()
    )
    for p in providers:
        lines.append(f"  provider: {p['provider_name']} ({p['role'] or 'role unstated'})")
    diagnoses = (
        (
            await s.execute(
                text("SELECT icd10, label FROM app.encounter_diagnoses WHERE encounter_id = :id"),
                {"id": enc_id},
            )
        )
        .mappings()
        .all()
    )
    for d in diagnoses:
        lines.append(f"  diagnosis: {d['icd10'] or '—'} {d['label']}")
    # Transfusions read from the encounter entity's `transfusion` events (§4.2).
    transfusions = (
        await s.execute(
            text(
                "SELECT value_json FROM app.facts WHERE entity_id = :id"
                " AND predicate = 'transfusion' AND status = 'active'"
            ),
            {"id": enc_id},
        )
    ).all()
    for (vj,) in transfusions:
        vj = vj or {}
        lines.append(
            f"  transfusion: {vj.get('product', '?')} x{vj.get('units', '?')}"
            f" — {vj.get('indication', '')}".rstrip()
        )
    lines.append(f"  source note: {row['source_note_id']}")
    return "\n".join(lines)
