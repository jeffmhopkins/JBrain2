"""A lean, single-writer daily-news briefing builder (docs/plans/DAILY_NEWS_V2_PLAN.md).

An alternative to the fan pipeline for the `daily_news` shape, built on one observation:
now that fetch/extract is reliable (the reader→byparr→Tavily recovery chain un-walls a URL
off-box), GATHERING no longer needs a fan of search-scout + fetch-reader sub-agents. It is a
DETERMINISTIC step — pull the curated feeds, news-search each beat, force-fetch the picks to
clean full text — that spends ZERO model tokens. The only unavoidable model work is WRITING
the briefing over that pre-fetched text.

So this builder does exactly that: deterministic per-beat gather → force-fetch → ONE tool-less
writer call (the same synthesizer prompt the pipeline uses, fed real article text as findings)
→ a deterministic section-completeness check → at most ONE repair call. The writer never holds
a search or fetch tool, so it cannot write from a snippet — the anti-hallucination guarantee is
structural, not prompted. Selected side-by-side with the pipeline via a preset's `engine:
briefing` flag; the existing `daily_news` preset (engine `pipeline`) is untouched.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import structlog

from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext
from jbrain.agent.spawn import _ChildResult
from jbrain.llm import LlmRouter
from jbrain.llm.promptfile import load_prompt
from jbrain.llm.types import UserMessage
from jbrain.web.feeds import FeedClient
from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.search import SearxngClient, WebSearchError

log = structlog.get_logger()

_PROMPTS = Path(__file__).parent / "prompts"
# The writer IS the pipeline's synthesizer — the same, proven "write the report from findings +
# outline + numbered sources, cite [^n]" contract — just fed deterministically-gathered article
# text instead of sub-agent findings. Reused, not forked, so writing quality tracks the pipeline.
_SYNTH = load_prompt(_PROMPTS / "deep_research_synthesize.prompt")
_TASK = "agent.turn"
_WRITER_MAX_TOKENS = 12000

# Per-article text handed to the writer. News is inverted-pyramid — the who/what/when is up top —
# so a few thousand chars carries the facts without ballooning the writer's context (the one
# thing that sets wall-clock on the single local model).
_BRIEF_BODY_CHARS = 3000
# Bounded concurrency for the force-fetch step (network I/O, off the model budget) so the summary
# beats' articles open in parallel instead of serially, without stampeding the fetch tiers.
_FETCH_CONCURRENCY = 6


@dataclass(frozen=True)
class _Beat:
    """One content section's deterministic gather recipe: the news_feed category and the
    news_search query that feed it, and how many articles to open (heavier for the space + AI
    beats, minimal for the strict-admission local beat). `section` must match a heading in the
    preset outline exactly, so the writer's section and this beat's material line up."""

    section: str
    category: str
    query: str
    quota: int


# The six CONTENT beats, mapped to the daily_news section headings. "Good Morning" and "That's
# Your Briefing" carry no beat — the writer composes them from the content per the objective.
_BRIEFING_BEATS: tuple[_Beat, ...] = (
    _Beat("Top National Stories", "national", "top US national news politics government today", 3),
    _Beat(
        "Business & the Economy", "economy", "US markets economy jobs inflation interest rates", 3
    ),
    _Beat("Around the World", "world", "major international world geopolitics news today", 3),
    _Beat("Space Industry", "space", "NASA SpaceX Blue Origin launch space industry news", 5),
    _Beat(
        "Artificial Intelligence & Tech",
        "ai_tech",
        "artificial intelligence AI model release tech news Tesla Musk",
        5,
    ),
    _Beat(
        "Around the Space Coast",
        "local",
        "Titusville Port St. John Brevard County Space Coast Florida news",
        2,
    ),
)


@dataclass(frozen=True)
class _Article:
    """One gathered item: metadata plus the real article `text` (empty until fetched). `read` is
    True once `text` is genuine full article body (from a full-text feed or a successful fetch) —
    the flag the citation registry carries so the writer's source is marked opened, not listed."""

    title: str
    url: str
    published: str
    outlet: str
    text: str
    read: bool


@dataclass(frozen=True)
class BriefingResult:
    """What `build` returns for the caller to persist: the finished spoken briefing, the source
    registry (read=True for opened articles), a synthetic roster (one child per opened article, so
    the stored report's sub-agent count reflects real reads), and whether a repair pass fired."""

    report_md: str
    sources: list[WebSource]
    roster: list[_ChildResult]
    repaired: bool
    # No articles opened for ANY content beat — the caller refuses rather than shipping a hollow
    # "nothing happened anywhere" briefing (the empty-gather parity with the pipeline).
    empty: bool
    # Some (but not all) content beats came back empty — an honest "coverage may be partial" signal
    # the stored report carries, so an infra hiccup that blanked 2 of 6 sections isn't invisible.
    coverage_limited: bool


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _canon(url: str) -> str:
    """A canonical URL key for cross-source dedup — host lowercased (`www.` dropped), trailing
    slash + fragment dropped, and tracking params (utm_*, gclid, fbclid, …) stripped with the rest
    SORTED. The tracking strip is what folds the SAME article surfaced by a feed (which appends
    `?utm_medium=rss`) and by a news search (the bare URL); without it the two survive as
    duplicates and the page is fetched twice and cited twice."""
    try:
        p = urlsplit(url.strip())
        host = (p.hostname or "").lower().removeprefix("www.")
        kept = sorted(
            f"{k}={v}"
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)
        )
        query = "?" + "&".join(kept) if kept else ""
        return f"{host}{p.path.rstrip('/')}{query}"
    except ValueError:
        return url.strip().lower()


# Non-utm tracking params dropped from the dedup key (utm_* is handled by prefix above).
_TRACKING_PARAMS = frozenset({"gclid", "fbclid", "mc_cid", "mc_eid", "igshid", "ref", "cmpid"})


class DailyBriefingBuilder:
    """Build a daily news briefing deterministically-gather-first, single-writer. Dependencies are
    the same on-box clients the tools use; any may be None in a headless/test build (the gather
    just yields fewer candidates), so the builder degrades rather than failing."""

    def __init__(
        self,
        router: LlmRouter,
        *,
        feeds: FeedClient | None = None,
        searxng: SearxngClient | None = None,
        fetcher: WebFetcher | None = None,
    ) -> None:
        self._router = router
        self._feeds = feeds
        self._searxng = searxng
        self._fetcher = fetcher

    async def build(
        self,
        ctx: ToolContext,
        *,
        question: str,
        objective: str,
        sections: list[str],
        on_write: Callable[[], None] | None = None,
    ) -> BriefingResult:
        """Gather each content beat deterministically, force-fetch the picks to real text, write
        the briefing in one call, and repair once if a section heading is missing. `sections` is the
        full preset outline (incl. Good Morning / sign-off); `objective` carries the spoken rules.
        `on_write`, if given, fires when gather is done and the (slow) writer call is about to run —
        the caller uses it to move the phase from Gathering to Writing at the right moment."""
        # 1+2. GATHER (concurrent, model-free) + FETCH (concurrent, model-free): each beat yields a
        # bundle of opened articles. Beats run together, sharing ONE fetch semaphore so total
        # concurrent fetches are bounded globally (not per-beat), since each fetch can escalate to
        # the slow solver tier — 6 beats × a per-beat limit would be a 6× stampede on the box.
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
        bundles = await asyncio.gather(*(self._gather_and_open(b, sem) for b in _BRIEFING_BEATS))
        by_section: dict[str, list[_Article]] = {
            beat.section: arts for beat, arts in zip(_BRIEFING_BEATS, bundles, strict=True)
        }
        # 3. Build the citation registry + the writer's findings block from the opened articles.
        articles = [a for arts in bundles for a in arts]
        sources = [WebSource(url=a.url, title=a.title, read=True) for a in articles]
        index = {id(a): i + 1 for i, a in enumerate(articles)}  # [^n] is positional over sources
        empty_beats = [s for s, arts in by_section.items() if not arts]
        roster = [
            _ChildResult(
                label=(a.title or a.url)[:60],
                persona="research_fetch",
                summary=a.text,
                ok=True,
                session_id="",
                web_sources=(WebSource(url=a.url, title=a.title, read=True),),
            )
            for a in articles
        ]
        # A totally-empty gather (every beat blanked — SearXNG down AND feeds down/absent) must NOT
        # ship a fluent "nothing happened anywhere today" briefing; the caller refuses on `empty`.
        # A partial blank flags coverage_limited so the stored report says so honestly.
        if not articles:
            log.warning("daily_briefing.empty_gather")
            return BriefingResult(
                report_md="",
                sources=[],
                roster=roster,
                repaired=False,
                empty=True,
                coverage_limited=True,
            )
        # 4+5. WRITE — one tool-less call over the real text. The writer cannot search or fetch, so
        # it can only spend the provided article bodies (the structural anti-snippet guarantee).
        prompt = _writer_prompt(question, objective, sections, by_section, index)
        if on_write is not None:
            on_write()
        report = await self._write(prompt)
        # 6. Deterministic completeness check over HEADING LINES (not the whole body — a section's
        # words appear in prose too, which would blind a substring test), normalized so `&`≡"and"
        # (the TTS objective spells symbols out, so the writer renders `Business and the Economy`).
        content_sections = [b.section for b in _BRIEFING_BEATS]
        missing = _missing_sections(report, content_sections)
        repaired = False
        if missing:
            log.info("daily_briefing.repairing", missing=missing)
            report = await self._write(prompt + _repair_note(missing))
            repaired = True
            still = _missing_sections(report, content_sections)
            if still:  # one bounded repair only — don't loop; log the residual for diagnosis
                log.warning("daily_briefing.repair_incomplete", missing=still)
        log.info(
            "daily_briefing.built",
            articles=len(articles),
            repaired=repaired,
            empty_beats=empty_beats,
            chars=len(report),
        )
        return BriefingResult(
            report_md=report,
            sources=sources,
            roster=roster,
            repaired=repaired,
            empty=False,
            coverage_limited=bool(empty_beats),
        )

    async def _gather_and_open(self, beat: _Beat, sem: asyncio.Semaphore) -> list[_Article]:
        """One beat's opened articles: gather candidates (feed + news search, deduped), then
        force-fetch the picks (concurrently, under the SHARED `sem`) to real text — skipping a
        full-text feed item, whose body is already in hand. Returns up to `beat.quota` with text."""
        candidates = await self._candidates(beat)

        async def _open(cand: _Article) -> _Article | None:
            if cand.read and cand.text.strip():
                return replace(cand, text=cand.text[:_BRIEF_BODY_CHARS])  # full-text feed: no fetch
            if self._fetcher is None:
                return None
            async with sem:
                try:
                    result = await self._fetcher.fetch(cand.url)
                except WebFetchError as exc:
                    log.info("daily_briefing.fetch_skipped", url=cand.url, error=repr(exc))
                    return None
                except Exception as exc:  # noqa: BLE001 - an off-contract raise (e.g. a malformed
                    # feed link) skips that one lead rather than sinking the whole concurrent run.
                    log.warning("daily_briefing.fetch_error", url=cand.url, error=repr(exc))
                    return None
            text = (result.full_text or result.text or "").strip()
            if not text:
                return None
            return replace(cand, text=text[:_BRIEF_BODY_CHARS], read=True)

        # Try more candidates than the quota so fetch failures don't starve the section, but keep
        # only the first `quota` that opened — order preserves the deterministic candidate ranking.
        opened = await asyncio.gather(*(_open(c) for c in candidates[: beat.quota * 3]))
        return [a for a in opened if a is not None][: beat.quota]

    async def _candidates(self, beat: _Beat) -> list[_Article]:
        """Deduped candidate articles for a beat: the curated feed items FIRST (full-text feeds put
        real body in hand for free), then news-search leads. Both filtered to the day by their
        source. A client that errors or is absent simply contributes nothing (best-effort)."""
        out: list[_Article] = []
        seen: set[str] = set()
        if self._feeds is not None:
            try:
                items = await self._feeds.fetch_category(
                    beat.category, since="day", limit=beat.quota * 3
                )
            except Exception:  # noqa: BLE001 - a feed hiccup never sinks the beat
                log.warning("daily_briefing.feed_failed", category=beat.category, exc_info=True)
                items = []
            for it in items:
                key = _canon(it.url)
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(
                    _Article(
                        title=it.title,
                        url=it.url,
                        published=it.published,
                        outlet=it.source or _host(it.url),
                        text=it.body,
                        read=it.has_full_body,
                    )
                )
        if self._searxng is not None:
            try:
                hits = await self._searxng.search_news(
                    beat.query, time_range="day", limit=beat.quota * 3
                )
            except WebSearchError as exc:
                log.info("daily_briefing.news_search_failed", query=beat.query, error=repr(exc))
                hits = []
            except Exception as exc:  # noqa: BLE001 - search is best-effort; an off-contract raise
                # contributes nothing rather than crashing the whole concurrent gather.
                log.warning("daily_briefing.news_search_error", query=beat.query, error=repr(exc))
                hits = []
            for h in hits:
                key = _canon(h.url)
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(
                    _Article(
                        title=h.title,
                        url=h.url,
                        published=h.published,
                        outlet=_host(h.url),
                        text="",
                        read=False,
                    )
                )
        return out

    async def _write(self, user_text: str) -> str:
        """One writer turn through the LLM adapter (never a provider SDK directly, invariant #1),
        using the pipeline's synthesizer system prompt. Non-streaming: the briefing is one bounded
        artifact, so the extra streaming plumbing isn't needed here."""
        turn = await self._router.converse(
            _TASK,
            system=_SYNTH.render(),
            messages=[UserMessage(text=user_text)],
            max_tokens=_WRITER_MAX_TOKENS,
        )
        return (turn.text or "").strip()


def _writer_prompt(
    question: str,
    objective: str,
    sections: list[str],
    by_section: dict[str, list[_Article]],
    index: dict[int, int],
) -> str:
    """The single writer prompt: the spoken-format objective, the fixed outline, the real article
    text grouped by section (each tagged with its [^n]), and the numbered SOURCES the writer cites
    against. Mirrors the pipeline synthesizer's message shape so the reused system prompt fits."""
    parts: list[str] = []
    if objective.strip():
        parts.append(f"OBJECTIVE — produce THIS:\n{objective.strip()}\n")
    parts.append(f"Question:\n{question}\n")
    outline = "\n".join(f"- {s}" for s in sections)
    parts.append(
        "Write the briefing using EXACTLY these section headings, in order (`## Heading`):\n"
        f"{outline}\n"
    )
    parts.append(
        "Base every factual claim ONLY on the article text below — do NOT use outside knowledge —"
        " and cite it with [^n] against the SOURCES list. Get the STATUS/TENSE right: a scheduled"
        " launch or an announced plan is NOT a completed event. Synthesize each story neutrally"
        " across the sources given. Write the Good Morning greeting and the sign-off from the"
        " material. If a section has no articles below, say in one short sentence that there is"
        " nothing new there today and move on.\n"
    )
    lines: list[str] = ["Article text by section:"]
    for section, arts in by_section.items():
        lines.append(f"\n### {section}")
        if not arts:
            lines.append("(no articles gathered for this section)")
            continue
        for a in arts:
            n = index[id(a)]
            when = a.published or "no date"
            lines.append(f"\n[^{n}] {a.title} — {a.outlet}, {when}\n{a.text}")
    parts.append("\n".join(lines))
    srcs = "\n".join(
        f"[^{index[id(a)]}] {a.title or a.url} — {a.url}"
        for arts in by_section.values()
        for a in arts
    )
    if srcs:
        parts.append("\nSOURCES — cite with these exact numbers:\n" + srcs)
    return "\n".join(parts)


def _normalize_heading(text: str) -> str:
    """A heading matched loosely: `&`→"and", non-alphanumerics dropped, lowercased. So the section
    `Business & the Economy` matches the writer's TTS rendering `Business and the Economy`, and a
    stray colon/emphasis in the heading line doesn't cause a false miss."""
    return re.sub(r"[^a-z0-9]+", "", text.lower().replace("&", "and"))


def _missing_sections(report: str, sections: list[str]) -> list[str]:
    """The content sections whose heading is absent — checked against MARKDOWN HEADING LINES only
    (a section's words appear in prose too, so a whole-body substring test is blind to a dropped
    heading), normalized so `&`≡"and". A section is present if its normalized name is a substring
    of some heading line's normalized text."""
    heads = [_normalize_heading(ln) for ln in report.splitlines() if ln.lstrip().startswith("#")]
    return [s for s in sections if not any(_normalize_heading(s) in h for h in heads)]


def _repair_note(missing: list[str]) -> str:
    """Appended to the writer prompt for the one repair pass when a section heading was dropped."""
    names = ", ".join(f'"{s}"' for s in missing)
    return (
        f"\n\nYOUR PREVIOUS DRAFT OMITTED these required sections: {names}. Rewrite the FULL"
        " briefing with EVERY section heading present and in order. For a section with no"
        " articles above, include the heading and say in one short sentence there is nothing new"
        " there today."
    )
