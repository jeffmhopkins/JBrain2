"""jerv's `public_records` tool + CourtListener client (docs/reference/ASSISTANT.md
"Agent selection"). HTTP is faked via MockTransport — no live network, like the web
search/fetch clients. The canned data includes the real "NEELAM UPPAL v. DEPT. OF
HEALTH" opinion this tool exists to surface (the web-only research miss it fixes)."""

from typing import Any

import httpx

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.publicrecordstools import build_public_records_handlers
from jbrain.agent.readtools import TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.web.public_records import CourtListenerClient

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

# The real hit this tool exists to find: an opinion filed under a name web-only research
# missed. Returned for type=o.
_UPPAL_OPINION = {
    "count": 1,
    "results": [
        {
            "caseName": "NEELAM UPPAL v. DEPT. OF HEALTH",
            "court": "Fla. Dist. Ct. App.",
            "dateFiled": "2018-05-16",
            "docketNumber": "2D17-1234",
            "absolute_url": "/opinion/4444/neelam-uppal-v-dept-of-health/",
        }
    ],
}
# A RECAP docket for the SAME case (the o/r overlap that must dedup to one row).
_UPPAL_DOCKET = {
    "count": 1,
    "results": [
        {
            "caseName": "NEELAM UPPAL v. DEPT. OF HEALTH",
            "court": "Fla. Dist. Ct. App.",
            "dateFiled": "2018-05-16",
            "docketNumber": "2D17-1234",
            "docket_absolute_url": "/docket/9999/neelam-uppal-v-dept-of-health/",
        }
    ],
}
_EMPTY = {"count": 0, "results": []}

# A people/alias hit: a judge filed under a prior name, linking to the canonical record.
_PEOPLE = {
    "count": 1,
    "results": [
        {
            "id": 42,
            "slug": "jane-roe",
            "name_first": "Jane",
            "name_last": "Roe",
            "is_alias_of": "https://www.courtlistener.com/api/rest/v4/people/7/",
            "positions": [
                {"position_type": "Judge", "court": "Fla. Dist. Ct. App."},
                {"position_type": "Attorney"},
            ],
        }
    ],
}
_PEOPLE_EMPTY = {"count": 0, "results": []}


def _client(
    o_body: dict, r_body: dict, *, token: str = "", people_body: dict | None = None
) -> CourtListenerClient:
    """A CourtListener client whose transport answers type=o, type=r, and the /people/ endpoint
    distinctly, and records the requests it saw (on the returned client's `.seen`)."""
    seen: list[httpx.Request] = []
    people = people_body if people_body is not None else _PEOPLE_EMPTY

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/people/"):
            return httpx.Response(200, json=people)
        body = o_body if request.url.params.get("type") == "o" else r_body
        return httpx.Response(200, json=body)

    client = CourtListenerClient(
        "https://www.courtlistener.com", token, transport=httpx.MockTransport(handle)
    )
    client.seen = seen  # type: ignore[attr-defined]
    return client


# --- CourtListenerClient ---------------------------------------------------


async def test_search_queries_both_corpora_and_parses_a_hit() -> None:
    client = _client(_UPPAL_OPINION, _EMPTY)
    records, ok = await client.search("Neelam Uppal")
    assert ok
    assert len(records) == 1
    rec = records[0]
    assert rec.case_name == "NEELAM UPPAL v. DEPT. OF HEALTH"
    assert rec.court == "Fla. Dist. Ct. App."
    assert rec.date_filed == "2018-05-16"
    assert rec.kind == "opinion"
    # The relative absolute_url is joined to the pinned base.
    assert rec.url == "https://www.courtlistener.com/opinion/4444/neelam-uppal-v-dept-of-health/"
    # Both corpora were queried (type=o and type=r).
    types = {r.url.params.get("type") for r in client.seen}  # type: ignore[attr-defined]
    assert types == {"o", "r"}


async def test_search_dedups_the_opinion_docket_overlap() -> None:
    # The same case surfaces in BOTH o and r — it must collapse to one row (the opinion).
    client = _client(_UPPAL_OPINION, _UPPAL_DOCKET)
    records, ok = await client.search("Neelam Uppal")
    assert ok
    assert len(records) == 1
    assert records[0].kind == "opinion"  # opinion is queried first, so it wins the dedup


async def test_search_sends_the_token_when_configured() -> None:
    client = _client(_EMPTY, _EMPTY, token="secret")
    await client.search("anyone")
    assert client.seen[0].headers["authorization"] == "Token secret"  # type: ignore[attr-defined]


async def test_search_omits_authorization_when_keyless() -> None:
    client = _client(_EMPTY, _EMPTY)
    await client.search("anyone")
    assert "authorization" not in client.seen[0].headers  # type: ignore[attr-defined]


async def test_search_unconfigured_returns_empty_ok_false() -> None:
    records, ok = await CourtListenerClient("").search("anyone")
    assert records == [] and ok is False


async def test_search_errors_return_empty_not_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = CourtListenerClient(
        "https://www.courtlistener.com", transport=httpx.MockTransport(boom)
    )
    records, ok = await client.search("anyone")
    assert records == [] and ok is False  # graceful: never raises into the tool


async def test_repeat_search_is_served_from_cache() -> None:
    client = _client(_UPPAL_OPINION, _EMPTY)
    await client.search("Neelam Uppal")
    await client.search("Neelam Uppal")
    # 2 calls (o+r) the first time, then the cache — never a second pair.
    assert len(client.seen) == 2  # type: ignore[attr-defined]


# --- people / alias lookup --------------------------------------------------


async def test_search_people_parses_the_alias_and_positions() -> None:
    client = _client(_EMPTY, _EMPTY, people_body=_PEOPLE)
    people, ok = await client.search_people("Jane Roe")
    assert ok
    assert len(people) == 1
    p = people[0]
    assert p.name == "Jane Roe"
    assert p.is_alias_of == "https://www.courtlistener.com/api/rest/v4/people/7/"  # the alias edge
    assert p.positions == ("Judge — Fla. Dist. Ct. App.", "Attorney")
    assert p.position_count == 2
    assert p.url == "https://www.courtlistener.com/person/42/jane-roe/"
    # The name split into first/last for the people endpoint's separate fields.
    people_req = [r for r in client.seen if r.url.path.endswith("/people/")][0]  # type: ignore[attr-defined]
    assert people_req.url.params.get("name_first") == "Jane"
    assert people_req.url.params.get("name_last") == "Roe"


async def test_search_people_errors_return_empty_not_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = CourtListenerClient(
        "https://www.courtlistener.com", transport=httpx.MockTransport(boom)
    )
    people, ok = await client.search_people("anyone")
    assert people == [] and ok is False


async def test_search_people_unconfigured_returns_empty_ok_false() -> None:
    people, ok = await CourtListenerClient("").search_people("anyone")
    assert people == [] and ok is False


# --- public_records tool ----------------------------------------------------


async def test_tool_surfaces_the_uppal_opinion_with_court_date_and_url() -> None:
    handlers = build_public_records_handlers(_client(_UPPAL_OPINION, _EMPTY))
    out = await handlers["public_records"]({"name": "Neelam Uppal"}, CTX)
    assert isinstance(out, ToolOutput)
    text = str(out)
    assert "NEELAM UPPAL v. DEPT. OF HEALTH" in text
    assert "Fla. Dist. Ct. App." in text
    assert "2018-05-16" in text
    assert "courtlistener.com/opinion/4444" in text
    # The header names the source and its limits, and a LEAD/verify caution is present.
    assert "CourtListener" in text
    assert "verify" in text.lower()
    # The record rides as a citable web source.
    assert out.web_sources and out.web_sources[0].url.startswith("https://www.courtlistener.com")


async def test_tool_surfaces_the_people_alias_section() -> None:
    handlers = build_public_records_handlers(_client(_EMPTY, _EMPTY, people_body=_PEOPLE))
    out = await handlers["public_records"]({"name": "Jane Roe"}, CTX)
    assert isinstance(out, ToolOutput)
    text = str(out)
    assert "judge/official record(s)" in text
    assert "Jane Roe" in text
    assert "ALIAS record" in text  # the is_alias_of link is surfaced
    assert "Judge — Fla. Dist. Ct. App." in text
    assert "person/42/jane-roe" in text
    # The person rides as a citable web source.
    assert any(s.url.endswith("/person/42/jane-roe/") for s in out.web_sources)


async def test_tool_dedups_and_reports_one_row_for_the_overlap() -> None:
    handlers = build_public_records_handlers(_client(_UPPAL_OPINION, _UPPAL_DOCKET))
    out = str(await handlers["public_records"]({"name": "Neelam Uppal"}, CTX))
    assert out.count("NEELAM UPPAL v. DEPT. OF HEALTH") == 1
    assert "1 record(s)" in out


async def test_tool_no_results_is_a_clean_line() -> None:
    handlers = build_public_records_handlers(_client(_EMPTY, _EMPTY))
    out = str(await handlers["public_records"]({"name": "Nobody McNobody"}, CTX))
    assert "No public court records" in out
    assert "Nobody McNobody" in out
    # It nudges toward a name variant, since records often sit under a prior name.
    assert "variant" in out.lower()


async def test_tool_needs_a_name() -> None:
    handlers = build_public_records_handlers(_client(_EMPTY, _EMPTY))
    assert "needs a name" in await handlers["public_records"]({"name": "  "}, CTX)


async def test_tool_reports_an_outage_distinctly_from_empty() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = CourtListenerClient(
        "https://www.courtlistener.com", transport=httpx.MockTransport(boom)
    )
    out = str(await build_public_records_handlers(client)["public_records"]({"name": "x"}, CTX))
    assert "unavailable" in out.lower()


async def test_tool_unconfigured_reports_not_configured() -> None:
    handlers = build_public_records_handlers(CourtListenerClient(""))
    out = str(await handlers["public_records"]({"name": "x"}, CTX))
    assert "not configured" in out.lower()


async def test_tool_emits_a_tendril_event() -> None:
    fired: list[tuple[str, str | None]] = []
    handlers = build_public_records_handlers(
        _client(_EMPTY, _EMPTY), emit=lambda kind, text=None: fired.append((kind, text))
    )
    await handlers["public_records"]({"name": "Neelam Uppal"}, CTX)
    assert fired == [("web_search", "Neelam Uppal")]


# --- registration / permission (web-class, jerv-only) -----------------------


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


def test_public_records_sidecar_is_a_web_tool_requiring_a_name() -> None:
    tf = load_tool(TOOLS_DIR / "public_records.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.side_effecting is False
    assert tf.spec.params["required"] == ["name"]
    assert "public_records" in JERV_TOOLS


def test_public_records_is_jerv_only_not_curator() -> None:
    registry = ToolRegistry(
        [RegisteredTool(toolfile=load_tool(TOOLS_DIR / "public_records.tool"), handler=_noop)]
    )
    curator = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "public_records" not in curator  # the web class is never in the curator wildcard
    assert "public_records" in registry.allowed_names(scopes=(), allow=JERV_TOOLS)
