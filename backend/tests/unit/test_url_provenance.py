"""The web tools' provenance gate (agent/url_provenance.py, owner decision 2026-10-08).

A model-supplied URL is fetched only when its site already appeared in the conversation —
an owner message or a tool result, never the model's own prose. Driven through the REAL
web_fetch / fetch_image / browse handlers and, for the seeding and the per-result growth,
through the agent loop itself, as a turn runs. A refused call never reaches the network.
Held at 100%: it is what stops a regurgitated training-data URL from leaving the box."""

from __future__ import annotations

from typing import Any

import httpx

from jbrain.agent import url_provenance
from jbrain.agent.browse import BrowseAgent
from jbrain.agent.browsetools import build_browse_handlers
from jbrain.agent.continuation import PlanContinuationRunner
from jbrain.agent.contracts import ToolSpec, WebSource
from jbrain.agent.fetchtools import build_fetch_image_handlers
from jbrain.agent.history_replay import build, transcript_sites
from jbrain.agent.loop import AgentLoop, ToolContext, ToolHandler, ToolOutput
from jbrain.agent.toolfile import ToolFile
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.agent.transcript_store import TurnRecord
from jbrain.agent.url_provenance import REFUSAL, REFUSAL_BRIEF, SeenSites, seeded, site_of
from jbrain.agent.webtools import build_web_handlers
from jbrain.db.session import SessionContext
from jbrain.llm import (
    AssistantMessage,
    FakeLlmClient,
    LlmRouter,
    LlmTurn,
    LlmUsage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from jbrain.web.fetch import WebFetcher
from jbrain.web.mcp_client import McpHttpClient
from jbrain.web.search import SearxngClient
from tests.unit.browse_fakes import FakeBrowser

OWNER = SessionContext(principal_kind="owner")
# The live incident's address, which appeared nowhere in the conversation.
INVENTED = (
    "https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/proxy_temp_file/x.ts"
    "?Expires=1&OSSAccessKeyId=k&Signature=s"
)


def _page(links: tuple[str, ...] = ()) -> bytes:
    anchors = "".join(f'<a href="{u}">link</a>' for u in links)
    return (
        b"<html><head><title>Page</title></head><body><article>"
        + b"<p>A long article with real content in it, more than enough to read.</p>" * 40
        + anchors.encode()
        + b"</article></body></html>"
    )


class _Net:
    """A fetcher whose every request is recorded, serving `pages` by host."""

    def __init__(self, pages: dict[str, bytes] | None = None) -> None:
        self.hosts: list[str] = []
        self._pages = pages or {}

    def fetcher(self) -> WebFetcher:
        def handle(request: httpx.Request) -> httpx.Response:
            self.hosts.append(request.url.host)
            body = self._pages.get(request.url.host, _page())
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})

        return WebFetcher(transport=httpx.MockTransport(handle))


class _NoSkips:
    """A skip list the gate must never consult: a refused call stops before it."""

    async def active_hosts(self) -> set[str]:
        raise AssertionError("the skip list was consulted for a refused address")


def _ctx(seen: SeenSites | None) -> ToolContext:
    return ToolContext(session=OWNER, scopes=(), seen_sites=seen)


def _seen(*texts: str) -> SeenSites:
    return seeded([UserMessage(text=t) for t in texts])


# --- The set ---------------------------------------------------------------------------


def test_site_of_folds_subdomains_aliases_and_bare_hosts() -> None:
    assert site_of("https://WWW.Example.com/a") == "example.com"
    assert site_of("news.bbc.co.uk/x") == "bbc.co.uk"
    assert site_of("https://raw.githubusercontent.com/o/r/main/f") == "github.com"
    assert site_of("https://gist.githubusercontent.com/u/1") == "github.com"
    assert site_of("https://codeload.github.com/o/r/tar.gz/main") == "github.com"
    assert site_of("https://youtu.be/abc") == "youtube.com"
    assert site_of("http://192.168.1.5:8080/x") == "192.168.1.5"
    assert site_of("http://localhost:8080/") == "localhost"
    assert site_of("http://[::1]/") == "::1"
    assert site_of("not a url") is None
    assert site_of("http://[bad") is None
    # A name the IDNA codec rejects (a label over 63 characters) is kept as written.
    long_label = "a" * 70 + ".com"
    assert site_of(f"https://{long_label}/") == long_label


def test_add_text_takes_urls_and_bare_hosts_but_not_paths_or_dotted_words() -> None:
    seen = SeenSites()
    seen.add_text(
        "see https://a.example.org/x, or check nytimes.com. The file frontend/src/codeLang.ts,"
        " os.path and jeff@mail.example.net are not hosts; v3.11 is a version."
    )
    assert "example.org" in seen and "nytimes.com" in seen
    assert len(seen) == 2


def test_the_set_is_bounded_and_drops_the_oldest_site() -> None:
    seen = SeenSites(limit=2)
    for host in ("a.com", "b.com", "a.com", "c.com"):
        seen.add_url(f"https://{host}/")
    # a.com was seen again after b.com, so b.com is the oldest when c.com arrives.
    assert "a.com" in seen and "c.com" in seen and "b.com" not in seen
    seen.add_url("junk")
    assert len(seen) == 2


def test_seeding_reads_user_messages_and_tool_results_never_the_models_prose() -> None:
    seen = seeded(
        [
            UserMessage(text="look at https://owner.example.com/page"),
            AssistantMessage(text="I recall https://invented.example.net/x"),
            ToolResultMessage(results=(ToolResult("c1", "hit: https://found.example.org/a"),)),
        ],
        ["stub-era result https://compacted.example.io/p"],
    )
    assert seen.allows("https://owner.example.com/other")
    assert seen.allows("https://found.example.org/b")
    assert seen.allows("https://compacted.example.io/q")
    assert not seen.allows("https://invented.example.net/x")


def test_no_set_means_no_gate() -> None:
    """A handler driven outside the agent loop (no seeded set) is not gated."""
    assert url_provenance.refusal(INVENTED, None) is None
    assert url_provenance.refusal(INVENTED, SeenSites()) == REFUSAL


# --- web_fetch -------------------------------------------------------------------------


async def test_an_invented_host_is_refused_with_no_network_call() -> None:
    net = _Net()
    fetch = build_web_handlers(
        SearxngClient(""),
        net.fetcher(),
        domain_skips=_NoSkips(),  # type: ignore[arg-type]
    )["web_fetch"]
    ctx = _ctx(_seen("Show me the first 60 lines of frontend/src/agent/codeLang.ts in jbrain2"))
    out = await fetch({"url": INVENTED}, ctx)
    assert isinstance(out, ToolOutput) and str(out) == REFUSAL
    assert out.result_brief == REFUSAL_BRIEF
    assert net.hosts == []
    # A refusal teaches nothing: no dead-URL memo, no browse opening, no skip-list entry.
    assert ctx.failed_fetches == {} and ctx.browser_needed == {}


async def test_a_host_from_an_owner_message_is_fetched() -> None:
    net = _Net()
    fetch = build_web_handlers(SearxngClient(""), net.fetcher())["web_fetch"]
    out = await fetch(
        {"url": "https://docs.example.com/guide"}, _ctx(_seen("read docs.example.com please"))
    )
    assert net.hosts == ["docs.example.com"] and "A long article" in out


async def test_a_refused_address_is_fetchable_once_the_owner_gives_it() -> None:
    """The refusal is not a skip list: the same URL goes through as soon as its site has
    appeared — here, the owner pasting it."""
    net = _Net()
    fetch = build_web_handlers(SearxngClient(""), net.fetcher())["web_fetch"]
    ctx = _ctx(SeenSites())
    assert await fetch({"url": "https://x.example.org/a"}, ctx) == REFUSAL
    assert ctx.seen_sites is not None
    ctx.seen_sites.add_text("here: https://x.example.org/a")
    out = await fetch({"url": "https://x.example.org/a"}, ctx)
    assert net.hosts == ["x.example.org"] and "A long article" in out


async def test_github_files_are_open_after_a_repo_link_was_given() -> None:
    net = _Net()
    fetch = build_web_handlers(SearxngClient(""), net.fetcher())["web_fetch"]
    ctx = _ctx(_seen("my repo is https://github.com/jeff/jbrain2"))
    await fetch({"url": "https://github.com/jeff/jbrain2/blob/main/frontend/src/a.ts"}, ctx)
    await fetch({"url": "https://raw.githubusercontent.com/jeff/jbrain2/main/README.md"}, ctx)
    assert net.hosts == ["github.com", "raw.githubusercontent.com"]
    # ...and never before one was.
    assert await fetch({"url": "https://github.com/jeff/jbrain2"}, _ctx(SeenSites())) == REFUSAL


# --- fetch_image and browse ------------------------------------------------------------


async def test_fetch_image_is_gated_the_same_way() -> None:
    net = _Net()
    fetch_image = build_fetch_image_handlers(
        net.fetcher(),
        blobs=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        maker=None,  # type: ignore[arg-type]
    )["fetch_image"]
    out = await fetch_image({"url": "https://cdn.invented.example/p.jpg"}, _ctx(SeenSites()))
    assert isinstance(out, ToolOutput) and out == REFUSAL and out.result_brief == REFUSAL_BRIEF
    assert net.hosts == []


async def test_browse_start_url_is_gated_before_the_fetch_first_gate() -> None:
    turn = LlmTurn("", [ToolCall("c1", "give_up", {"reason": "x"})], "tool_use", LlmUsage(1, 1))
    fake = FakeLlmClient(turns=[turn])
    router = LlmRouter({"xai": fake}, {"browse.step": ("xai", "grok-4.3")})
    browser = FakeBrowser()
    mcp = McpHttpClient("http://browser:8931/mcp", transport=browser.transport())
    browse = build_browse_handlers(BrowseAgent(router, mcp, loop="b1"))["browse"]
    ctx = _ctx(SeenSites())
    # Even a site the fetch-first memo would open is refused while its address is unseen.
    ctx.browser_needed["epictheatres.com"] = "gated"
    out = await browse({"goal": "g", "start_url": "https://www.epictheatres.com/"}, ctx)
    assert isinstance(out, ToolOutput) and out == REFUSAL and out.result_brief == REFUSAL_BRIEF
    assert fake.converse_calls == [] and browser.methods == []
    # Seen, it falls through to the browse gates as before (here: a run that gives up).
    assert ctx.seen_sites is not None
    ctx.seen_sites.add_url("https://epictheatres.com/titusville")
    out = await browse({"goal": "g", "start_url": "https://www.epictheatres.com/"}, ctx)
    assert out.startswith("[BROWSE RESULT")
    # No start_url is the fetch-first gate's refusal, not this one's.
    out = await browse({"goal": "g"}, _ctx(SeenSites()))
    assert isinstance(out, ToolOutput) and out.result_brief == "refused · fetch first"


# --- Through the agent loop ------------------------------------------------------------


def _tool(name: str, handler: ToolHandler) -> RegisteredTool:
    spec = ToolSpec(name=name, version=1, params={"type": "object"}, permission="read")
    return RegisteredTool(toolfile=ToolFile(spec=spec, description=name), handler=handler)


def _calls(*calls: tuple[str, dict[str, Any]]) -> list[LlmTurn]:
    turns = [
        LlmTurn("", [ToolCall(f"c{i}", name, args)], "tool_use", LlmUsage(1, 1))
        for i, (name, args) in enumerate(calls)
    ]
    return [*turns, LlmTurn("done", [], "end_turn", LlmUsage(1, 1))]


async def _search(arguments: dict, ctx: ToolContext) -> ToolOutput:
    # The URL rides only on the web source, so the loop's web_sources branch is what opens it.
    return ToolOutput(
        "1 result", web_sources=(WebSource(url="https://hit.example.org/a", title="A"),)
    )


def _loop(net: _Net, turns: list[LlmTurn]) -> tuple[AgentLoop, FakeLlmClient]:
    fake = FakeLlmClient(turns=turns)
    router = LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")})
    fetch = build_web_handlers(SearxngClient(""), net.fetcher())["web_fetch"]
    registry = ToolRegistry([_tool("web_fetch", fetch), _tool("web_search", _search)])
    return AgentLoop(router, registry), fake


def _results(fake: FakeLlmClient) -> list[str]:
    """Every tool result the model was shown, in order."""
    messages = fake.converse_calls[-1]["messages"]
    return [r.content for m in messages if isinstance(m, ToolResultMessage) for r in m.results]


async def test_a_search_hit_and_a_fetched_pages_links_open_their_sites() -> None:
    net = _Net({"hit.example.org": _page(("https://linked.example.net/next",))})
    loop, fake = _loop(
        net,
        _calls(
            ("web_fetch", {"url": "https://hit.example.org/a"}),  # before the search: refused
            ("web_search", {"q": "x"}),
            ("web_fetch", {"url": "https://hit.example.org/a"}),
            ("web_fetch", {"url": "https://linked.example.net/next"}),
        ),
    )
    await loop.run(session=OWNER, scopes=(), conversation=[UserMessage(text="find x")])
    results = _results(fake)
    assert results[0] == REFUSAL
    assert net.hosts == ["hit.example.org", "linked.example.net"]


async def test_a_sub_agent_inherits_its_parents_sites_not_its_brief() -> None:
    """A spawned child runs on a copy of the parent's set (spawn.py passes `ctx.seen_sites`):
    a site the parent saw is fetchable whether or not the brief names it, and one only the
    brief names — the parent MODEL's text — is not."""
    net = _Net()
    loop, fake = _loop(
        net,
        _calls(
            ("web_fetch", {"url": "https://parent-seen.com/report"}),
            ("web_fetch", {"url": "https://brief-only.org/x"}),
            ("web_fetch", {"url": "https://inherited.net/y"}),
        ),
    )
    parent = _seen("compare https://parent-seen.com/a with https://inherited.net/b")
    await loop.run(
        session=OWNER,
        scopes=(),
        conversation=[
            UserMessage(text="Now: 2026-10-08"),
            UserMessage(text="Read https://parent-seen.com/report and https://brief-only.org/x."),
        ],
        force_final_answer=True,
        seen_sites=parent,
    )
    assert net.hosts == ["parent-seen.com", "inherited.net"]
    assert _results(fake)[1] == REFUSAL
    # The child grew its own copy; the parent's set is untouched by the child's run.
    assert "brief-only.org" not in parent


async def test_a_child_with_no_parent_set_seeds_from_its_brief() -> None:
    """The deepest orchestrator holds no set, so its children scan their own brief."""
    net = _Net()
    loop, _fake = _loop(net, _calls(("web_fetch", {"url": "https://task-site.com/r"})))
    await loop.run(
        session=OWNER,
        scopes=(),
        conversation=[UserMessage(text="Summarize https://task-site.com/r")],
        force_final_answer=True,
    )
    assert net.hosts == ["task-site.com"]


async def _echoing_search(arguments: dict, ctx: ToolContext) -> str:
    # web_search's own zero-hit wording, which repeats the query back.
    return f"No web results for '{arguments.get('query', '')}'."


async def test_searching_an_invented_address_does_not_open_it() -> None:
    """The refusal says to search first; a search that finds nothing echoes the query, and
    the echo of the model's own argument must not count as a source for it."""
    net = _Net()
    fake = FakeLlmClient(
        turns=_calls(
            ("web_fetch", {"url": INVENTED}),
            ("web_search", {"query": INVENTED}),
            ("web_fetch", {"url": INVENTED}),
        )
    )
    router = LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")})
    fetch = build_web_handlers(SearxngClient(""), net.fetcher())["web_fetch"]
    registry = ToolRegistry([_tool("web_fetch", fetch), _tool("web_search", _echoing_search)])
    await AgentLoop(router, registry).run(
        session=OWNER, scopes=(), conversation=[UserMessage(text="show me codeLang.ts")]
    )
    results = _results(fake)
    assert results[0] == REFUSAL and results[2] == REFUSAL
    assert net.hosts == []


def test_without_echoes_cuts_only_arguments_that_name_a_site() -> None:
    content = "No results for 'evil.com/x'; see https://real.org/p about python"
    args = {"query": "evil.com/x", "nested": [{"q": "python"}], "n": 3}
    cut = url_provenance.without_echoes(content, args)
    assert "evil.com" not in cut and "python" in cut and "https://real.org/p" in cut


def test_internationalized_names_match_in_either_spelling() -> None:
    assert _seen("https://bücher.de/katalog").allows("https://www.xn--bcher-kva.de/x")
    assert _seen("https://xn--bcher-kva.de/").allows("https://bücher.de/y")


def test_private_hosts_count_only_from_the_owners_words() -> None:
    owner = _seen("my NAS is at http://192.168.1.5:5000 or nas.local; also http://localhost:8080")
    for url in ("http://192.168.1.5/", "http://nas.local/x", "http://localhost:8080/"):
        assert owner.allows(url)
    tool = SeenSites()
    tool.add_text("links: http://192.168.1.5/ http://nas.local/ http://localhost/ nas.local")
    tool.add_url("http://localhost/")
    assert len(tool) == 0


def test_the_bound_keeps_the_current_message_over_old_results() -> None:
    seen = seeded(
        [UserMessage(text="now open https://current.com/x")],
        ["https://old1.com", "https://old2.com", "https://old3.com"],
        limit=2,
    )
    assert seen.allows("https://current.com/") and not seen.allows("https://old1.com/")


def test_transcript_sites_reads_owner_turns_and_tool_results_only() -> None:
    turns = [
        TurnRecord(role="user", content="see https://owner-said.com", seq=1),
        TurnRecord(
            role="assistant",
            content="I made up https://invented.org/x",
            tools=[{"id": "t1", "name": "web_search", "summary": "https://hit.net/a"}],
            seq=2,
        ),
    ]
    seen = transcript_sites(turns)
    assert seen.allows("https://owner-said.com/") and seen.allows("https://hit.net/")
    assert not seen.allows("https://invented.org/x")


class _Transcript:
    def __init__(self, turns: list[TurnRecord] | None) -> None:
        self._turns = turns

    async def load(self, ctx: object, session_id: str) -> list[TurnRecord]:
        if self._turns is None:
            raise RuntimeError("db down")
        return self._turns


def _continuations(transcript: _Transcript) -> PlanContinuationRunner:
    return PlanContinuationRunner(
        maker=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        runlog=None,  # type: ignore[arg-type]
        transcript=transcript,  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=None,  # type: ignore[arg-type]
    )


async def test_a_plan_continuation_seeds_from_the_transcript_not_the_plan() -> None:
    turns = [TurnRecord(role="user", content="plan a trip using https://owner-site.com", seq=1)]
    seen = await _continuations(_Transcript(turns))._seen_sites(OWNER, "s1")
    assert seen is not None and seen.allows("https://owner-site.com/")
    # Unreadable: None, and the loop falls back to scanning the conversation.
    assert await _continuations(_Transcript(None))._seen_sites(OWNER, "s1") is None


async def test_a_reopened_chat_keeps_its_history_sites_and_compacted_ones() -> None:
    """Replayed history seeds the set (an earlier search result), and so does `seen_seed` —
    the persisted results of a turn the replay stubbed — on the streaming path and its
    buffered retry twin alike."""
    history = [
        UserMessage(text="earlier question"),
        AssistantMessage(tool_calls=(ToolCall("h1", "web_search", {}),)),
        ToolResultMessage(results=(ToolResult("h1", "https://oldhit.com/a"),)),
        AssistantMessage(text="answer"),
        UserMessage(text="open both"),
    ]
    for buffer_retry in (False, True):
        net = _Net()
        loop, _fake = _loop(
            net,
            _calls(
                ("web_fetch", {"url": "https://oldhit.com/a"}),
                ("web_fetch", {"url": "https://stubbed.org/b"}),
                ("web_fetch", {"url": "https://never.net/c"}),
            ),
        )
        async for _event in loop.run_stream(
            session=OWNER,
            scopes=(),
            conversation=history,
            buffer_retry=buffer_retry,
            seen_seed=["result https://stubbed.org/b"],
        ):
            pass
        assert net.hosts == ["oldhit.com", "stubbed.org"]


def test_history_replay_carries_each_turns_provenance_past_its_stub() -> None:
    step = {
        "id": "t1",
        "name": "web_search",
        "summary": "https://in-summary.example.com/a",
        "web_sources": [{"url": "https://in-source.example.com/b"}, "junk"],
    }
    turns = [
        TurnRecord(role="user", content="q", seq=1),
        TurnRecord(role="assistant", content="a", tools=[step, {"id": "t2"}], seq=2),
    ]
    # Floor past the turn: its result replays as a stub, its provenance does not.
    entries = build(turns, floor=10)
    assert entries[0].provenance == ()
    assert entries[1].provenance == (
        "https://in-summary.example.com/a",
        "https://in-source.example.com/b",
    )
