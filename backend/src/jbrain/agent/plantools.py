"""jerv's per-conversation planning tools (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

A `web`-class (direct-exec, jerv-only) pair over the owner-only `agent_session_plans`
row keyed to THIS chat: jerv drafts a plan, the owner approves it, and jerv executes
against it. `read_plan` returns the current body + status; `write_plan` creates/rewrites
the body and/or moves the status — but the state machine here forbids jerv from ever
setting `approved` (owner-only) or jumping to `in_work` before the owner has approved.
Each handler runs under `ctx.session`'s RLS scope (jerv is an owner session), so the
owner-only firewall on the table — not this code — is the security gate; the state
machine is a UX/anti-injection guard on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import scoped_session
from jbrain.models.plan import PlanRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Cap the plan body so it stays a working plan, not an unbounded dumping ground.
_MAX_CHARS = 40_000
# Cap one scratchpad result entry — a step's distilled synthesis, not a raw dump.
_MAX_RESULT_CHARS = 20_000

# How each status reads back to the model (and, via the card, the owner).
_STATUS_LABEL = {
    "not_approved": "awaiting your approval",
    "approved": "approved",
    "in_work": "in work",
}


def _plan_view(
    session_id: str, body: str, status: str, updated_at: str, results: list | None = None
) -> ViewPayload:
    """The inline `plan_card`: the plan body, its status, the step-results scratchpad, and the
    Approve control the owner taps (data-only; the PWA renders the registered component, never
    model markup)."""
    return ViewPayload(
        view="plan_card",
        surface="inline",
        data={
            "session_id": session_id,
            "body": body,
            "status": status,
            "updated_at": updated_at,
            "results": results or [],
        },
    )


def format_plan_results(results: list | None) -> str:
    """The step-results scratchpad rendered as Markdown for the model (read_plan + the per-turn
    re-injection): each append-ordered entry as a numbered, optionally-headed block. Empty when
    nothing has been recorded yet."""
    lines: list[str] = []
    for i, entry in enumerate(results or [], start=1):
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("heading") or "").strip()
        note = str(entry.get("note") or "").strip()
        label = f"{i}. {heading}" if heading else f"Result {i}"
        lines.append(f"### {label}\n{note}" if note else f"### {label}")
    return "\n\n".join(lines)


def build_plan_handlers(maker: async_sessionmaker[AsyncSession]) -> dict[str, ToolHandler]:
    """The read_plan/write_plan tools, bound to the app's sessionmaker. Each runs under
    `ctx.session` (jerv's owner-scoped session), so the owner-only RLS on
    `agent_session_plans` is the firewall."""
    repo = PlanRepo()

    async def read_plan(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if not ctx.agent_session_id:
            return "There's no conversation to attach a plan to here."
        async with scoped_session(maker, ctx.session) as session:
            plan = await repo.get(session, ctx.agent_session_id)
        if plan is None:
            return "(no plan yet — draft one with write_plan when the task needs it)"
        view = _plan_view(
            ctx.agent_session_id,
            plan.body,
            plan.status,
            plan.updated_at.isoformat(),
            results=plan.results,
        )
        text = f"Plan ({_STATUS_LABEL.get(plan.status, plan.status)}):\n\n{plan.body}"
        scratch = format_plan_results(plan.results)
        if scratch:
            text += f"\n\n--- Step results recorded so far ---\n\n{scratch}"
        return ToolOutput(text, view=view)

    async def write_plan(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if not ctx.agent_session_id:
            return "There's no conversation to attach a plan to here."
        body = arguments.get("body")
        status = arguments.get("status")
        pause = arguments.get("pause")
        if body is None and status is None and pause is None:
            return "write_plan needs a body (the plan text), a status, and/or a pause signal."
        if body is not None and len(str(body)) > _MAX_CHARS:
            return (
                f"That plan is too long to store ({len(str(body))} chars, max {_MAX_CHARS}). "
                "Trim it to the essential steps and save again."
            )
        # The state machine — enforced server-side, never trusted to the model: jerv may
        # only ever set `not_approved` or `in_work`. `approved` is the owner's alone (its
        # own api endpoint), so a web page jerv reads can never talk it into self-approving.
        if status == "approved":
            return (
                "Only the owner can approve a plan (by tapping Approve). Set it to "
                "not_approved and ask them to review it — don't mark it approved yourself."
            )

        async with scoped_session(maker, ctx.session) as session:
            current = await repo.get(session, ctx.agent_session_id)
            current_status = current.status if current else "not_approved"

            # `in_work` is only reachable once the owner has approved the plan.
            if status == "in_work" and current_status not in ("approved", "in_work"):
                return (
                    "Can't start work — this plan isn't approved yet. Leave it at "
                    "not_approved and ask the owner to approve it first."
                )
            if pause is not None and current is None and body is None:
                return "There's no plan to pause — draft one first with write_plan(body=…)."

            if body is not None or status is not None:
                plan = await repo.upsert(
                    session,
                    ctx.agent_session_id,
                    body=None if body is None else str(body),
                    status=status,
                )
            else:
                plan = current  # pause-only call
            assert plan is not None
            # Snapshot the view fields now — a pause UPDATE below expires the ORM row, and
            # a pause never changes body/status/results anyway.
            new_body, new_status, updated = plan.body, plan.status, plan.updated_at.isoformat()
            new_results = list(plan.results or [])

            # Apply the continuation opt-out/opt-in. await_owner stops the auto-loop and
            # clears any pending continuation; checkpoint clears the flag so the loop runs.
            # BUT await_owner on a `not_approved` draft is a no-op: a draft is ALREADY waiting
            # for the owner (to approve it), so setting the flag is meaningless and actively
            # harmful — it hides the Approve control on the card and, left set, would block the
            # loop even after approval. The approval gate IS the pause for a draft.
            paused_for_owner = pause == "await_owner" and new_status != "not_approved"
            if paused_for_owner:
                await repo.set_awaiting_owner(session, ctx.agent_session_id, True)
            elif pause == "checkpoint":
                await repo.set_awaiting_owner(session, ctx.agent_session_id, False)

        view = _plan_view(ctx.agent_session_id, new_body, new_status, updated, results=new_results)
        if paused_for_owner:
            note = "Saved — paused for you. I'll wait for your reply before the next step."
        elif new_status == "not_approved":
            note = "Saved the plan (awaiting your approval — tap Approve to sign off)."
            # Teach the model why a requested pause was ignored, so it doesn't re-issue it.
            if pause == "await_owner":
                note += " (A draft already waits for your approval — no need to pause it.)"
        elif new_status == "in_work":
            note = "Plan updated — marked in work."
        else:
            note = f"Plan saved (status: {_STATUS_LABEL.get(new_status, new_status)})."
        return ToolOutput(note, view=view)

    async def write_plan_result(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if not ctx.agent_session_id:
            return "There's no conversation to attach a plan to here."
        note = arguments.get("note")
        if note is None or not str(note).strip():
            return "write_plan_result needs a `note` — the synthesized result of the step."
        note_s = str(note)
        if len(note_s) > _MAX_RESULT_CHARS:
            return (
                f"That result is too long to store ({len(note_s)} chars, max "
                f"{_MAX_RESULT_CHARS}). Distill it to the essential findings and save again."
            )
        heading_raw = arguments.get("heading")
        heading = str(heading_raw).strip() if heading_raw is not None else None
        async with scoped_session(maker, ctx.session) as session:
            plan = await repo.append_result(
                session, ctx.agent_session_id, note=note_s, heading=heading or None
            )
        if plan is None:
            return (
                "There's no plan to record a result on — draft one first with write_plan(body=…)."
            )
        view = _plan_view(
            ctx.agent_session_id,
            plan.body,
            plan.status,
            plan.updated_at.isoformat(),
            results=plan.results,
        )
        return ToolOutput(
            f"Recorded result #{len(plan.results)} on the plan scratchpad.", view=view
        )

    return {
        "read_plan": read_plan,
        "write_plan": write_plan,
        "write_plan_result": write_plan_result,
    }
