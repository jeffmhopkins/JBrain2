"""Unit tests for the check_channel tool handler: title filtering, corpus-dedup, bad input,
and formatting. The channel lister and the corpus dedup are stubbed here (network + DB are
covered by the integration tests)."""

import jbrain.agent.externaltools as externaltools
from jbrain.agent.externaltools import build_external_handlers
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.stream import ChannelVideo, StreamError

_CTX = ToolContext(session=SessionContext(principal_id="owner", principal_kind="owner"), scopes=())


def _uploads(*items: tuple[str, str]) -> list[ChannelVideo]:
    return [
        ChannelVideo(video_id=v, title=t, url=f"https://www.youtube.com/watch?v={v}")
        for v, t in items
    ]


def _handler(uploads, *, raise_exc=None):
    def lister(channel_id, *, limit=10):
        if raise_exc is not None:
            raise raise_exc
        return uploads[:limit]

    return build_external_handlers(object(), object(), lister)["check_channel"]  # type: ignore[arg-type]


async def _fresh(monkeypatch, keep: set[str]):
    async def fake_filter(maker, provider, video_ids, *, principal_id=""):
        return {v for v in video_ids if v in keep}

    monkeypatch.setattr(externaltools, "filter_new_video_ids", fake_filter)


async def test_returns_new_videos_not_in_corpus(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v2"})  # v1 already in the library
    out = await _handler(_uploads(("v1", "Old Recap"), ("v2", "Starship Static Fire")))(
        {"channel_id": "UCabc"}, _CTX
    )
    assert "v2" in out and "Starship Static Fire" in out
    assert "v1" not in out and "1 new video" in out


async def test_title_filter_is_case_insensitive_substring(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1", "v2"})
    out = await _handler(_uploads(("v1", "STARSHIP update"), ("v2", "Falcon 9 launch")))(
        {"channel_id": "UCabc", "title_include": "starship"}, _CTX
    )
    assert "v1" in out and "v2" not in out


async def test_all_already_ingested(monkeypatch) -> None:
    await _fresh(monkeypatch, set())  # nothing fresh
    out = await _handler(_uploads(("v1", "A"), ("v2", "B")))({"channel_id": "UCabc"}, _CTX)
    assert "already in the library" in out


async def test_no_uploads_match_title(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    out = await _handler(_uploads(("v1", "Falcon")))(
        {"channel_id": "UCabc", "title_include": "starship"}, _CTX
    )
    assert "matched" in out


async def test_bad_and_missing_channel_id(monkeypatch) -> None:
    await _fresh(monkeypatch, set())
    h = _handler(_uploads())
    assert "needs a channel_id" in await h({"channel_id": "  "}, _CTX)
    assert "not a URL" in await h({"channel_id": "https://youtube.com/x"}, _CTX)


async def test_lister_error_is_surfaced(monkeypatch) -> None:
    await _fresh(monkeypatch, set())
    out = await _handler([], raise_exc=StreamError("that channel couldn't be listed"))(
        {"channel_id": "UCabc"}, _CTX
    )
    assert out == "that channel couldn't be listed"
