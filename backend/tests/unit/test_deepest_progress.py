"""The deepest-run progress channel (DEEPEST_RESEARCH_TOOL_PLAN.md, R6): per-round and
completion ticks into the initiating chat + an owner nudge, over the proven off-turn
paths. Proven with fakes — no DB, no real bus — so the assertions are the contract: a
durable assistant turn (server-authored, no user bubble), a NotifyBus nudge deep-linked
by session id, an FCM poke, and best-effort semantics (a failure never propagates)."""

import uuid
from typing import Any

from jbrain.agent.deepest_progress import NOTIFY_KIND, DeepestProgressChannel
from jbrain.db.session import SessionContext

OWNER = SessionContext(principal_id="owner-1", principal_kind="owner")


class _FakeTranscript:
    def __init__(self, *, boom: bool = False) -> None:
        self.answers: list[dict[str, Any]] = []
        self._boom = boom

    async def record_answer(  # noqa: ANN001, ANN003
        self, ctx, *, session_id, run_id, assistant_text, tools, reasoning=""
    ):
        if self._boom:
            raise RuntimeError("transcript down")
        # Mirror the real store's coercion (transcript_store.record_answer does
        # `uuid.UUID(run_id)`): a non-UUID run_id — e.g. the lane's "deepest-<uuid>" key —
        # must raise here, not be quietly swallowed, so the regression that dropped every
        # progress turn can never come back unnoticed.
        if run_id is not None:
            uuid.UUID(run_id)
        self.answers.append(
            {
                "session_id": session_id,
                "run_id": run_id,
                "text": assistant_text,
                "kind": "assistant",
                "tools": list(tools),
            }
        )


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, note: Any) -> None:
        self.published.append(note)


class _FakePush:
    def __init__(self) -> None:
        self.pokes: list[list[str]] = []

    async def poke(self, tokens: list[str]) -> None:
        self.pokes.append(list(tokens))


def _channel(**kw: Any) -> tuple[DeepestProgressChannel, _FakeTranscript, _FakeBus, _FakePush]:
    tr, bus, push = _FakeTranscript(**kw), _FakeBus(), _FakePush()
    ch = DeepestProgressChannel(transcript=tr, notify=bus, push=push, push_tokens=["tok-1"])  # type: ignore[arg-type]
    return ch, tr, bus, push


async def test_round_posts_a_server_authored_turn_and_nudges() -> None:
    ch, tr, bus, push = _channel()
    await ch.round(
        OWNER,
        session_id="sess-1",
        run_id="run-1",
        round_no=3,
        findings=42,
        coverage_label="70% covered",
    )
    # (1) a durable assistant-only turn into the initiating session (no user bubble).
    assert len(tr.answers) == 1
    ans = tr.answers[0]
    # run_id is None: these are server-authored turns with no agent run, and the lane's
    # "run-1"-style key is not an `app.runs` UUID (feeding it here dropped every turn).
    assert ans["session_id"] == "sess-1" and ans["run_id"] is None and ans["kind"] == "assistant"
    assert "round 3" in ans["text"] and "42" in ans["text"] and "still going" in ans["text"]
    # The deepest_run tool-view rides the turn so it replays as the timeline card on reload.
    view = ans["tools"][0]["view"]
    assert view["view"] == "deepest_run"
    assert view["data"]["round"] == 3 and view["data"]["status"] == "running"
    # (2) a NotifyBus nudge deep-linked to the chat by session id.
    assert len(bus.published) == 1
    note = bus.published[0]
    assert note.kind == NOTIFY_KIND and note.ref == "sess-1"
    # (3) an FCM poke to wake a closed app.
    assert push.pokes == [["tok-1"]]


async def test_done_announces_completion() -> None:
    ch, tr, bus, _ = _channel()
    await ch.done(OWNER, session_id="sess-1", run_id="run-1", question="how does X work")
    assert len(tr.answers) == 1 and "complete" in tr.answers[0]["text"]
    assert "how does X work" in tr.answers[0]["text"]
    assert bus.published[0].ref == "sess-1"


async def test_progress_is_best_effort_a_transcript_failure_never_propagates() -> None:
    """A transcript write that raises is swallowed — and the nudge still fires, so a
    background run never crashes or stalls on a progress hiccup."""
    ch, _, bus, push = _channel(boom=True)
    await ch.round(OWNER, session_id="s", run_id="r", round_no=1, findings=1, coverage_label="thin")
    assert bus.published and push.pokes  # the nudge + poke still went out


async def test_no_push_tokens_means_no_poke() -> None:
    tr, bus, push = _FakeTranscript(), _FakeBus(), _FakePush()
    ch = DeepestProgressChannel(transcript=tr, notify=bus, push=push, push_tokens=[])  # type: ignore[arg-type]
    await ch.round(OWNER, session_id="s", run_id="r", round_no=1, findings=1, coverage_label="x")
    assert push.pokes == []  # no tokens → no poke, but the turn + nudge still happen
    assert tr.answers and bus.published


async def test_none_transports_are_a_noop() -> None:
    """A run with no transcript/notify/push configured degrades to a clean no-op."""
    ch = DeepestProgressChannel(transcript=None, notify=None, push=None)
    await ch.round(OWNER, session_id="s", run_id="r", round_no=1, findings=0, coverage_label="none")
    await ch.done(OWNER, session_id="s", run_id="r", question="q")  # no crash
