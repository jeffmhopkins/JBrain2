"""jerv's `provider_license` tool + NPPES client (docs/reference/ASSISTANT.md
"Agent selection"). HTTP is faked via MockTransport — no live network. The canned data
includes an `other_names` ALIAS row and per-state license numbers — the tool's whole point."""

import httpx

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.providerlicensetools import build_provider_license_handlers
from jbrain.db.session import SessionContext
from jbrain.web.nppes import NppesClient

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

# A provider with an other_names row (a maiden name) and a state license — the alias + the
# license number are exactly what the tool exists to surface.
_MATCH = {
    "results": [
        {
            "number": "1234567890",
            "basic": {
                "first_name": "SHALOO",
                "last_name": "CHOUDHARY",
                "credential": "OTR",
                "status": "A",
            },
            "other_names": [
                {"code": "5", "first_name": "SHALOO", "last_name": "UPPAL", "type": "Other Name"}
            ],
            "taxonomies": [
                {
                    "code": "225X00000X",
                    "desc": "Occupational Therapist",
                    "license": "46TR001",
                    "primary": True,
                    "state": "NJ",
                }
            ],
            "addresses": [{"city": "NEWARK", "state": "NJ"}],
        }
    ]
}
_EMPTY: dict = {"results": []}


def _client(body: dict) -> NppesClient:
    """An NPPES client whose transport answers with `body` and records requests on `.seen`."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    client = NppesClient("https://npiregistry.cms.hhs.gov", transport=httpx.MockTransport(handle))
    client.seen = seen  # type: ignore[attr-defined]
    return client


# --- NppesClient -----------------------------------------------------------


async def test_search_splits_the_name_and_parses_alias_and_license() -> None:
    client = _client(_MATCH)
    providers, ok = await client.search("Shaloo Choudhary")
    assert ok
    assert len(providers) == 1
    p = providers[0]
    assert p.npi == "1234567890"
    assert p.name == "SHALOO CHOUDHARY"
    assert p.credential == "OTR"
    assert p.status == "active"  # "A" expanded
    assert p.other_names == ("SHALOO UPPAL (Other Name)",)  # the maiden-name pivot
    assert p.taxonomies[0].license == "46TR001" and p.taxonomies[0].state == "NJ"
    assert p.city == "NEWARK" and p.state == "NJ"
    # The name was split into first/last for the registry's separate fields.
    params = client.seen[0].url.params  # type: ignore[attr-defined]
    assert params.get("first_name") == "Shaloo" and params.get("last_name") == "Choudhary"


async def test_search_passes_the_optional_state() -> None:
    client = _client(_EMPTY)
    await client.search("Jane Roe", state="fl")
    assert client.seen[0].url.params.get("state") == "FL"  # type: ignore[attr-defined]


async def test_search_single_token_is_a_surname() -> None:
    client = _client(_EMPTY)
    await client.search("Uppal")
    params = client.seen[0].url.params  # type: ignore[attr-defined]
    assert params.get("last_name") == "Uppal" and "first_name" not in params


async def test_search_unconfigured_returns_empty_ok_false() -> None:
    providers, ok = await NppesClient("").search("anyone")
    assert providers == [] and ok is False


async def test_search_empty_is_ok_true_and_empty() -> None:
    providers, ok = await _client(_EMPTY).search("Nobody")
    assert ok is True and providers == []


async def test_search_errors_return_empty_not_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = NppesClient("https://npiregistry.cms.hhs.gov", transport=httpx.MockTransport(boom))
    providers, ok = await client.search("anyone")
    assert providers == [] and ok is False  # graceful: never raises into the tool


async def test_repeat_search_is_served_from_cache() -> None:
    client = _client(_MATCH)
    await client.search("Shaloo Choudhary")
    await client.search("Shaloo Choudhary")
    assert len(client.seen) == 1  # type: ignore[attr-defined]  # the repeat served from cache


# --- provider_license tool --------------------------------------------------


async def test_tool_surfaces_the_alias_license_and_registry_caveat() -> None:
    handlers = build_provider_license_handlers(_client(_MATCH))
    out = await handlers["provider_license"]({"name": "Shaloo Choudhary"}, CTX)
    assert isinstance(out, ToolOutput)
    text = str(out)
    assert "SHALOO CHOUDHARY" in text
    assert "SHALOO UPPAL (Other Name)" in text  # the alias is surfaced prominently
    assert "NJ #46TR001" in text and "Occupational Therapist" in text
    assert "NPPES" in text
    # It frames the registry limit: no disciplinary actions here — go to the state board.
    assert "does" in text.lower() and "disciplinary" in text.lower()
    assert out.web_sources and out.web_sources[0].url.endswith("/provider-view/1234567890")


async def test_tool_no_results_is_a_clean_line() -> None:
    handlers = build_provider_license_handlers(_client(_EMPTY))
    out = str(await handlers["provider_license"]({"name": "Nobody McNobody"}, CTX))
    assert "No provider registered" in out
    assert "variant" in out.lower()


async def test_tool_needs_a_name() -> None:
    handlers = build_provider_license_handlers(_client(_EMPTY))
    assert "needs a name" in await handlers["provider_license"]({"name": "  "}, CTX)


async def test_tool_reports_an_outage_distinctly_from_empty() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = NppesClient("https://npiregistry.cms.hhs.gov", transport=httpx.MockTransport(boom))
    out = str(await build_provider_license_handlers(client)["provider_license"]({"name": "x"}, CTX))
    assert "unavailable" in out.lower()


async def test_tool_unconfigured_reports_not_configured() -> None:
    handlers = build_provider_license_handlers(NppesClient(""))
    out = str(await handlers["provider_license"]({"name": "x"}, CTX))
    assert "not configured" in out.lower()


async def test_tool_emits_a_tendril_event() -> None:
    fired: list[tuple[str, str | None]] = []
    handlers = build_provider_license_handlers(
        _client(_EMPTY), emit=lambda kind, text=None: fired.append((kind, text))
    )
    await handlers["provider_license"]({"name": "Shaloo Choudhary"}, CTX)
    assert fired == [("web_search", "Shaloo Choudhary")]


# provider_license is no longer a top-level tool — it is a `source` of the public_records
# umbrella (docs/plans/TOOL_CATALOG_PLAN.md); its sidecar + jerv-only/web-class registration are
# covered by test_public_records.py. This file keeps the NPPES source's own behaviour.
