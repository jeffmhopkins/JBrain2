"""The Gmail client + in-memory fake (docs/EMAIL_ARCHIVIST_PLAN.md). HTTP is faked
via MockTransport — no live network, like the web client and the LLM adapter."""

import base64
import json
from collections.abc import Callable

import httpx
import pytest

from jbrain.gmail import FakeGmail, GmailClient, GmailError, GmailMessage

_BASE = "https://gmail.googleapis.com/gmail/v1"
_TOKEN = "https://oauth2.googleapis.com/token"


def _client(
    api_handler: Callable[[httpx.Request], httpx.Response],
    *,
    refresh_token: str = "rt",
) -> GmailClient:
    """A client whose token endpoint always succeeds; `api_handler` answers the rest."""

    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        return api_handler(request)

    return GmailClient(
        "cid",
        "secret",
        refresh_token,
        base_url=_BASE,
        token_url=_TOKEN,
        transport=httpx.MockTransport(dispatch),
    )


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- auth ------------------------------------------------------------------


async def test_token_minted_once_and_reused() -> None:
    tokens = {"n": 0}

    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            tokens["n"] += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    client = GmailClient(
        "c", "s", "rt", base_url=_BASE, token_url=_TOKEN, transport=httpx.MockTransport(dispatch)
    )
    await client.search("a")
    await client.search("b")
    assert tokens["n"] == 1  # the cached access token is reused across calls


async def test_unconfigured_refresh_token_raises() -> None:
    client = _client(lambda r: httpx.Response(200), refresh_token="")
    with pytest.raises(GmailError):
        await client.search("q")


async def test_token_without_access_token_raises() -> None:
    def dispatch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # no access_token in the grant response

    client = GmailClient(
        "c", "s", "rt", base_url=_BASE, token_url=_TOKEN, transport=httpx.MockTransport(dispatch)
    )
    with pytest.raises(GmailError):
        await client.search("q")


async def test_api_retries_once_on_401() -> None:
    state = {"n": 0}

    def api(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    ids = await _client(api).search("q")
    assert ids == ["m1"]
    assert state["n"] == 2  # one failure, one retry after a fresh token


# --- reads -----------------------------------------------------------------


async def test_search_shapes_query_and_parses_ids() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2"}]}
        )

    ids = await _client(api).search("from:x", max_results=5)
    assert ids == ["m1", "m2"]
    assert seen[-1].url.params["q"] == "from:x"
    assert seen[-1].url.params["maxResults"] == "5"
    assert "/users/me/messages" in str(seen[-1].url)


async def test_get_full_parses_headers_and_body() -> None:
    raw = {
        "id": "m1",
        "threadId": "t1",
        "snippet": "Hi there",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "a@x.com"},
                {"name": "To", "value": "me@y.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Wed, 1 Jan 2020"},
            ],
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>ignored</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64url("the real body")}},
            ],
        },
    }
    msg = await _client(lambda r: httpx.Response(200, json=raw)).get("m1")
    assert msg.sender == "a@x.com"
    assert msg.subject == "Hello"
    assert msg.snippet == "Hi there"
    assert msg.body == "the real body"  # text/plain wins over the html sibling


async def test_get_metadata_only_sets_format_and_empty_body() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "snippet": "peek",
                "payload": {"headers": [{"name": "Subject", "value": "S"}]},
            },
        )

    msg = await _client(api).get("m1", metadata_only=True)
    assert seen[-1].url.params["format"] == "metadata"
    assert msg.subject == "S"
    assert msg.body == ""  # no body fetched in metadata mode


# --- labels + writes -------------------------------------------------------


async def test_list_labels_parses() -> None:
    body = {"labels": [{"id": "L1", "name": "Finance"}, {"id": "INBOX", "name": "INBOX"}]}
    labels = await _client(lambda r: httpx.Response(200, json=body)).list_labels()
    assert ("L1", "Finance") in [(lbl.id, lbl.name) for lbl in labels]


async def test_create_label_returns_new() -> None:
    body = {"id": "L9", "name": "Finance/Taxes"}
    label = await _client(lambda r: httpx.Response(200, json=body)).create_label("Finance/Taxes")
    assert label.id == "L9"
    assert label.name == "Finance/Taxes"


async def test_create_label_idempotent_on_conflict() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"error": "label exists"})
        return httpx.Response(200, json={"labels": [{"id": "L1", "name": "Finance"}]})

    label = await _client(api).create_label("Finance")
    assert label.id == "L1"  # resolved to the existing label, not re-created


async def test_modify_posts_label_changes() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    await _client(api).modify("m1", add_label_ids=["L1"], remove_label_ids=["INBOX"])
    body = json.loads(seen[-1].content)
    assert body["addLabelIds"] == ["L1"]
    assert body["removeLabelIds"] == ["INBOX"]
    assert "/messages/m1/modify" in str(seen[-1].url)


async def test_server_error_raises_gmail_error() -> None:
    with pytest.raises(GmailError):
        await _client(lambda r: httpx.Response(500)).search("q")


# --- FakeGmail -------------------------------------------------------------


def _msg(mid: str = "m1", subject: str = "Invoice", body: str = "please pay") -> GmailMessage:
    return GmailMessage(
        id=mid,
        thread_id="t",
        sender="a@x.com",
        to="me@y.com",
        subject=subject,
        date="2020",
        snippet=body[:20],
        body=body,
    )


async def test_fake_search_get_and_archive() -> None:
    fake = FakeGmail([_msg()])
    assert await fake.search("invoice") == ["m1"]
    assert (await fake.get("m1")).body == "please pay"
    assert fake.labels_on("m1") == {"INBOX"}
    label = await fake.create_label("Finance")
    await fake.modify("m1", add_label_ids=[label.id], remove_label_ids=["INBOX"])
    assert fake.labels_on("m1") == {label.id}  # moved out of the inbox into Finance


async def test_fake_create_label_idempotent() -> None:
    fake = FakeGmail()
    first = await fake.create_label("Finance")
    second = await fake.create_label("Finance")
    assert first.id == second.id


async def test_fake_metadata_only_drops_body() -> None:
    fake = FakeGmail([_msg(body="secret")])
    assert (await fake.get("m1", metadata_only=True)).body == ""


async def test_fake_get_missing_raises() -> None:
    with pytest.raises(GmailError):
        await FakeGmail().get("nope")
