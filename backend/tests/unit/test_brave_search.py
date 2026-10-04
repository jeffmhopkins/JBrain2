"""Brave Search as web_search's metered middle tier (docs/plans/BROWSER_AGENT_PLAN.md B3): the
client's request + parsing, its failure mapping, the monthly budget gate, and the SearXNG → Brave
→ Tavily chain. HTTP is faked via MockTransport — no live network."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jbrain.web.search import (
    SEARXNG_CHAIN_TIMEOUT_S,
    BraveConfig,
    BraveSearch,
    BraveUsage,
    HostedOutcome,
    SearchHit,
    SearchOptions,
    SearxngClient,
    WebSearchError,
    usage_count,
    utc_month,
)

_BRAVE_OK = {
    "type": "search",
    "web": {
        "results": [
            {
                "title": "<strong>EPIC</strong> Theatres &amp; Titusville",
                "url": "https://epictheatres.example/t",
                "description": "Now <strong>playing</strong> at EPIC",
                "page_age": "2026-10-01T00:00:00",
                "age": "3 days ago",
            },
            {
                "title": "",
                "url": "https://b.example/2",
                "description": "second",
                "age": "October 1, 2026",
            },
            {"title": "no url", "url": "", "description": "dropped"},
            "junk row",
        ]
    },
}
_SEARX_THIN = {"results": [{"title": "A", "url": "https://a.example/1", "content": "x"}]}
_SEARX_FULL = {
    "results": [
        {"title": t, "url": f"https://{t}.example/", "content": t, "engines": ["bing", "mojeek"]}
        for t in ("a", "b", "c", "d")
    ],
}


class _Box:
    """The Brave tier's live settings + its persisted usage/error, as plain fields."""

    def __init__(self, *, enabled: bool = True, key: str = "brv-k", budget: int = 900) -> None:
        self.enabled = enabled
        self.key = key
        self.budget = budget
        self.month = "2026-10"
        self.usage: object = None
        self.errors: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []

    async def settings(self) -> BraveConfig:
        return BraveConfig(self.enabled, self.key, self.budget)

    async def load(self) -> object:
        await asyncio.sleep(0)  # yield, so concurrent reservations really interleave
        return self.usage

    async def save(self, record: dict[str, object]) -> None:
        await asyncio.sleep(0)
        self.usage = record

    async def save_error(self, record: dict[str, str]) -> None:
        self.errors.append(record)

    def client(self, handler: Any, **kw: Any) -> BraveSearch:
        def spy(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        month = lambda: self.month  # noqa: E731
        return BraveSearch(
            kw.pop("base_url", "https://api.brave.example"),
            self.settings,
            BraveUsage(self.load, self.save, month=month),
            httpx.MockTransport(spy),
            save_error=self.save_error,
            month=month,
            **kw,
        )

    @property
    def count(self) -> int:
        return usage_count(self.usage, self.month)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_BRAVE_OK)


# --- the request and the parse ---------------------------------------------------------------


async def test_sends_the_key_as_a_header_and_parses_web_results() -> None:
    box = _Box()
    out = await box.client(_ok).search("epic titusville", 5, time_range="day")
    req = box.requests[0]
    assert req.method == "GET" and req.url.path == "/res/v1/web/search"
    assert req.headers["X-Subscription-Token"] == "brv-k"
    assert req.headers["Accept"] == "application/json"
    assert dict(req.url.params) == {
        "q": "epic titusville",
        "count": "5",
        "safesearch": "moderate",
        "freshness": "pd",
    }
    assert "brv-k" not in str(req.url)  # the key travels only in the header
    assert out.failure == ""
    assert out.hits == [
        SearchHit(
            "EPIC Theatres & Titusville",
            "https://epictheatres.example/t",
            "Now playing at EPIC",
            "2026-10-01T00:00:00",
        ),
        SearchHit("https://b.example/2", "https://b.example/2", "second", "October 1, 2026"),
    ]


@pytest.mark.parametrize(("window", "code"), [("week", "pw"), ("month", "pm"), ("year", "py")])
async def test_maps_the_recency_window_to_brave_freshness(window: str, code: str) -> None:
    box = _Box()
    await box.client(_ok).search("q", 3, time_range=window)
    assert box.requests[0].url.params["freshness"] == code


async def test_no_window_sends_no_freshness_and_site_filters_ride_the_query() -> None:
    box = _Box()
    opts = SearchOptions(include_domains=("a.com",), exclude_domains=("c.net",))
    await box.client(_ok).search("showtimes", 50, time_range="fortnight", options=opts)
    params = box.requests[0].url.params
    assert "freshness" not in params
    assert params["q"] == "showtimes site:a.com -site:c.net"
    assert params["count"] == "20"  # Brave's ceiling, whatever the caller asked for


async def test_hits_are_capped_at_the_limit() -> None:
    out = await _Box().client(_ok).search("q", 1)
    assert [h.url for h in out.hits] == ["https://epictheatres.example/t"]


async def test_a_body_without_web_results_is_an_empty_answer() -> None:
    box = _Box()
    out = await box.client(lambda r: httpx.Response(200, json={"type": "search"})).search("q", 3)
    assert out == HostedOutcome([]) and box.count == 1  # it was sent, so it was counted


# --- off, keyless, unwired --------------------------------------------------------------------


async def test_off_keyless_or_unwired_sends_nothing_and_counts_nothing() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call may leave the box")

    for box in (_Box(enabled=False), _Box(key="")):
        assert await box.client(boom).search("q", 3) == HostedOutcome([])
        assert box.usage is None
    box = _Box()
    unwired = box.client(boom, base_url="")
    assert not unwired.wired and await unwired.search("q", 3) == HostedOutcome([])


async def test_unreadable_settings_skip_the_tier() -> None:
    async def broken() -> BraveConfig:
        raise RuntimeError("db down")

    box = _Box()
    client = BraveSearch(
        "https://api.brave.example",
        broken,
        BraveUsage(box.load, box.save),
        httpx.MockTransport(_ok),
    )
    assert await client.search("q", 3) == HostedOutcome([])
    assert await client.probe() == (False, 0, "Couldn't read the Brave settings — try again.")


# --- the monthly budget -----------------------------------------------------------------------


async def test_each_sent_query_is_counted_and_a_cached_repeat_is_free() -> None:
    box = _Box()
    client = box.client(_ok)
    await client.search("q", 3)
    await client.search("q", 3)  # the one-hour cache answers
    await client.search("other", 3)
    assert len(box.requests) == 2 and box.usage == {"month": "2026-10", "count": 2}


async def test_at_the_budget_brave_is_skipped_silently_for_the_month() -> None:
    box = _Box(budget=2)
    box.usage = {"month": "2026-10", "count": 2}
    out = await box.client(_ok).search("q", 3)
    assert out == HostedOutcome([]) and box.requests == []  # the owner's stop, not a failure


async def test_a_new_month_starts_the_count_over() -> None:
    box = _Box(budget=900)
    box.usage = {"month": "2026-09", "count": 900}
    await box.client(_ok).search("q", 3)
    assert len(box.requests) == 1 and box.usage == {"month": "2026-10", "count": 1}


async def test_concurrent_searches_never_overspend_the_budget() -> None:
    """A research fan fires many searches at once: each must reserve its query atomically, or
    two that both read count = budget - 1 would both spend."""
    box = _Box(budget=5)
    box.usage = {"month": "2026-10", "count": 0}
    client = box.client(_ok, cache_ttl_s=0)
    await asyncio.gather(*(client.search(f"q{i}", 3) for i in range(20)))
    assert len(box.requests) == 5 and box.count == 5


async def test_a_usage_store_failure_fails_closed() -> None:
    async def load() -> object:
        raise RuntimeError("db down")

    async def save(record: dict[str, object]) -> None:
        raise AssertionError("unreachable")

    usage = BraveUsage(load, save)
    assert await usage.reserve(900) is False
    box = _Box()
    client = BraveSearch("https://api.brave.example", box.settings, usage, httpx.MockTransport(_ok))
    assert await client.search("q", 3) == HostedOutcome([])


def test_usage_count_reads_only_this_months_well_formed_record() -> None:
    assert usage_count({"month": "2026-10", "count": 7}, "2026-10") == 7
    assert usage_count({"month": "2026-09", "count": 7}, "2026-10") == 0
    for junk in (None, "x", {"month": "2026-10"}, {"month": "2026-10", "count": -1}):
        assert usage_count(junk, "2026-10") == 0
    assert usage_count({"month": "2026-10", "count": True}, "2026-10") == 0


def test_utc_month_is_year_dash_month() -> None:
    from datetime import UTC, datetime

    assert utc_month(datetime(2026, 1, 31, 23, 59, tzinfo=UTC)) == "2026-01"
    assert len(utc_month()) == 7


# --- failure mapping --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_is_recorded_and_not_retried_until_it_changes(status: int) -> None:
    box = _Box()
    client = box.client(lambda r: httpx.Response(status))
    first = await client.search("a", 3)
    assert first.failure.startswith(f"Brave rejected the API key (HTTP {status})")
    assert box.errors[-1]["detail"] == first.failure and box.errors[-1]["at"]
    second = await client.search("b", 3)
    assert second.failure == "Brave rejected the API key" and len(box.requests) == 1
    box.key = "brv-new"  # the owner saves a fresh key in Settings: it is tried at once
    await client.search("c", 3)
    assert len(box.requests) == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(402, {}), (429, {"error": {"code": "QUOTA_LIMITED", "status": 429}})],
)
async def test_a_spent_plan_skips_brave_for_the_rest_of_the_month(status: int, body: dict) -> None:
    box = _Box()
    client = box.client(lambda r: httpx.Response(status, json=body))
    first = await client.search("a", 3)
    assert "plan credit is used up" in first.failure and f"HTTP {status}" in first.failure
    assert "plan credit" in box.errors[-1]["detail"]
    second = await client.search("b", 3)
    assert "used up" in second.failure and len(box.requests) == 1
    box.month = "2026-11"
    await client.search("c", 3)
    assert len(box.requests) == 2


async def test_a_rate_limit_skips_only_this_query_and_records_nothing() -> None:
    box = _Box()
    client = box.client(lambda r: httpx.Response(429, json={"error": {"code": "RATE_LIMITED"}}))
    out = await client.search("a", 3)
    assert out.failure == "Brave is rate-limiting this box (HTTP 429)" and box.errors == []
    await client.search("b", 3)
    assert len(box.requests) == 2


async def test_a_rate_limit_with_an_unreadable_body_is_still_a_rate_limit() -> None:
    box = _Box()
    out = await box.client(lambda r: httpx.Response(429, text="slow down")).search("a", 3)
    assert "rate-limiting" in out.failure


async def test_other_errors_and_transport_failures_read_as_themselves() -> None:
    box = _Box()
    assert (await box.client(lambda r: httpx.Response(500)).search("a", 3)).failure == (
        "Brave returned HTTP 500"
    )
    assert box.errors[-1]["detail"] == "Brave returned HTTP 500"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    assert (await box.client(down).search("b", 3)).failure == "Brave could not be reached"
    bad = await box.client(lambda r: httpx.Response(200, content=b"not json")).search("c", 3)
    assert bad.failure == "Brave could not be reached"


async def test_a_success_clears_a_recorded_error_once() -> None:
    box = _Box()
    responses = iter([httpx.Response(500), httpx.Response(200, json=_BRAVE_OK)])
    client = box.client(lambda r: next(responses), cache_ttl_s=0)
    await client.search("a", 3)
    await client.search("b", 3)
    assert box.errors[-1] == {"detail": "", "at": ""}
    writes = len(box.errors)
    responses = iter([httpx.Response(200, json=_BRAVE_OK)])
    await client.search("c", 3)
    assert len(box.errors) == writes  # already clear: no write per search


async def test_error_bookkeeping_never_breaks_a_search() -> None:
    async def save_error(record: dict[str, str]) -> None:
        raise RuntimeError("db down")

    box = _Box()
    client = BraveSearch(
        "https://api.brave.example",
        box.settings,
        BraveUsage(box.load, box.save),
        httpx.MockTransport(lambda r: httpx.Response(500)),
        save_error=save_error,
    )
    assert (await client.search("q", 3)).failure == "Brave returned HTTP 500"
    unrecorded = BraveSearch(
        "https://api.brave.example",
        box.settings,
        BraveUsage(box.load, box.save),
        httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert (await unrecorded.search("q", 3)).failure == "Brave returned HTTP 500"


_TOKEN_INVALID = {
    "error": {
        "code": "SUBSCRIPTION_TOKEN_INVALID",
        "detail": "The provided subscription token is invalid.",
        "meta": {"component": "authentication"},
        "status": 422,
    },
    "type": "ErrorResponse",
}


async def test_brave_s_real_invalid_token_answer_is_a_dead_key() -> None:
    """Observed live 2026-10-04: Brave answers a bad token with a 422, not a 401."""
    box = _Box()
    client = box.client(lambda r: httpx.Response(422, json=_TOKEN_INVALID))
    first = await client.search("a", 3)
    assert first.failure.startswith(
        "Brave rejected the API key (HTTP 422, SUBSCRIPTION_TOKEN_INVALID)"
    )
    assert "provided subscription token" not in first.failure  # Brave's prose stays out
    assert box.errors[-1]["detail"] == first.failure
    assert client.blocked("brv-k") == "key_rejected" and client.blocked("brv-other") == ""
    assert (await client.search("b", 3)).failure == "Brave rejected the API key"
    assert len(box.requests) == 1


async def test_a_token_code_on_any_status_is_a_dead_key_but_a_422_for_another_reason_is_not() -> (
    None
):
    box = _Box()
    body = {"error": {"code": "SUBSCRIPTION_TOKEN_EXPIRED"}}
    client = box.client(lambda r: httpx.Response(400, json=body))
    assert "rejected the API key" in (await client.search("a", 3)).failure
    other = _Box()
    bad_param = other.client(lambda r: httpx.Response(422, json={"error": {"code": "VALIDATION"}}))
    out = await bad_param.search("a", 3)
    assert out.failure == "Brave returned HTTP 422, VALIDATION"
    assert bad_param.blocked("brv-k") == ""
    bare = _Box().client(lambda r: httpx.Response(422))
    await bare.search("a", 3)
    assert bare.blocked("brv-k") == "key_rejected"


async def test_any_other_429_code_spends_the_month_and_records_the_code() -> None:
    box = _Box()
    body = {"error": {"code": "USAGE_LIMIT_EXCEEDED", "detail": "secret-ish prose"}}
    client = box.client(lambda r: httpx.Response(429, json=body))
    out = await client.search("a", 3)
    assert out.failure == (
        "Brave's plan credit is used up for this month (HTTP 429, USAGE_LIMIT_EXCEEDED)"
    )
    assert "USAGE_LIMIT_EXCEEDED" in box.errors[-1]["detail"]
    assert client.blocked("brv-k") == "credit_spent"
    box.month = "2026-11"
    assert client.blocked("brv-k") == ""


async def test_switching_brave_off_takes_effect_before_its_cache() -> None:
    box = _Box()
    client = box.client(_ok)
    assert (await client.search("q", 3)).hits
    box.enabled = False
    assert await client.search("q", 3) == HostedOutcome([])
    box.enabled, box.key = True, ""
    assert await client.search("q", 3) == HostedOutcome([])


# --- the Settings "Test key" probe ------------------------------------------------------------


async def test_probe_spends_one_counted_query_and_says_the_key_works() -> None:
    box = _Box()
    ok, hits, detail = await box.client(_ok).probe()
    assert ok and hits == 2 and "key works" in detail
    assert box.requests[0].url.params["q"] == "test" and box.count == 1


async def test_probe_reports_each_reason_it_did_not_run() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call")

    assert (await _Box().client(boom, base_url="").probe())[2].startswith("The Brave tier isn't")
    assert "No Brave API key" in (await _Box(key="").client(boom).probe())[2]
    assert "switched off" in (await _Box(enabled=False).client(boom).probe())[2]
    spent = _Box(budget=3)
    spent.usage = {"month": "2026-10", "count": 3}
    assert await spent.client(boom).probe() == (
        False,
        0,
        "This month's budget of 3 queries is used up.",
    )


async def test_probe_tries_a_remembered_dead_key_and_reports_the_rejection() -> None:
    box = _Box()
    responses = iter([httpx.Response(401)] * 2 + [httpx.Response(200, json=_BRAVE_OK)] * 2)
    client = box.client(lambda r: next(responses), cache_ttl_s=0)
    await client.search("a", 3)
    ok, _, detail = await client.probe()  # bypasses the memory: the owner is re-checking
    assert not ok and "rejected the API key (HTTP 401)" in detail
    assert (await client.probe())[0] is True
    await client.search("b", 3)  # the memory was cleared by the success
    assert len(box.requests) == 4


# --- the chain: SearXNG → Brave → Tavily ------------------------------------------------------


def _tier(outcome: HostedOutcome):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    async def tier(
        query: str, limit: int, *, time_range: str = "", options: object = None
    ) -> HostedOutcome:
        calls.append(query)
        return outcome

    return tier, calls


def _chain(
    searx: dict[str, Any] | Callable[[httpx.Request], httpx.Response], **tiers: Any
) -> SearxngClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return searx(request) if callable(searx) else httpx.Response(200, json=searx)

    return SearxngClient("http://searxng:8080", httpx.MockTransport(handler), **tiers)


async def test_a_thin_searxng_answer_goes_to_brave_and_tavily_is_not_asked() -> None:
    box = _Box()
    tavily, tavily_calls = _tier(HostedOutcome([SearchHit("T", "https://t.example/", "s")]))
    body = {**_SEARX_THIN, "answers": ["42"]}
    result = await _chain(body, brave=box.client(_ok).search, hosted=tavily).search("q", 6)
    assert result.source == "brave" and tavily_calls == []
    assert result.hits[0].url == "https://epictheatres.example/t"
    assert result.answers == ("42",)  # SearXNG's zero-click extras still ride along


async def test_a_full_searxng_answer_spends_nothing() -> None:
    box = _Box()
    result = await _chain(_SEARX_FULL, brave=box.client(_ok).search).search("q", 6)
    assert result.source == "searxng" and box.requests == [] and box.usage is None


async def test_a_limit_below_the_threshold_is_satisfied_by_that_many_hits() -> None:
    box = _Box()
    body = {"results": _SEARX_FULL["results"][:2]}
    result = await _chain(body, brave=box.client(_ok).search).search("q", 2)
    assert result.source == "searxng" and box.requests == []


async def test_a_degraded_searxng_answer_goes_to_brave_however_many_hits() -> None:
    box = _Box()
    body = {
        "results": [
            {"title": t, "url": f"https://{t}.example/", "content": t, "engines": ["bing"]}
            for t in ("a", "b", "c", "d")
        ],
        "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["qwant", "403"]],
    }
    result = await _chain(body, brave=box.client(_ok).search).search("epic", 6)
    assert result.source == "brave" and not result.degraded


async def test_brave_over_budget_hands_on_to_tavily() -> None:
    box = _Box(budget=1)
    box.usage = {"month": "2026-10", "count": 1}
    tavily, calls = _tier(HostedOutcome([SearchHit("T", "https://t.example/", "s")]))
    result = await _chain(_SEARX_THIN, brave=box.client(_ok).search, hosted=tavily).search("q")
    assert result.source == "tavily" and calls == ["q"] and box.requests == []
    assert result.hosted_failure == ""


async def test_a_failed_brave_then_a_tavily_answer_carries_no_note() -> None:
    box = _Box()
    tavily, _ = _tier(HostedOutcome([SearchHit("T", "https://t.example/", "s")]))
    brave = box.client(lambda r: httpx.Response(500)).search
    result = await _chain(_SEARX_THIN, brave=brave, hosted=tavily).search("q")
    assert result.source == "tavily" and result.hosted_failure == ""


async def test_every_tier_failing_returns_searxngs_thin_answer_with_the_reasons() -> None:
    box = _Box()
    tavily, _ = _tier(HostedOutcome([], "Tavily's plan credit limit is used up (HTTP 432)"))
    brave = box.client(lambda r: httpx.Response(500)).search
    result = await _chain(_SEARX_THIN, brave=brave, hosted=tavily).search("q")
    assert result.source == "searxng" and [h.url for h in result.hits] == ["https://a.example/1"]
    assert result.hosted_failure == (
        "Brave returned HTTP 500; Tavily's plan credit limit is used up (HTTP 432)"
    )


async def test_all_tiers_disabled_keeps_the_plain_searxng_behaviour() -> None:
    off = _Box(enabled=False)
    tavily, _ = _tier(HostedOutcome([]))  # off / keyless reads as an empty outcome
    result = await _chain(_SEARX_THIN, brave=off.client(_ok).search, hosted=tavily).search("q")
    assert result.source == "searxng" and result.hosted_failure == ""
    assert [h.url for h in result.hits] == ["https://a.example/1"] and off.requests == []


async def test_searxng_down_falls_to_brave() -> None:
    box = _Box()
    result = await _chain(lambda r: httpx.Response(502), brave=box.client(_ok).search).search("q")
    assert result.source == "brave" and result.infobox is None and result.answers == ()


async def test_searxng_down_and_every_tier_failing_names_all_the_reasons() -> None:
    box = _Box()
    tavily, _ = _tier(HostedOutcome([], "Tavily could not be reached"))
    brave = box.client(lambda r: httpx.Response(500)).search
    client = _chain(lambda r: httpx.Response(502), brave=brave, hosted=tavily)
    with pytest.raises(WebSearchError) as err:
        await client.search("q")
    assert "unavailable" in str(err.value)
    assert "Brave returned HTTP 500; Tavily could not be reached" in str(err.value)


async def test_searxng_gets_a_shorter_wait_only_when_a_tier_stands_behind_it() -> None:
    waits: list[float] = []

    def searx(request: httpx.Request) -> httpx.Response:
        waits.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_SEARX_FULL)

    tier, _ = _tier(HostedOutcome([]))
    await _chain(searx, brave=tier).search("q")
    await _chain(searx).search("q")
    assert waits == [SEARXNG_CHAIN_TIMEOUT_S, 15.0] and SEARXNG_CHAIN_TIMEOUT_S < 15.0


async def test_searxng_down_and_no_tier_answering_raises_searxngs_error() -> None:
    off = _Box(enabled=False)
    client = _chain(lambda r: httpx.Response(502), brave=off.client(_ok).search)
    with pytest.raises(WebSearchError, match="unavailable"):
        await client.search("q")
    with pytest.raises(WebSearchError):  # and with no tiers wired at all, as before
        await _chain(lambda r: httpx.Response(502)).search("q")


async def test_the_window_reaches_brave_after_searxng_widened_it() -> None:
    box = _Box()
    calls: list[httpx.Request] = []

    def searx(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": []} if len(calls) == 1 else _SEARX_THIN)

    result = await _chain(searx, brave=box.client(_ok).search).search("q", time_range="week")
    assert len(calls) == 2 and result.source == "brave" and not result.window_dropped
    assert box.requests[0].url.params["freshness"] == "pw"
