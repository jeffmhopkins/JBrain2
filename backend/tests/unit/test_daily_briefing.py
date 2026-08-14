"""The lean daily-news briefing builder (jbrain.agent.daily_briefing): deterministic per-beat
gather + force-fetch + one tool-less writer, all deps faked (no network, no real model)."""

from __future__ import annotations

from jbrain.agent.daily_briefing import (
    _BRIEFING_BEATS,
    _WRITER_MAX_TOKENS,
    DailyBriefingBuilder,
)
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.llm.types import LlmTurn, LlmUsage, ReasoningChunk, TextChunk
from jbrain.web.feeds import FeedItem
from jbrain.web.fetch import FetchResult
from jbrain.web.search import NewsHit

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

_SECTIONS = [
    "Good Morning",
    "Top National Stories",
    "Business & the Economy",
    "Around the World",
    "Space Industry",
    "Artificial Intelligence & Tech",
    "Around the Space Coast",
    "That's Your Briefing",
]


def _full_report(missing: str | None = None) -> str:
    """A stand-in written briefing: every section heading present (so the completeness check
    passes) with a citation, unless `missing` names a section to omit (to trigger a repair)."""
    out = []
    for s in _SECTIONS:
        if s == missing:
            continue
        out.append(f"## {s}\nSomething happened [^1].")
    return "\n\n".join(out)


class _FakeRouter:
    """Records each writer prompt (and the `max_tokens` it was called with) and STREAMS canned
    report text per call, in order (the writer goes through `converse_stream`): an optional
    reasoning chunk, a text chunk, then the closing turn carrying the authoritative text, stop
    reason, and reasoning trace — the shapes `_write` reads from. `stop_reason` / `reasoning` are
    overridable so the writer-failure (runaway-thinking) path can be exercised."""

    def __init__(
        self, responses: list[str], *, stop_reason: str = "end_turn", reasoning: str = ""
    ) -> None:
        self._responses = responses
        self._stop_reason = stop_reason
        self._reasoning = reasoning
        self.prompts: list[str] = []
        self.max_tokens_seen: list[int] = []

    async def converse_stream(self, task, *, system, messages, max_tokens, **_kw):  # noqa: ANN001
        self.prompts.append(messages[0].text)
        self.max_tokens_seen.append(max_tokens)
        text = self._responses[min(len(self.prompts) - 1, len(self._responses) - 1)]
        if self._reasoning:
            yield ReasoningChunk(text=self._reasoning)
        if text:
            yield TextChunk(text=text)
        yield LlmTurn(
            text=text,
            tool_calls=(),
            stop_reason=self._stop_reason,  # type: ignore[arg-type]
            usage=LlmUsage(10, 20),
            reasoning=self._reasoning,
        )


class _FakeFeeds:
    """Returns full-text FeedItems for space/local, nothing for the summary beats."""

    def __init__(self, by_cat: dict[str, list[FeedItem]]) -> None:
        self._by_cat = by_cat

    async def fetch_category(self, category, *, since="day", limit=8):  # noqa: ANN001
        return list(self._by_cat.get(category, []))


class _FakeSearx:
    def __init__(self, hits: list[NewsHit]) -> None:
        self._hits = hits

    async def search_news(self, query, *, time_range="day", limit=6):  # noqa: ANN001
        return list(self._hits)


class _FakeFetcher:
    """Returns article text for any URL and RECORDS which URLs were fetched (to prove a
    full-text feed item is never re-fetched)."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def fetch(self, url, **_kw):  # noqa: ANN001
        self.fetched.append(url)
        return FetchResult(url=url, title=f"T {url}", text="", full_text=f"BODY of {url}")


def _feed_item(url: str, *, body: str) -> FeedItem:
    return FeedItem(
        title=f"Feed {url}",
        url=url,
        published="Thu, 13 Aug 2026",
        summary="lead",
        body=body,
        source="Feed Outlet",
        epoch=1.0,
    )


def _builder(router, *, feeds=None, searxng=None, fetcher=None) -> DailyBriefingBuilder:  # noqa: ANN001
    return DailyBriefingBuilder(router, feeds=feeds, searxng=searxng, fetcher=fetcher)


async def test_build_gathers_writes_and_marks_full_text_feeds_as_read_without_refetch() -> None:
    feeds = _FakeFeeds(
        {
            "space": [_feed_item("https://nasa.gov/a", body="X" * 500)],
            "local": [_feed_item("https://spacecoastdaily.com/b", body="Y" * 500)],
        }
    )
    searx = _FakeSearx(
        [NewsHit(title="Nat", url="https://npr.org/n", snippet="s", published="Thu")]
    )
    fetcher = _FakeFetcher()
    router = _FakeRouter([_full_report()])
    result = await _builder(router, feeds=feeds, searxng=searx, fetcher=fetcher).build(
        CTX, question="Daily brief", objective="Speak plainly.", sections=_SECTIONS
    )
    # A report came back; every gathered article is a read source; roster mirrors them.
    assert "## Space Industry" in result.report_md and not result.repaired
    assert result.sources and all(s.read for s in result.sources)
    assert len(result.roster) == len(result.sources)
    # The full-text feed items (space + local) were NOT re-fetched; only the summary lead was.
    assert "https://nasa.gov/a" not in fetcher.fetched
    assert "https://spacecoastdaily.com/b" not in fetcher.fetched
    assert "https://npr.org/n" in fetcher.fetched
    # The writer prompt carried the real article bodies under their section headings.
    prompt = router.prompts[0]
    assert (
        "### Space Industry" in prompt
        and "X" * 100 in prompt
        and "BODY of https://npr.org/n" in prompt
    )


async def test_build_fires_the_three_phases_and_streams_the_writer() -> None:
    # The builder drives its OWN progress timeline: Gather (feeds+search) → Read (force-fetch) →
    # Write (streamed). The two deterministic steps carry a status line and no preview; the writer
    # phase streams the accumulating draft into the preview so the PWA renders it live.
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    searx = _FakeSearx([NewsHit(title="N", url="https://npr.org/n", snippet="s", published="Thu")])
    router = _FakeRouter([_full_report()])
    calls: list[tuple[int, str, str | None, str | None]] = []
    result = await _builder(router, feeds=feeds, searxng=searx, fetcher=_FakeFetcher()).build(
        CTX,
        question="q",
        objective="",
        sections=_SECTIONS,
        progress=lambda step, label, preview, reasoning: calls.append(
            (step, label, preview, reasoning)
        ),
    )
    steps = [c[0] for c in calls]
    assert steps[0] == 1 and 2 in steps and steps[-1] == 3  # Gather → Read → Write, in order
    # The deterministic gather/read steps carry a label and no streamed preview.
    assert calls[0][1] and calls[0][2] is None
    assert any(s == 2 and "Reading" in lbl and pv is None for s, lbl, pv, _ in calls)
    # The write phase streams the draft: a step-3 call carries the report text as preview.
    assert any(s == 3 and pv and "## Space Industry" in pv for s, _, pv, _r in calls)
    assert not result.empty


async def test_writer_reasoning_is_streamed_to_the_progress_component() -> None:
    # A hybrid reasoner emits a <think> trace before any visible text. The builder must stream that
    # thinking to the Write phase (the frozen empty pane the owner hit) — a step-3 progress call
    # carries the reasoning tail, and the eventual draft too.
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    router = _FakeRouter([_full_report()], reasoning="I am thinking hard about the news. " * 10)
    calls: list[tuple[int, str, str | None, str | None]] = []
    await _builder(router, feeds=feeds).build(
        CTX,
        question="q",
        objective="",
        sections=_SECTIONS,
        progress=lambda step, label, preview, reasoning: calls.append(
            (step, label, preview, reasoning)
        ),
    )
    # A Write-phase tick surfaced the model's thinking (not just the draft).
    assert any(s == 3 and r and "thinking hard" in r for s, _, _p, r in calls)
    # And the draft still streams on the same phase.
    assert any(s == 3 and p and "## Space Industry" in p for s, _, p, _r in calls)


async def test_writer_budget_is_sized_for_reasoning_headroom() -> None:
    # The writer's max_tokens is passed through and sized well above the old 12k cap, so a hybrid
    # reasoner has room to finish thinking AND still write the briefing (the starvation that
    # produced the empty report).
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    router = _FakeRouter([_full_report()])
    await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert router.max_tokens_seen == [_WRITER_MAX_TOKENS]
    assert _WRITER_MAX_TOKENS >= 24000


async def test_empty_writer_output_fails_loudly_rather_than_shipping_a_hollow_report() -> None:
    # Gather works (a space article), but the writer burns the whole budget on reasoning and returns
    # an EMPTY body on both the initial write and the repair — the runaway-thinking failure. The
    # builder must NOT ship a chars=0 report: it flags writer_failed with a diagnosis naming the
    # cause, distinct from an empty gather.
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    router = _FakeRouter(["", ""], stop_reason="max_tokens", reasoning="think " * 500)
    result = await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert result.writer_failed is True
    assert result.empty is False  # gather was fine — the WRITER failed, a distinct signal
    assert len(router.prompts) == 2  # it still tried the one repair before giving up
    assert "reasoning channel" in result.failure_detail  # the runaway remedy is named
    assert "output_tokens" in result.failure_detail  # the token diagnosis rides along


async def test_writer_with_no_headings_fails_loudly_even_when_body_is_nonempty() -> None:
    # The writer returns prose with NOT ONE section heading, even after the repair (a big think dump
    # with no `##`). All content sections missing == a hollow briefing → writer_failed, not a
    # silently-shipped partial.
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    blob = "I keep thinking and never write a heading. " * 30
    router = _FakeRouter([blob, blob], stop_reason="max_tokens", reasoning="x" * 4000)
    result = await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert result.writer_failed is True and result.failure_detail


async def test_run_briefing_refuses_loudly_on_a_writer_failure() -> None:
    from jbrain.agent.deep_research import DeepResearchService
    from jbrain.agent.research_presets import render_preset

    rp = render_preset("daily_news", {"today": "Thu", "now": "Thu 6am"})
    svc = DeepResearchService(
        router=_FakeRouter(["", ""], stop_reason="max_tokens", reasoning="t" * 3000),  # type: ignore[arg-type]
        spawn=object(),  # type: ignore[arg-type]
        feeds=_FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]}),  # type: ignore[arg-type]
    )
    out = str(await svc._run_briefing(CTX, rp))
    assert "refused" in out.lower()
    assert "writer failed to produce" in out.lower()  # the loud, specific refusal


async def test_missing_section_triggers_exactly_one_repair() -> None:
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    # First draft drops "Around the World"; the repair draft is complete.
    router = _FakeRouter([_full_report(missing="Around the World"), _full_report()])
    result = await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert len(router.prompts) == 2  # one repair pass fired
    assert result.repaired is True
    assert "## Around the World" in result.report_md
    assert "OMITTED these required sections" in router.prompts[1]  # the repair note was appended


async def test_degrades_without_searxng_or_fetcher() -> None:
    # Only full-text feeds wired: the summary beats yield nothing, but space still fills and the
    # writer is called exactly once (no fetch attempts, no crash).
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Q" * 500)]})
    router = _FakeRouter([_full_report()])
    result = await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert len(router.prompts) == 1
    assert [s.url for s in result.sources] == ["https://nasa.gov/a"]


async def test_empty_gather_returns_empty_without_calling_the_writer() -> None:
    # No clients wired → every beat blanks. The builder must NOT spend a writer call on a hollow
    # prompt; it returns empty so the caller can refuse (empty-gather parity with the pipeline).
    router = _FakeRouter([_full_report()])
    result = await _builder(router).build(CTX, question="q", objective="", sections=_SECTIONS)
    assert result.empty is True and result.report_md == "" and result.sources == []
    assert router.prompts == []  # the writer was never called


async def test_run_briefing_refuses_a_totally_empty_gather() -> None:
    from jbrain.agent.deep_research import DeepResearchService
    from jbrain.agent.research_presets import render_preset

    rp = render_preset("daily_news", {"today": "Thu", "now": "Thu 6am"})
    svc = DeepResearchService(router=_FakeRouter([_full_report()]), spawn=object())  # type: ignore[arg-type]
    out = str(await svc._run_briefing(CTX, rp))
    assert "refused" in out.lower()  # a hollow briefing is refused, not shipped


async def test_completeness_check_is_heading_based_and_ampersand_tolerant() -> None:
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    # The writer spells "&" as "and" (a TTS rule) and mentions topics in prose — the heading-based,
    # &-tolerant check must NOT fire a needless repair (the false-positive that doubled the cost).
    good = "\n\n".join(f"## {s.replace('&', 'and')}\nCovered [^1]." for s in _SECTIONS)
    router = _FakeRouter([good])
    result = await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert len(router.prompts) == 1 and result.repaired is False


async def test_completeness_check_catches_a_dropped_heading_despite_prose_mention() -> None:
    feeds = _FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="Z" * 500)]})
    # First draft omits the "## Space Industry" HEADING but mentions "the space industry" in prose
    # (which a whole-body substring test would have been fooled by); the repair draft is complete.
    dropped = (
        "\n\n".join(f"## {s}\nx [^1]" for s in _SECTIONS if s != "Space Industry")
        + "\n\nWe also note the space industry had news."
    )
    router = _FakeRouter([dropped, _full_report()])
    result = await _builder(router, feeds=feeds).build(
        CTX, question="q", objective="", sections=_SECTIONS
    )
    assert len(router.prompts) == 2 and result.repaired is True


def test_canon_folds_utm_tagged_and_www_variants() -> None:
    from jbrain.agent.daily_briefing import _canon

    # A feed URL with an RSS utm tag folds with the bare search URL of the same story.
    assert _canon("https://host.com/story?utm_medium=rss") == _canon("https://host.com/story")
    assert _canon("https://www.host.com/story/") == _canon("https://host.com/story")
    # A meaningful query param is kept (distinct stories stay distinct).
    assert _canon("https://host.com/p?id=1") != _canon("https://host.com/p?id=2")


def test_beats_cover_the_six_content_sections() -> None:
    # The fixed beats map to exactly the content sections (not the greeting/sign-off).
    beat_sections = {b.section for b in _BRIEFING_BEATS}
    assert beat_sections == set(_SECTIONS[1:-1])


async def test_engine_briefing_routes_through_the_builder_and_frames_like_the_pipeline() -> None:
    # An `engine: briefing` preset (daily_news) routes DeepResearchService to the builder, which
    # frames + finalizes the report exactly like a pipeline run (library card / view parity).
    from jbrain.agent.deep_research import DeepResearchService
    from jbrain.agent.research_presets import render_preset

    rp = render_preset(
        "daily_news",
        {"today": "Thursday, August 13, 2026", "now": "Thursday, August 13, 2026 at 6:15 AM EDT"},
    )
    assert rp.engine == "briefing"
    svc = DeepResearchService(
        router=_FakeRouter([_full_report()]),  # type: ignore[arg-type]
        spawn=object(),  # type: ignore[arg-type]  # unused by the briefing path
        feeds=_FakeFeeds({"space": [_feed_item("https://nasa.gov/a", body="X" * 500)]}),  # type: ignore[arg-type]
    )
    out = str(await svc._run_briefing(CTX, rp))
    assert "DEEP RESEARCH REPORT" in out  # framed like a pipeline run
    assert "## Space Industry" in out
    assert "## Sources" in out  # the trailing Sources block was rebuilt from the [^n] markers
