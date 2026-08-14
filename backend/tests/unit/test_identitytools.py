"""jerv's `resolve_identity` tool + Wikidata client (docs/reference/ASSISTANT.md
"Agent selection"). HTTP is faked via MockTransport — no live network, like the other web
clients. The canned data includes an ALIAS case (aliases.en) — the whole point of the tool."""

import httpx

from jbrain.agent.identitytools import build_resolve_identity_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.web.wikidata import WikidataClient

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

# A physician with a prior name — the alias this tool exists to harvest.
_SEARCH = {"search": [{"id": "Q1", "label": "Jane Roe", "description": "American physician"}]}
_ENTITY = {
    "entities": {
        "Q1": {
            "labels": {"en": {"value": "Jane Roe"}},
            "descriptions": {"en": {"value": "American physician"}},
            "aliases": {"en": [{"value": "Jane Doe"}, {"value": "J. M. Roe"}]},
            "claims": {
                "P106": [
                    {"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q39631"}}}}
                ],
                "P9450": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "datatype": "external-id",
                            "datavalue": {"value": "1234567890"},
                        }
                    }
                ],
                "P569": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "datatype": "time",
                            "datavalue": {
                                "value": {"time": "+1970-06-04T00:00:00Z", "precision": 11},
                                "type": "time",
                            },
                        }
                    }
                ],
            },
        }
    }
}
# The batched label resolution for the occupation QID + the NPI property.
_LABELS = {
    "entities": {
        "Q39631": {"labels": {"en": {"value": "physician"}}},
        "P9450": {"labels": {"en": {"value": "National Provider Identifier"}}},
    }
}
_EMPTY_SEARCH: dict = {"search": []}


def _client(search: dict, entity: dict, labels: dict) -> WikidataClient:
    """A Wikidata client whose transport answers the three call shapes distinctly and records
    the requests it saw (on the returned client's `.seen`)."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        action = request.url.params.get("action")
        if action == "wbsearchentities":
            return httpx.Response(200, json=search)
        if action == "wbgetentities":
            return httpx.Response(200, json=labels)
        # Otherwise it's Special:EntityData/<QID>.json.
        return httpx.Response(200, json=entity)

    client = WikidataClient("https://www.wikidata.org", transport=httpx.MockTransport(handle))
    client.seen = seen  # type: ignore[attr-defined]
    return client


# --- WikidataClient --------------------------------------------------------


async def test_resolve_harvests_aliases_occupation_and_identifiers() -> None:
    client = _client(_SEARCH, _ENTITY, _LABELS)
    entities, ok = await client.resolve("Jane Roe")
    assert ok
    assert len(entities) == 1
    e = entities[0]
    assert e.qid == "Q1"
    assert e.label == "Jane Roe"
    assert e.description == "American physician"
    assert e.aliases == ("Jane Doe", "J. M. Roe")  # the prior-name harvest
    assert e.occupations == ("physician",)  # the P106 QID resolved to a label
    assert e.identifiers == (("National Provider Identifier", "1234567890"),)  # NPI pivot
    assert e.birth_year == 1970  # P569 harvested — the records identity gate's era signal
    assert e.url == "https://www.wikidata.org/wiki/Q1"
    # A descriptive User-Agent (Wikidata's policy requires it) rides every call.
    assert all("JBrain2 research" in r.headers["user-agent"] for r in client.seen)  # type: ignore[attr-defined]


async def test_resolve_without_birth_date_leaves_birth_year_none() -> None:
    # Not every entity records P569; the records identity gate then degrades to no era check (the
    # party-name gate still applies), so birth_year must be None rather than raising.
    no_dob = {"entities": {"Q1": {"labels": {"en": {"value": "Jane Roe"}}, "claims": {}}}}
    entities, ok = await _client(_SEARCH, no_dob, _LABELS).resolve("Jane Roe")
    assert ok is True and entities[0].birth_year is None


async def test_resolve_sends_a_user_agent() -> None:
    client = _client(_SEARCH, _ENTITY, _LABELS)
    await client.resolve("Jane Roe")
    assert "jeff.m.hopkins@gmail.com" in client.seen[0].headers["user-agent"]  # type: ignore[attr-defined]


async def test_resolve_unconfigured_returns_empty_ok_false() -> None:
    entities, ok = await WikidataClient("").resolve("anyone")
    assert entities == [] and ok is False


async def test_resolve_empty_search_is_ok_true_and_empty() -> None:
    client = _client(_EMPTY_SEARCH, _ENTITY, _LABELS)
    entities, ok = await client.resolve("Nobody")
    assert ok is True and entities == []


async def test_resolve_errors_return_empty_not_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = WikidataClient("https://www.wikidata.org", transport=httpx.MockTransport(boom))
    entities, ok = await client.resolve("anyone")
    assert entities == [] and ok is False  # graceful: never raises into the tool


async def test_repeat_resolve_is_served_from_cache() -> None:
    client = _client(_SEARCH, _ENTITY, _LABELS)
    await client.resolve("Jane Roe")
    calls = len(client.seen)  # type: ignore[attr-defined]
    await client.resolve("Jane Roe")
    assert len(client.seen) == calls  # type: ignore[attr-defined]  # the repeat served from cache


# --- resolve_identity tool --------------------------------------------------


async def test_tool_surfaces_the_alias_and_reframes_the_reruns() -> None:
    handlers = build_resolve_identity_handlers(_client(_SEARCH, _ENTITY, _LABELS))
    out = await handlers["resolve_identity"]({"name": "Jane Roe"}, CTX)
    assert isinstance(out, ToolOutput)
    text = str(out)
    assert "Jane Roe" in text
    assert "Jane Doe" in text  # the alias is surfaced
    assert "physician" in text
    assert "National Provider Identifier 1234567890" in text
    assert "Wikidata" in text
    # It reframes toward re-running public_records (its sources) under each variant.
    assert "public_records" in text and "license source" in text
    assert out.web_sources and out.web_sources[0].url == "https://www.wikidata.org/wiki/Q1"


async def test_tool_no_results_is_a_clean_line() -> None:
    handlers = build_resolve_identity_handlers(_client(_EMPTY_SEARCH, _ENTITY, _LABELS))
    out = str(await handlers["resolve_identity"]({"name": "Nobody McNobody"}, CTX))
    assert "No Wikidata entity" in out
    assert "Nobody McNobody" in out


async def test_tool_needs_a_name() -> None:
    handlers = build_resolve_identity_handlers(_client(_SEARCH, _ENTITY, _LABELS))
    assert "needs a name" in await handlers["resolve_identity"]({"name": "  "}, CTX)


async def test_tool_reports_an_outage_distinctly_from_empty() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = WikidataClient("https://www.wikidata.org", transport=httpx.MockTransport(boom))
    out = str(await build_resolve_identity_handlers(client)["resolve_identity"]({"name": "x"}, CTX))
    assert "unavailable" in out.lower()


async def test_tool_unconfigured_reports_not_configured() -> None:
    handlers = build_resolve_identity_handlers(WikidataClient(""))
    out = str(await handlers["resolve_identity"]({"name": "x"}, CTX))
    assert "not configured" in out.lower()


async def test_tool_emits_a_tendril_event() -> None:
    fired: list[tuple[str, str | None]] = []
    handlers = build_resolve_identity_handlers(
        _client(_EMPTY_SEARCH, _ENTITY, _LABELS),
        emit=lambda kind, text=None: fired.append((kind, text)),
    )
    await handlers["resolve_identity"]({"name": "Jane Roe"}, CTX)
    assert fired == [("web_search", "Jane Roe")]


# resolve_identity is no longer a top-level tool — it is a `source` of the public_records
# umbrella (docs/plans/TOOL_CATALOG_PLAN.md); its sidecar + jerv-only/web-class registration are
# covered by test_public_records.py. This file keeps the Wikidata source's own behaviour.
