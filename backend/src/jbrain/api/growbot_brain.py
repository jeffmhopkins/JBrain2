"""GrowBot brain tools (docs/plans/GROWBOT_PLAN.md "Brain tools"): a persona's creature
searches the owner's notes and files a new one, the way a note typed in the PWA lands.

The sidecar's routes, on the growbot token, keyed like the soul's by the persona and the phone
the sidecar signed into that phone's session. Every call is refused (403 `off`) unless the
owner turned brain access on for this persona on this phone and the phone holds it right now
(`PersonaRepo.brain_ok`, migration 0267): the phone can neither set that nor name another
phone. Only then does the call run as the owner, across every domain (owner, 2026-11-13:
"searching notes should not be domain restricted with owner key"). Each call is written to the
GrowBot call log as kind `brain`, so the owner sees what was asked and what came back.

A search answers with a few short lines, never whole notes: the creature reads them aloud.
"""

from __future__ import annotations

import collections
import time
import uuid
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.growbot import PHONE_RE
from jbrain.api.growbot_llm import log_call
from jbrain.api.internal_auth import require_growbot
from jbrain.auth.owner import owner_principal_id
from jbrain.db.session import SessionContext
from jbrain.growbot.calls import CallRecord
from jbrain.growbot.personas import PersonaRepo
from jbrain.notes.service import UnknownDomain
from jbrain.search.service import SearchResult, SearchService, WikiSearchResult
from jbrain.workflow import events as wf_events

internal_router = APIRouter(prefix="/growbot/brain", dependencies=[Depends(require_growbot)])

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
QUERY_MAX = 200
NOTE_MAX = 2000
HITS = 4
# What one hit may say aloud: a sentence or two, so the follow-up turn stays short.
HIT_CHARS = 220
# Notes a persona may file: a creature stuck in a loop (or talked into one) files a few, never a
# flood, and each one costs an ingest. Kept in memory: a restart forgets, which is fine.
NOTE_WINDOWS = ((600.0, 6), (86_400.0, 60))


class BrainIn(BaseModel):
    persona: str = ""
    phone: str = ""


class SearchIn(BrainIn):
    q: str = ""


class NoteIn(BrainIn):
    text: str = ""


class FoundOut(BaseModel):
    n: int
    # One line per hit, its date first; "" when nothing matched.
    text: str


class FiledOut(BaseModel):
    ok: bool


async def _owner(request: Request, body: BrainIn) -> SessionContext:
    """The owner's context for a call the gate lets through; 401 for a body naming no phone,
    403 `off` for every phone the owner did not turn brain access on for."""
    if not PHONE_RE.fullmatch(body.phone) or len(body.persona) > 64:
        raise HTTPException(status_code=401, detail="unauthorized")
    personas = cast(PersonaRepo, request.app.state.growbot_personas)
    if not await personas.brain_ok(_SYSTEM_OWNER, body.persona, body.phone):
        raise HTTPException(status_code=403, detail="off")
    owner = await owner_principal_id(request.app.state.session_maker)
    if owner is None:
        raise HTTPException(status_code=403, detail="off")
    return SessionContext(principal_id=owner, principal_kind="owner")


def _line(hit: SearchResult | WikiSearchResult) -> str:
    if isinstance(hit, WikiSearchResult):
        head, said = f"(wiki: {hit.title})", hit.blurb or hit.snippet
    else:
        head, said = f"({hit.created_at:%Y-%m-%d})", hit.snippet or hit.body_preview
    said = " ".join(said.replace("<b>", "").replace("</b>", "").split())
    if len(said) > HIT_CHARS:
        said = said[:HIT_CHARS].rsplit(" ", 1)[0] + "…"
    return f"- {head} {said}"


def _log(
    request: Request,
    body: BrainIn,
    what: str,
    asked: str,
    ok: bool,
    reply: str,
    t0: float,
    error: str = "",
) -> None:
    log_call(
        request,
        CallRecord(
            kind="brain",
            ok=ok,
            total_ms=int((time.monotonic() - t0) * 1000),
            persona_id=body.persona or None,
            model=what,
            error=error,
            request={"asked": asked},
            reply=reply,
        ),
    )


def _note_allowed(request: Request, persona: str) -> bool:
    filed: dict[str, collections.deque[float]] | None = getattr(
        request.app.state, "growbot_brain_notes", None
    )
    if filed is None:
        filed = request.app.state.growbot_brain_notes = {}
    now = time.monotonic()
    seen = filed.setdefault(persona, collections.deque())
    while seen and now - seen[0] > NOTE_WINDOWS[-1][0]:
        seen.popleft()
    if any(sum(1 for t in seen if now - t <= span) >= most for span, most in NOTE_WINDOWS):
        return False
    seen.append(now)
    return True


@internal_router.post("/search")
async def brain_search(request: Request, body: SearchIn) -> FoundOut:
    """The owner's notes (and wiki) for a few search words, as a few short dated lines."""
    t0 = time.monotonic()
    ctx = await _owner(request, body)
    q = " ".join(body.q.split())[:QUERY_MAX]
    if not q:
        raise HTTPException(status_code=400, detail="a search")
    search = cast(SearchService, request.app.state.search_service)
    try:
        found = await search.search(ctx, q, None, HITS)
    except Exception as exc:
        _log(request, body, "search", q, False, "", t0, type(exc).__name__)
        raise HTTPException(status_code=503, detail="busy") from exc
    lines = [_line(h) for h in list(found.results)[:HITS]]
    out = FoundOut(n=len(lines), text="\n".join(lines))
    _log(request, body, "search", q, True, out.text, t0)
    return out


@internal_router.post("/note")
async def brain_note(request: Request, body: NoteIn) -> FiledOut:
    """A new note in the owner's notes, ingested like one typed in the PWA, marked as the
    creature's (`agent`, source `growbot:<persona>`)."""
    t0 = time.monotonic()
    ctx = await _owner(request, body)
    said = body.text.strip()[:NOTE_MAX]
    if not said:
        raise HTTPException(status_code=400, detail="a note")
    if not _note_allowed(request, body.persona):
        _log(request, body, "note", said, False, "", t0, "too many notes")
        raise HTTPException(status_code=429, detail="too many notes")
    notes = request.app.state.notes_repo
    try:
        note, created = await notes.create_note(
            ctx,
            client_id=f"growbot-{uuid.uuid4().hex}",
            domain="general",
            destination=None,
            body=said,
            provenance="agent",
            source_ref=f"growbot:{body.persona}",
        )
    except UnknownDomain as exc:
        _log(request, body, "note", said, False, "", t0, "unknown domain")
        raise HTTPException(status_code=503, detail="busy") from exc
    if created:
        await wf_events.emit_event(
            request.app.state.session_maker,
            ctx,
            type=wf_events.NOTE_CREATED,
            domain_code=note.domain,
            payload={"note_id": note.id},
            enqueued=wf_events.shadow_enqueued("ingest_note", {"note_id": note.id}),
            principal_id=ctx.principal_id,
        )
    _log(request, body, "note", said, True, note.id, t0)
    return FiledOut(ok=True)
