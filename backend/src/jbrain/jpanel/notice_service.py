"""Send a notice to the owner's desk panel, and hear the answer (JPANEL_C6_PLAN.md §2, C3b).

The one way in for every sender: the owner's route and the debug console
(`api/panel_notice.py`), Jerv's `panel_notify` tool (`agent/paneltools.py`), and anything on
the box that wants to tap the owner on the shoulder — an Ops alert, a workflow step — which
calls `NoticeService.send` with its own `sender` (`ops`, `workflow:<name>`). One instance
lives on `app.state.panel_notices`.

`send` checks everything that is free to check first (the text, the refusal table), then does
the work a picture or a sound costs (fitting and encoding on a thread, decoding through
ffmpeg, synthesis through the on-box TTS), then stores the encoded picture and the PCM in the
blob store (CLAUDE.md #2) and writes the row. Every query runs under an owner context: the
table's owner policy is what lets anything insert (CLAUDE.md #3, migration 0235).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.blob_refs import blob_referenced
from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpanel import debug_relay as relay
from jbrain.jpanel import notice as N
from jbrain.storage import BlobStore

log = structlog.get_logger()

#: Every sender writes as the owner: the notice is the box speaking to its owner.
_OWNER = SessionContext(principal_kind="owner")
#: How often a waiting sender re-reads the row.
_POLL_S = 0.5
#: ffmpeg is handed at most this much of a clip, so a two-hour upload costs seconds, not
#: minutes; anything that long is refused as too long anyway.
_DECODE_CAP_S = 30
_DECODE_TIMEOUT_S = 30.0

ImageModeIn = Literal["auto", "full", "banner"]
TtsFetch = Callable[[dict[str, str]], Awaitable[bytes]]


@dataclass(frozen=True)
class PanelRef:
    device_id: str
    name: str


@dataclass(frozen=True)
class Sent:
    id: str
    device_id: str
    panel: str
    state: str
    #: Seconds the panel has to collect it; then it is expired, not delivered late.
    expires_in_s: int
    image: dict[str, Any] | None
    audio_ms: int | None


@dataclass(frozen=True)
class NoticeView:
    id: str
    device_id: str
    sender: str
    title: str
    body: str
    choices: list[str]
    #: queued -> delivered -> shown -> answered / dismissed / expired.
    state: str
    answer: str | None
    created_at: str
    delivered_at: str | None
    shown_at: str | None
    finished_at: str | None
    hide_after_s: int | None
    image: dict[str, Any] | None
    audio_ms: int | None

    @property
    def final(self) -> bool:
        return self.state in N.FINAL_STATES

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class NoticeService:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        *,
        tts_url: str = "",
        voice: Callable[[], Awaitable[Any]] | None = None,
        tts_fetch: TtsFetch | None = None,
    ) -> None:
        self._maker = maker
        self._blobs = blobs
        self._tts_url = tts_url.rstrip("/")
        # The PWA's read-aloud voice (`panel_agent.owner_voice`), resolved per send so a voice
        # changed in Settings is the one the next notice speaks in.
        self._voice = voice
        self._tts_fetch = tts_fetch or self._fetch_tts
        self._can_speak = bool(self._tts_url) or tts_fetch is not None

    # ---- which panel ------------------------------------------------------------------

    async def panels(self) -> list[PanelRef]:
        async with scoped_session(self._maker, _OWNER) as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT p.id::text, s.display_name
                        FROM app.principals p
                        JOIN app.subjects s ON s.id = p.subject_id
                        WHERE p.kind = 'device_key' AND p.revoked_at IS NULL
                          AND s.device_role IS NOT NULL
                        ORDER BY p.created_at DESC
                        """
                    )
                )
            ).all()
        from jbrain.api.endpoint import panel_display_name  # noqa: PLC0415 - the API imports this

        return [PanelRef(str(r[0]), panel_display_name(str(r[1]))) for r in rows]

    async def resolve(self, device_id: str | None) -> PanelRef:
        """The panel to send to: the one named, or — when the sender names none, as Jerv and
        Ops do — the one panel that takes notices. Never a guess between two."""
        panels = await self.panels()
        if device_id:
            for p in panels:
                if p.device_id == device_id:
                    return p
            raise N.NoticeRefused("no_such_panel", 404)
        takers = [p for p in panels if N.prefs_for(p.device_id) is not None]
        if len(takers) == 1:
            return takers[0]
        if not takers:
            # None online and reporting: answer for the one that WOULD take them, so the
            # sender hears "offline" rather than "which panel?" about a panel that is off.
            debug = [p for p in panels if (relay.last_seen(p.device_id) or (0, ""))[1]]
            if len(debug) == 1:
                return debug[0]
        raise N.NoticeRefused(
            "which_panel",
            409,
            panels=[{"device_id": p.device_id, "name": p.name} for p in panels],
        )

    # ---- send ---------------------------------------------------------------------------

    async def send(
        self,
        *,
        sender: str,
        created_by: str,
        title: str,
        body: str = "",
        choices: Sequence[str] = (),
        chime: bool = False,
        hide_after_s: int | None = None,
        ttl_s: int | None = None,
        image: bytes | None = None,
        image_mode: ImageModeIn = "auto",
        audio: bytes | None = None,
        say: str | None = None,
        device_id: str | None = None,
        now: datetime | None = None,
    ) -> Sent:
        """Put one notice on a panel, or raise `NoticeRefused` saying why not."""
        N.check_sender(sender)
        title, body, picks = N.check_text(title, body, list(choices))
        hide = N.check_hide(hide_after_s, question=bool(picks))
        ttl = N.check_ttl(ttl_s)
        if audio is not None and say:
            raise N.NoticeRefused("bad_field", 422, field="say", want="audio or say, not both")
        if say is not None and not say.strip():
            raise N.NoticeRefused("bad_field", 422, field="say", want="non-empty")

        panel = await self.resolve(device_id)
        refused = N.refusal(
            relay.last_seen(panel.device_id), N.prefs_for(panel.device_id), now or datetime.now(UTC)
        )
        if refused is not None:
            log.info(
                "panel.notice_refused", device=panel.device_id, sender=sender, why=refused.reason
            )
            raise refused
        await self.sweep()
        if await self._open_count(panel.device_id) >= N.MAX_OPEN:
            raise N.NoticeRefused("busy", 429, open=N.MAX_OPEN)

        fitted: N.FittedImage | None = None
        if image is not None:
            mode: N.ImageMode = N.default_mode(body, picks) if image_mode == "auto" else image_mode
            fitted = await asyncio.to_thread(N.fit_image, image, mode)
        pcm: bytes | None = None
        if audio is not None:
            pcm = await self.decode_audio(audio)
        elif say:
            pcm = await self.speak(say)

        image_sha = await self._blobs.put(fitted.blob()) if fitted else None
        audio_sha = await self._blobs.put(pcm) if pcm else None
        async with scoped_session(self._maker, _OWNER) as s:
            nid = (
                await s.execute(
                    text(
                        """
                        INSERT INTO app.panel_notice
                            (device_id, sender, title, body, choices, chime, hide_after_s,
                             ttl_s, image_mode, image_w, image_h, image_sha256, image_bytes,
                             audio_sha256, audio_ms, created_by, expires_at)
                        VALUES (CAST(:dev AS uuid), :sender, :title, :body,
                                CAST(:choices AS jsonb), :chime, :hide, :ttl, :mode, :w, :h,
                                :isha, :ibytes, :asha, :ams, :by,
                                now() + make_interval(secs => :pickup))
                        RETURNING id::text
                        """
                    ),
                    {
                        "dev": panel.device_id,
                        "sender": sender,
                        "title": title,
                        "body": body,
                        "choices": json.dumps(picks),
                        "chime": chime,
                        "hide": hide,
                        "ttl": ttl,
                        "mode": fitted.mode if fitted else None,
                        "w": fitted.width if fitted else None,
                        "h": fitted.height if fitted else None,
                        "isha": image_sha,
                        "ibytes": len(fitted.blob()) if fitted else None,
                        "asha": audio_sha,
                        "ams": N.pcm_ms(pcm) if pcm else None,
                        "by": created_by[:200],
                        "pickup": N.PICKUP_WINDOW_S,
                    },
                )
            ).scalar_one()
            await s.commit()
        image_out = (
            {
                "mode": fitted.mode,
                "w": fitted.width,
                "h": fitted.height,
                "bytes": fitted.stream_bytes,
                "posterize": fitted.posterize,
            }
            if fitted
            else None
        )
        log.info(
            "panel.notice_sent",
            id=nid,
            device=panel.device_id,
            sender=sender,
            by=created_by,
            title=title[:60],
            choices=picks,
            image=image_out,
            audio_ms=N.pcm_ms(pcm) if pcm else None,
        )
        return Sent(
            id=str(nid),
            device_id=panel.device_id,
            panel=panel.name,
            state="queued",
            expires_in_s=N.PICKUP_WINDOW_S,
            image=image_out,
            audio_ms=N.pcm_ms(pcm) if pcm else None,
        )

    async def _open_count(self, device_id: str) -> int:
        async with scoped_session(self._maker, _OWNER) as s:
            n = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM app.panel_notice WHERE device_id = CAST(:d AS uuid)"
                        " AND state IN ('queued', 'delivered', 'shown') AND expires_at > now()"
                    ),
                    {"d": device_id},
                )
            ).scalar_one()
        return int(n)

    # ---- the sound ----------------------------------------------------------------------

    async def decode_audio(self, data: bytes) -> bytes:
        """The sender's clip as 16 kHz mono s16, ≤ 8 s. A PCM WAV is read here; anything else
        (MP3, Ogg/Opus, M4A, FLAC, WebM) goes through ffmpeg."""
        if len(data) > N.AUDIO_UPLOAD_MAX:
            raise N.NoticeRefused("audio_too_big", 413, max_bytes=N.AUDIO_UPLOAD_MAX)
        pcm = N.wav_pcm(data)
        if pcm is None:
            pcm = await _ffmpeg_pcm(data)
        return N.check_pcm(pcm)

    async def speak(self, words: str) -> bytes:
        """`words` in the PWA's read-aloud voice, as the panel plays it."""
        from jbrain.api import panel_agent  # noqa: PLC0415 - the API imports this module
        from jbrain.api.endpoint import (  # noqa: PLC0415
            _pcm_from_wav,
            _to_panel_rate,
            _trim_to_speech,
        )

        words = words.strip()
        if len(words) > N.SAY_CHARS:
            raise N.NoticeRefused("too_long", 422, field="say", max_chars=N.SAY_CHARS)
        if not self._can_speak:
            raise N.NoticeRefused("tts_unavailable", 503)
        voice = None
        if self._voice is not None:
            try:
                voice = await self._voice()
            except Exception as exc:  # noqa: BLE001 — the default voice is still a voice
                log.warning("panel.notice_voice_unavailable", error=repr(exc))
        try:
            wav = await self._tts_fetch(panel_agent.tts_params(voice, words))
            raw, rate = _pcm_from_wav(wav)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("panel.notice_tts_error", error=repr(exc))
            raise N.NoticeRefused("tts_unavailable", 503) from None
        pcm = _trim_to_speech(_to_panel_rate(raw, rate), lead_ms=30, tail_ms=120)
        try:
            return N.check_pcm(pcm)
        except N.NoticeRefused as exc:
            if exc.reason == "audio_too_long":
                exc.extra["hint"] = "say less: the clip is capped at eight seconds"
            raise

    async def _fetch_tts(self, params: dict[str, str]) -> bytes:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(f"{self._tts_url}/tts", params=params)
        resp.raise_for_status()
        return resp.content

    # ---- reading and waiting ----------------------------------------------------------

    async def read(self, notice_id: str, device_id: str | None = None) -> NoticeView | None:
        try:
            uuid.UUID(notice_id)
            if device_id:
                uuid.UUID(device_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, _OWNER) as s:
            row = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT id::text, device_id::text, sender, title, body, choices,
                                   CASE WHEN state IN ('queued', 'delivered', 'shown')
                                             AND expires_at <= now()
                                        THEN 'expired' ELSE state END AS state,
                                   answer, created_at, delivered_at, shown_at, finished_at,
                                   hide_after_s, image_mode, image_w, image_h, image_bytes,
                                   audio_ms
                            FROM app.panel_notice
                            WHERE id = CAST(:id AS uuid)
                              AND (CAST(:dev AS uuid) IS NULL OR device_id = CAST(:dev AS uuid))
                            """
                        ),
                        {"id": notice_id, "dev": device_id or None},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None

        def iso(v: datetime | None) -> str | None:
            return v.isoformat() if v else None

        return NoticeView(
            id=row["id"],
            device_id=row["device_id"],
            sender=row["sender"],
            title=row["title"],
            body=row["body"],
            choices=list(row["choices"] or []),
            state=row["state"],
            answer=row["answer"],
            created_at=row["created_at"].isoformat(),
            delivered_at=iso(row["delivered_at"]),
            shown_at=iso(row["shown_at"]),
            finished_at=iso(row["finished_at"]),
            hide_after_s=row["hide_after_s"],
            image=(
                {
                    "mode": row["image_mode"],
                    "w": row["image_w"],
                    "h": row["image_h"],
                    "bytes": row["image_bytes"],
                }
                if row["image_mode"]
                else None
            ),
            audio_ms=row["audio_ms"],
        )

    async def wait(
        self, notice_id: str, wait_s: float, device_id: str | None = None
    ) -> NoticeView | None:
        """The notice once it is answered, dismissed or expired — or as it stands when
        `wait_s` runs out. None if there is no such notice."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_s)
        while True:
            view = await self.read(notice_id, device_id)
            if view is None or view.final or loop.time() >= deadline:
                return view
            await asyncio.sleep(min(_POLL_S, max(0.0, deadline - loop.time())))

    async def sweep(self) -> int:
        """Expire what was never collected or answered in time, and delete what finished
        long ago with its picture and sound — each blob only once `blob_refs` says nothing
        else on the box holds the same bytes. Lazy, run on every send and owner read, because
        nothing else reads this table."""
        async with scoped_session(self._maker, _OWNER) as s:
            await s.execute(
                text(
                    """
                    UPDATE app.panel_notice SET state = 'expired', finished_at = expires_at
                    WHERE state IN ('queued', 'delivered', 'shown') AND expires_at <= now()
                    """
                )
            )
            gone = (
                await s.execute(
                    text(
                        """
                        DELETE FROM app.panel_notice
                        WHERE state IN ('answered', 'dismissed', 'expired')
                          AND finished_at <= now() - make_interval(secs => :ttl)
                        RETURNING image_sha256, audio_sha256
                        """
                    ),
                    {"ttl": N.RESULT_TTL_S},
                )
            ).all()
            shas = {str(sha) for row in gone for sha in row if sha}
            free = [sha for sha in shas if not await blob_referenced(s, sha)]
            await s.commit()
        for sha in free:
            await self._blobs.delete(sha)
        return len(gone)

    # ---- the panel's side: the stream ---------------------------------------------------

    async def stream_for(self, ctx: SessionContext, notice_id: str | None) -> bytes | None:
        """The JN1 stream of one of THIS panel's notices, read under the panel's own context
        (its policies, not this SQL, keep a sibling out). `notice_id` None: collect the oldest
        queued one, marking it delivered. A named one must already be delivered or shown — a
        re-read after a torn download. None when there is nothing."""
        async with scoped_session(self._maker, ctx) as s:
            if notice_id is None:
                row = (
                    (
                        await s.execute(
                            text(
                                f"""
                                UPDATE app.panel_notice SET state = 'delivered'
                                WHERE id = (
                                    SELECT id FROM app.panel_notice
                                    WHERE device_id = CAST(:me AS uuid) AND state = 'queued'
                                      AND expires_at > now()
                                    ORDER BY created_at
                                    LIMIT 1
                                    FOR UPDATE SKIP LOCKED
                                )
                                RETURNING {_WIRE_COLS}
                                """
                            ),
                            {"me": ctx.principal_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                await s.commit()
            else:
                row = (
                    (
                        await s.execute(
                            text(
                                f"""
                                SELECT {_WIRE_COLS} FROM app.panel_notice
                                WHERE id = CAST(:id AS uuid)
                                  AND state IN ('delivered', 'shown')
                                """
                            ),
                            {"id": notice_id},
                        )
                    )
                    .mappings()
                    .first()
                )
        if row is None:
            return None
        image = await self._blobs.get(row["image_sha256"]) if row["image_sha256"] else b""
        pcm = await self._blobs.get(row["audio_sha256"]) if row["audio_sha256"] else b""
        left = max(0, int((row["expires_at"] - datetime.now(UTC)).total_seconds()))
        wire = N.WireNotice(
            id=str(row["id"]),
            sender_label=N.sender_label(row["sender"]),
            title=row["title"],
            body=row["body"],
            choices=tuple(row["choices"] or ()),
            chime=bool(row["chime"]),
            hide_after_s=row["hide_after_s"],
            ttl_s=left,
            image_mode=row["image_mode"],
            image_w=row["image_w"] or 0,
            image_h=row["image_h"] or 0,
            audio_ms=row["audio_ms"] or 0,
        )
        return N.encode(wire, image, pcm)

    async def panel_state(
        self, ctx: SessionContext, notice_id: str, state: str, choice: int | None
    ) -> None:
        """The panel moves its own notice along. Raises `NoticeRefused`: 404 for anything not
        this panel's live notice, 409 for a move the trigger refuses, 422 for a bad choice."""
        if state not in ("shown", "answered", "dismissed", "expired"):
            raise N.NoticeRefused(
                "bad_state", 422, want=["shown", "answered", "dismissed", "expired"]
            )
        try:
            uuid.UUID(notice_id)
        except ValueError:
            raise N.NoticeRefused("no_such_notice", 404) from None
        async with scoped_session(self._maker, ctx) as s:
            cur = (
                await s.execute(
                    text(
                        "SELECT state, choices FROM app.panel_notice"
                        " WHERE id = CAST(:id AS uuid)"
                        " AND state IN ('delivered', 'shown') AND expires_at > now()"
                    ),
                    {"id": notice_id},
                )
            ).first()
        if cur is None:
            raise N.NoticeRefused("no_such_notice", 404)
        choices = list(cur[1] or [])
        answer: str | None = None
        if state == "answered":
            if choice is None or not 0 <= choice < len(choices):
                raise N.NoticeRefused("bad_choice", 422, choices=len(choices))
            answer = choices[choice]
        elif choice is not None:
            raise N.NoticeRefused("bad_choice", 422, why="only an answer carries a choice")
        if (cur[0], state) == ("shown", "shown"):
            return  # a repeated report is the panel retrying after a lost reply
        try:
            async with scoped_session(self._maker, ctx) as s:
                done = (
                    await s.execute(
                        text(
                            "UPDATE app.panel_notice SET state = :st, answer = :ans"
                            " WHERE id = CAST(:id AS uuid) RETURNING id"
                        ),
                        {"st": state, "ans": answer, "id": notice_id},
                    )
                ).first()
                await s.commit()
        except Exception as exc:  # noqa: BLE001 — the trigger's refusal is a 409, not a 500
            log.warning("panel.notice_state_refused", id=notice_id, state=state, error=repr(exc))
            raise N.NoticeRefused("bad_move", 409, now=cur[0]) from None
        if done is None:
            raise N.NoticeRefused("no_such_notice", 404)
        log.info(
            "panel.notice_state", id=notice_id, device=ctx.principal_id, state=state, answer=answer
        )


_WIRE_COLS = (
    "id::text AS id, sender, title, body, choices, chime, hide_after_s, image_mode, image_w,"
    " image_h, image_sha256, audio_sha256, audio_ms, expires_at"
)


async def queued_count(session: AsyncSession) -> int:
    """How many notices wait for the panel whose session this is — the `X-Notice` hint on
    its debug pickup."""
    n = (
        await session.execute(
            text(
                "SELECT count(*) FROM app.panel_notice"
                " WHERE state = 'queued' AND expires_at > now()"
            )
        )
    ).scalar_one()
    return int(n)


async def _ffmpeg_pcm(data: bytes) -> bytes:
    from jbrain.media import INPUT_PROTOCOLS, run_media_proc  # noqa: PLC0415

    if shutil.which("ffmpeg") is None:
        raise N.NoticeRefused("audio_format", 415, want="a 16-bit PCM WAV (no ffmpeg on this box)")
    with tempfile.TemporaryDirectory(prefix="jbrain-notice-") as tmp:
        # A file rather than a pipe: MP4/M4A keep their index at the end, which a pipe
        # cannot seek back to.
        src = Path(tmp) / "clip"
        src.write_bytes(data)
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *INPUT_PROTOCOLS,
            "-i",
            str(src),
            "-t",
            str(_DECODE_CAP_S),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(N.PANEL_RATE),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ]
        try:
            code, out, err = await run_media_proc(cmd, timeout_s=_DECODE_TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            raise N.NoticeRefused("audio_unreadable", 422, why=type(exc).__name__) from None
    if code != 0 or not out:
        raise N.NoticeRefused(
            "audio_unreadable", 422, why=err.decode("utf-8", "replace").strip()[-200:]
        )
    return out
