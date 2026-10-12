"""Jerv's `panel_notify`: a notice on the owner's desk panel, and his tap back (C3b).

docs/plans/JPANEL_C6_PLAN.md §2. A thin handler over `jpanel.notice_service` — the same send
the owner's route and the debug console use, so the refusal table, the limits and the picture
and sound pipelines are one implementation. `web`-classed: opt-in, jerv-only, direct (the
notice goes to the owner's own panel on his own box; there is no egress payload to approve).

A refusal comes back as words the model can act on — "quiet hours until 06:30" — never an
exception: the turn goes on, and Jerv says it in chat instead.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.chat_images import resolve_source
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.jpanel import notice as N
from jbrain.jpanel.notice_service import NoticeService
from jbrain.models.images import GeneratedImageRepo
from jbrain.storage import BlobStore

#: Dropped from the registry when the service is not wired (a test registry, a worker).
OPTIONAL_PANEL_TOOL = frozenset({"panel_notify"})

WAIT_DEFAULT_S = 90
WAIT_MIN_S = 5
WAIT_MAX_S = 240


def refusal_words(exc: N.NoticeRefused) -> str:
    """The refusal as the model should relay it."""
    x = exc.extra
    why = {
        "offline": (
            "the panel is offline"
            + (f" (last heard {x['last_seen_s']} s ago)" if x.get("last_seen_s") else "")
        ),
        "not_accepting": "he has notices switched off on the panel",
        "quiet_hours": f"it is the panel's quiet hours, until {x.get('until', 'morning')}",
        "no_notice_surface": "the panel's firmware cannot show notices yet",
        "busy": f"{N.MAX_OPEN} notices are already waiting on it",
        "which_panel": "more than one panel could take it and none was named",
        "tts_unavailable": "the box cannot speak right now (send it without `say`)",
    }.get(exc.reason)
    if why is not None:
        return f"The panel refused it: {why}. Do not retry; tell him in your reply instead."
    detail = ", ".join(f"{k}={v}" for k, v in x.items())
    return f"Not sent — {exc.reason}" + (f" ({detail})" if detail else "") + ". Fix it and retry."


def build_panel_handlers(
    notices: NoticeService,
    *,
    maker: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    images: GeneratedImageRepo,
    attachments: TurnAttachmentRepo,
) -> dict[str, ToolHandler]:
    async def panel_notify_tool(arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        title = str(arguments.get("title", ""))
        raw_choices = arguments.get("choices") or []
        if not isinstance(raw_choices, list):
            return ToolOutput("`choices` must be a list of short strings.")
        choices = [str(c) for c in raw_choices]
        image: bytes | None = None
        image_id = str(arguments.get("image_id") or "")
        attachment_id = str(arguments.get("attachment_id") or "")
        if image_id or attachment_id:
            if image_id and attachment_id:
                return ToolOutput("Give image_id or attachment_id, not both.")
            found = await resolve_source(
                image_id,
                attachment_id,
                session_ctx=ctx.session,
                agent_session_id=ctx.agent_session_id,
                blobs=blobs,
                repo=images,
                attachments=attachments,
                maker=maker,
            )
            if isinstance(found, str):
                return ToolOutput(found)
            image = found[0]
        raw_hide = arguments.get("hide_after_s")
        try:
            hide = int(raw_hide) if raw_hide is not None else None
        except (TypeError, ValueError):
            return ToolOutput("`hide_after_s` must be a whole number of seconds (3-600).")
        say = arguments.get("say")
        try:
            sent = await notices.send(
                sender="jerv",
                created_by=f"jerv (session {ctx.agent_session_id or 'none'})",
                title=title,
                body=str(arguments.get("body") or ""),
                choices=choices,
                chime=bool(arguments.get("chime", False)),
                hide_after_s=hide,
                image=image,
                say=str(say) if say else None,
            )
        except N.NoticeRefused as exc:
            return ToolOutput(refusal_words(exc), result_brief=f"refused: {exc.reason}")

        on = f"the {sent.panel} panel" if sent.panel else "the panel"
        await_answer = bool(arguments.get("await_answer", bool(choices)))
        if not choices or not await_answer:
            return ToolOutput(
                f"Shown on {on} (notice {sent.id}).",
                result_brief="sent",
            )
        try:
            wait_s = int(arguments.get("wait_s", WAIT_DEFAULT_S))
        except (TypeError, ValueError):
            wait_s = WAIT_DEFAULT_S
        wait_s = max(WAIT_MIN_S, min(WAIT_MAX_S, wait_s))
        view = await notices.wait(sent.id, wait_s)
        state = view.state if view else "expired"
        if view is not None and view.state == "answered":
            return ToolOutput(
                f"He answered on {on}: {view.answer}.", result_brief=view.answer or ""
            )
        if state == "dismissed":
            return ToolOutput(f"He dismissed it on {on} without answering.", result_brief=state)
        if state == "expired":
            return ToolOutput(f"It expired on {on} unanswered.", result_brief=state)
        return ToolOutput(
            f"No answer yet after {wait_s} s (the notice is {state} on {on}; the question "
            f"stays up for {N.TTL_DEFAULT_S // 60} minutes). Notice id {sent.id}.",
            result_brief="no answer yet",
        )

    return {"panel_notify": panel_notify_tool}
