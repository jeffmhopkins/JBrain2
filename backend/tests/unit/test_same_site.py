"""Same-site request forgery (security plan S1.3): the fetch-metadata / Origin guard on
every cookie-bearing HTTP request and WS handshake, the fail-closed WS Origin gate, and
the `__Host-` session cookie with its silent migration from the legacy name.

The attacker is a page on a sibling subdomain (a jcode preview), so the browser attaches
the victim's cookies — even Strict ones — and only the request's metadata gives it away.
"""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from jbrain.api.live import origin_allowed
from jbrain.api.same_site import (
    cookie_free_get,
    normalize_authority,
    origin_matches_host,
    refusal_reason,
)
from jbrain.api.session_cookie import apply_migration, mark_for_migration
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo
from tests.unit.test_route_auth_matrix import COOKIE_EXEMPT

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_LEGACY = "jbrain_session"
_HOST = "__Host-jbrain_session"
_REFUSED = {"detail": "cross-site request refused"}


def _owner_token(repo: FakeAuthRepo) -> str:
    async def go() -> str:
        key = await auth_service.rotate_owner_key(repo)
        return await auth_service.login(repo, key, "t")

    return asyncio.run(go())


def _device_token(repo: FakeAuthRepo) -> str:
    async def go() -> str:
        await repo.create_principal("device_key", keys.hash_key("dev-key"), "phone", "sub-1")
        token = await auth_service.mint_dashboard_session(repo, "dev-key")
        assert token is not None
        return token

    return asyncio.run(go())


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    with TestClient(app) as c:
        app.state.auth_repo = repo
        yield c


@pytest.fixture
def secure_client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    # https so the jar returns Secure cookies, as a real browser on the deploy would.
    app = create_app(Settings(secure_cookies=True, database_url=_DB))
    with TestClient(app, base_url="https://testserver") as c:
        app.state.auth_repo = repo
        yield c


def _cookie(token: str, name: str = _LEGACY) -> dict[str, str]:
    return {"cookie": f"{name}={token}"}


# --- the pure decision -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example.COM", "example.com"),
        ("example.com:443", "example.com"),
        ("example.com:80", "example.com"),
        ("example.com:8443", "example.com:8443"),
        ("[::1]:443", "[::1]"),
        ("[::1]:8000", "[::1]:8000"),
        ("", None),
        ("a:b:c", None),
        ("user@example.com", None),
        ("example.com:http", None),
        ("[::1", None),
        ("[::1]8000", None),
    ],
)
def test_normalize_authority(value: str, expected: str | None) -> None:
    assert normalize_authority(value) == expected


def test_origin_matches_host_ignores_scheme_and_default_ports() -> None:
    # Behind the tunnel the api sees plain HTTP for a page loaded over HTTPS.
    assert origin_matches_host("https://brain.example.com", "brain.example.com") is True
    assert origin_matches_host("https://brain.example.com", "brain.example.com:80") is True
    assert origin_matches_host("http://jbrain.local:8443", "jbrain.local:8443") is True
    assert origin_matches_host("https://abc-preview.example.com", "example.com") is False
    assert origin_matches_host("null", "example.com") is False
    assert origin_matches_host("https://example.com", None) is False


_CALLBACK = "/api/settings/gmail/callback"
_NAV = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}


@pytest.mark.parametrize(
    ("headers", "method", "path", "refused"),
    [
        ({}, "POST", "/api/x", False),
        ({"sec-fetch-site": "same-origin"}, "POST", "/api/x", False),
        ({"sec-fetch-site": "none"}, "GET", "/api/x", False),
        ({"sec-fetch-site": "same-site"}, "POST", "/api/x", True),
        ({"sec-fetch-site": "cross-site"}, "POST", "/api/x", True),
        ({"sec-fetch-site": "same-site", "sec-fetch-mode": "cors"}, "GET", "/api/x", True),
        # Same-site is refused even as a top-level navigation (a preview's popup onto a
        # side-effecting GET), and even on the callback.
        ({"sec-fetch-site": "same-site", **_NAV}, "GET", "/api/brain/tts", True),
        ({"sec-fetch-site": "same-site", **_NAV}, "GET", _CALLBACK, True),
        # Cross-site navigation passes only on an allow-listed callback.
        ({"sec-fetch-site": "cross-site", **_NAV}, "GET", _CALLBACK, False),
        ({"sec-fetch-site": "cross-site", **_NAV}, "HEAD", _CALLBACK, False),
        ({"sec-fetch-site": "cross-site", **_NAV}, "GET", "/api/settings/gmail/connect", True),
        ({"sec-fetch-site": "cross-site", **_NAV}, "POST", _CALLBACK, True),
        ({"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}, "GET", _CALLBACK, True),
        # A WS handshake never gets the navigation exemption.
        ({"sec-fetch-site": "cross-site", **_NAV}, None, _CALLBACK, True),
        ({"origin": "https://h.example", "host": "h.example"}, "POST", "/api/x", False),
        ({"origin": "https://x-preview.h.example", "host": "h.example"}, "POST", "/api/x", True),
        ({"origin": "null", "host": "h.example"}, "POST", "/api/x", True),
        ({"origin": "https://h.example"}, "POST", "/api/x", True),
        # Fetch metadata, when present, is the authority; Origin is the fallback.
        ({"sec-fetch-site": "same-origin", "origin": "https://other"}, "POST", "/api/x", False),
    ],
)
def test_refusal_reason(
    headers: dict[str, str], method: str | None, path: str, refused: bool
) -> None:
    assert (refusal_reason(Headers(headers), method, path) is not None) is refused


# --- the middleware, HTTP ----------------------------------------------------


def test_same_site_post_with_cookie_is_refused(client: TestClient, repo: FakeAuthRepo) -> None:
    token = _owner_token(repo)
    for site in ("same-site", "cross-site"):
        resp = client.post(
            "/api/auth/session",
            json={"owner_key": "x"},
            headers={**_cookie(token), "sec-fetch-site": site, "sec-fetch-mode": "cors"},
        )
        assert resp.status_code == 403
        assert resp.json() == _REFUSED


def test_same_site_get_fetch_with_cookie_is_refused(client: TestClient, repo: FakeAuthRepo) -> None:
    # Side-effecting GETs exist (connect flows, TTS, fetches): a fetch() is not exempt.
    token = _owner_token(repo)
    resp = client.get(
        "/api/auth/me",
        headers={**_cookie(token), "sec-fetch-site": "same-site", "sec-fetch-mode": "cors"},
    )
    assert resp.status_code == 403


def test_navigation_is_exempt_only_cross_site_onto_a_callback(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    token = _owner_token(repo)
    # Google's redirect back after consent passes the guard and reaches the route. The
    # route itself needs Postgres (absent here), so it errors: anything but the guard's 403.
    lenient = TestClient(client.app, raise_server_exceptions=False)
    resp = lenient.get(
        _CALLBACK,
        headers={**_cookie(token), "sec-fetch-site": "cross-site", **_NAV},
        follow_redirects=False,
    )
    assert resp.status_code != 403
    # A preview's popup onto the connect route (which would overwrite the OAuth state).
    resp = client.get(
        "/api/settings/gmail/connect",
        headers={**_cookie(token), "sec-fetch-site": "same-site", **_NAV},
        follow_redirects=False,
    )
    assert resp.status_code == 403 and resp.json() == _REFUSED


def test_same_origin_and_headerless_requests_pass(client: TestClient, repo: FakeAuthRepo) -> None:
    token = _owner_token(repo)
    for extra in (
        {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"},
        {"origin": "http://testserver"},
        {"origin": "https://testserver:443"},
        {},  # OkHttp, curl, firmware: neither header
    ):
        resp = client.get("/api/auth/me", headers={**_cookie(token), **extra})
        assert resp.status_code == 200, extra
        assert resp.json()["kind"] == "owner"


def test_mismatched_or_null_origin_with_cookie_is_refused(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    token = _owner_token(repo)
    for origin in ("https://abc-preview.testserver", "null"):
        resp = client.get("/api/auth/me", headers={**_cookie(token), "origin": origin})
        assert resp.status_code == 403, origin


def test_cookieless_cross_site_request_passes(client: TestClient) -> None:
    # Login, pairing and mint carry no session cookie: the guard leaves them to the route.
    resp = client.post(
        "/api/auth/session",
        json={"owner_key": "wrong"},
        headers={"sec-fetch-site": "cross-site", "origin": "https://elsewhere.example"},
    )
    assert resp.status_code == 401


# --- the middleware, WebSocket -----------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"sec-fetch-site": "same-site", "sec-fetch-mode": "websocket"},
        {"sec-fetch-site": "cross-site", "sec-fetch-mode": "websocket"},
        {"origin": "https://abc-preview.testserver"},
    ],
)
def test_forged_ws_handshake_with_cookie_closes_before_accept(
    client: TestClient, repo: FakeAuthRepo, extra: dict[str, str]
) -> None:
    token = _owner_token(repo)
    for path in ("/api/locations/live", "/api/jcode/sessions/abc/terminal"):
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(path, headers={**_cookie(token), **extra}),
        ):
            pass
        assert exc.value.code == 4403, path


def test_same_origin_ws_with_cookie_reaches_the_route(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    # The owner's own terminal handshake gets through the guard and authenticates; code
    # mode is off in this app, so the route answers 4404 (not the guard's 4403).
    token = _owner_token(repo)
    headers = {**_cookie(token), "sec-fetch-site": "same-origin", "origin": "http://testserver"}
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/jcode/sessions/abc/terminal", headers=headers),
    ):
        pass
    assert exc.value.code == 4404


def test_cookieless_ws_passes_the_guard_to_the_route(client: TestClient) -> None:
    # The route's own auth answers (4401), proving the guard did not.
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/locations/live", headers={"sec-fetch-site": "cross-site"}),
    ):
        pass
    assert exc.value.code == 4401


# --- origin_allowed fails closed ---------------------------------------------


class _WS:
    def __init__(self, origin: str | None, host: str | None = "brain.example.com") -> None:
        self.headers: dict[str, str] = {}
        if origin is not None:
            self.headers["origin"] = origin
        if host is not None:
            self.headers["host"] = host


def test_origin_allowed_without_allow_list_requires_origin_equal_host() -> None:
    s = Settings(database_url=_DB)
    assert origin_allowed(_WS("https://brain.example.com"), s) is True  # type: ignore[arg-type]
    assert origin_allowed(_WS("https://brain.example.com:443"), s) is True  # type: ignore[arg-type]
    assert origin_allowed(_WS("https://x-preview.brain.example.com"), s) is False  # type: ignore[arg-type]
    assert origin_allowed(_WS("https://evil.example"), s) is False  # type: ignore[arg-type]
    assert origin_allowed(_WS("null"), s) is False  # type: ignore[arg-type]
    # Native clients send no Origin; a present Origin with no Host to compare fails closed.
    assert origin_allowed(_WS(None), s) is True  # type: ignore[arg-type]
    assert origin_allowed(_WS(None, host=None), s) is True  # type: ignore[arg-type]
    assert origin_allowed(_WS("https://brain.example.com", host=None), s) is False  # type: ignore[arg-type]


def test_origin_allowed_configured_allow_list_wins() -> None:
    s = Settings(database_url=_DB, dashboard_allowed_origins="https://dash.example")
    assert origin_allowed(_WS("https://dash.example"), s) is True  # type: ignore[arg-type]
    # Even the request's own host is refused when an allow-list names something else.
    assert origin_allowed(_WS("https://brain.example.com"), s) is False  # type: ignore[arg-type]
    assert origin_allowed(_WS(None), s) is True  # type: ignore[arg-type]


# --- the __Host- cookie and its migration ------------------------------------


def _set_cookies(resp: object) -> list[str]:
    return resp.headers.get_list("set-cookie")  # type: ignore[attr-defined]


def test_login_sets_the_host_prefixed_cookie(secure_client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(auth_service.rotate_owner_key(repo))
    resp = secure_client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    assert resp.status_code == 204
    [cookie] = _set_cookies(resp)
    assert cookie.startswith(f"{_HOST}=")
    lowered = cookie.lower()
    assert "secure" in lowered and "path=/" in lowered and "domain" not in lowered
    assert "httponly" in lowered and "samesite=lax" in lowered
    assert secure_client.get("/api/auth/me").json()["kind"] == "owner"


def test_dev_keeps_the_legacy_name(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(auth_service.rotate_owner_key(repo))
    resp = client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    assert [c.split("=", 1)[0] for c in _set_cookies(resp)] == [_LEGACY]
    # And a legacy-cookie request in dev is never "migrated".
    token = client.cookies.get(_LEGACY) or ""
    client.cookies.clear()
    assert _set_cookies(client.get("/api/auth/me", headers=_cookie(token))) == []


def test_legacy_owner_cookie_migrates_silently(
    secure_client: TestClient, repo: FakeAuthRepo
) -> None:
    token = _owner_token(repo)
    resp = secure_client.get("/api/auth/me", headers=_cookie(token))
    assert resp.status_code == 200 and resp.json()["kind"] == "owner"
    cookies = _set_cookies(resp)
    reissued = next(c for c in cookies if c.startswith(f"{_HOST}="))
    assert reissued.startswith(f"{_HOST}={token};")
    assert "samesite=lax" in reissued.lower() and "max-age=31536000" in reissued.lower()
    assert "secure" in reissued.lower() and "path=/" in reissued.lower()
    deleted = next(c for c in cookies if c.startswith(f"{_LEGACY}="))
    assert "max-age=0" in deleted.lower()


def test_legacy_member_cookie_keeps_working_but_is_not_promoted(
    secure_client: TestClient, repo: FakeAuthRepo
) -> None:
    # A device key's holder can mint their own member session and plant it from a preview,
    # so it is never promoted; the dashboard mints a `__Host-` cookie on every launch anyway.
    token = _device_token(repo)
    resp = secure_client.get("/api/auth/me", headers=_cookie(token))
    assert resp.json()["kind"] == "device_key"
    assert _set_cookies(resp) == []


def test_preview_traffic_is_not_judged_by_a_planted_session_cookie(
    secure_client: TestClient,
) -> None:
    # The preview routes read no cookie and strip it; a parent-domain cookie planted by one
    # preview must not 403 every preview load the PWA makes (a same-site navigation).
    headers = {**_cookie("planted"), "sec-fetch-site": "same-site"}
    resp = secure_client.get("/__jcode_preview/0123456789abcdef/index.html", headers=headers)
    assert resp.status_code != 403


def test_failed_auth_does_not_migrate(secure_client: TestClient) -> None:
    resp = secure_client.get("/api/auth/me", headers=_cookie("not-a-session"))
    assert resp.status_code == 401
    assert _set_cookies(resp) == []


def test_host_cookie_wins_over_a_tossed_legacy_cookie(
    secure_client: TestClient, repo: FakeAuthRepo
) -> None:
    # A sibling subdomain can plant a parent-domain `jbrain_session` (here, its own member
    # session); it must neither displace the owner's session nor trigger a migration.
    owner = _owner_token(repo)
    planted = _device_token(repo)
    resp = secure_client.get(
        "/api/auth/me", headers={"cookie": f"{_LEGACY}={planted}; {_HOST}={owner}"}
    )
    assert resp.json()["kind"] == "owner"
    assert _set_cookies(resp) == []


def _share_token(repo: FakeAuthRepo, kind: str) -> str:
    async def go() -> str:
        await repo.create_principal(kind, keys.hash_key(f"{kind}-secret"), "stranger")
        principal = repo.principals[-1]
        token = keys.generate_session_token()
        await repo.create_session(principal.id, keys.hash_token(token), "stranger")
        return token

    return asyncio.run(go())


@pytest.mark.parametrize("kind", ["jcode_share_link", "intake_link"])
def test_share_and_intake_sessions_are_never_promoted(
    secure_client: TestClient, repo: FakeAuthRepo, kind: str
) -> None:
    # They are what an attacker can mint for themselves; promoting a tossed one to
    # `__Host-` would pin the victim's browser to the attacker's session.
    token = _share_token(repo, kind)
    resp = secure_client.get("/api/auth/me", headers=_cookie(token))
    assert resp.status_code == 200 and resp.json()["kind"] == kind
    assert _set_cookies(resp) == []


def test_duplicated_legacy_cookie_authenticates_nobody(
    secure_client: TestClient, repo: FakeAuthRepo
) -> None:
    # Two `jbrain_session` values: the host-only one and a parent-domain one a preview
    # tossed. Neither can be told apart, so neither is trusted, and nothing migrates.
    owner = _owner_token(repo)
    planted = _owner_token(repo)
    resp = secure_client.get(
        "/api/auth/me", headers={"cookie": f"{_LEGACY}={planted}; {_LEGACY}={owner}"}
    )
    assert resp.status_code == 401
    assert _set_cookies(resp) == []


def test_logout_deletes_both_names(secure_client: TestClient, repo: FakeAuthRepo) -> None:
    token = _owner_token(repo)
    resp = secure_client.delete("/api/auth/session", headers=_cookie(token, _HOST))
    assert resp.status_code == 204
    cookies = _set_cookies(resp)
    assert {c.split("=", 1)[0] for c in cookies} == {_HOST, _LEGACY}
    assert all("max-age=0" in c.lower() for c in cookies)
    # A browser ignores a `__Host-` deletion that is not itself Secure with Path=/.
    host_delete = next(c for c in cookies if c.startswith(f"{_HOST}=")).lower()
    assert "secure" in host_delete and "path=/" in host_delete and "domain" not in host_delete
    # The session itself is revoked, whichever name carried it.
    assert asyncio.run(auth_service.authenticate(repo, token)) is None


def test_legacy_logout_revokes_without_a_competing_reissue(
    secure_client: TestClient, repo: FakeAuthRepo
) -> None:
    token = _owner_token(repo)
    resp = secure_client.delete("/api/auth/session", headers=_cookie(token))
    assert asyncio.run(auth_service.authenticate(repo, token)) is None
    assert all("max-age=0" in c.lower() for c in _set_cookies(resp))


def test_dashboard_mint_emits_exactly_one_cookie(
    secure_client: TestClient, repo: FakeAuthRepo
) -> None:
    # The Android minter hands the WebView only the single Set-Cookie OkHttp returns.
    asyncio.run(repo.create_principal("device_key", keys.hash_key("dev-key"), "phone", "sub-1"))
    resp = secure_client.post("/api/session/mint", json={"device_key": "dev-key"})
    assert resp.status_code == 204
    [cookie] = _set_cookies(resp)
    assert cookie.startswith(f"{_HOST}=") and "samesite=strict" in cookie.lower()


def test_a_route_that_writes_the_session_cookie_itself_wins_over_migration() -> None:
    settings = Settings(secure_cookies=True, database_url=_DB)
    scope = {"type": "http", "headers": [(b"cookie", f"{_LEGACY}=old".encode())]}
    owner = PrincipalInfo(id="o", kind="owner", label="")
    mark_for_migration(HTTPConnection(scope), settings, "old", owner)
    own = [(b"set-cookie", f"{_HOST}=new; Path=/; Secure".encode())]
    headers = MutableHeaders(scope={"headers": list(own)})
    apply_migration(scope["state"], headers, settings)  # type: ignore[arg-type]
    assert headers.getlist("set-cookie") == [f"{_HOST}=new; Path=/; Secure"]


# --- #1706: the allow-list and the cookie-free routes ------------------------


def test_a_configured_dashboard_origin_reaches_the_live_feed(repo: FakeAuthRepo) -> None:
    # The owner-configured allow-list names an origin the guard would otherwise refuse;
    # the guard must defer to it, as `origin_allowed` does.
    settings = Settings(
        secure_cookies=False, database_url=_DB, dashboard_allowed_origins="https://dash.example"
    )
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.auth_repo = repo
        token = _owner_token(repo)
        headers = {
            **_cookie(token),
            "origin": "https://dash.example",
            "sec-fetch-site": "cross-site",
        }
        try:
            with c.websocket_connect("/api/locations/live", headers=headers):
                pass
        except WebSocketDisconnect as exc:
            assert exc.code != 4403
        # An origin the owner did not list is still refused by the guard.
        headers["origin"] = "https://evil.example"
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            c.websocket_connect("/api/locations/live", headers=headers),
        ):
            pass
        assert refused.value.code == 4403


@pytest.mark.parametrize(
    "path",
    [
        "/api/feed/appointments.ics?token=nope",
        "/api/research-share/tok",
        "/api/jlaunch-share/tok",
        "/api/mc-share/tok",
    ],
)
def test_a_cookie_free_route_opened_from_another_site_is_not_refused(
    repo: FakeAuthRepo, path: str
) -> None:
    # The owner opens a feed or share link from webmail: the browser attaches the Lax
    # cookie to the top-level GET, but these routes never read it. Past the guard they hit
    # the (absent) database, so anything but the guard's refusal proves the point.
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.auth_repo = repo
        token = _owner_token(repo)
        headers = {**_cookie(token), "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
        resp = c.get(path, headers=headers)
    assert not (resp.status_code == 403 and resp.json() == _REFUSED)


def test_a_cookie_reading_route_beside_the_feed_is_still_guarded(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    token = _owner_token(repo)
    headers = {**_cookie(token), "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
    resp = client.get("/api/feed/appointments", headers=headers)
    assert resp.status_code == 403 and resp.json() == _REFUSED
    resp = client.post(
        "/api/feed/appointments/rotate", headers={**_cookie(token), "sec-fetch-site": "same-site"}
    )
    assert resp.status_code == 403 and resp.json() == _REFUSED


def test_the_guards_cookie_free_routes_never_read_the_cookie() -> None:
    # Coupled to the route matrix over every real route: a GET the guard waves through
    # must be one the matrix has classified as authenticating by something other than
    # the session cookie, so a new cookie-reading GET under a share prefix fails here.
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    waved = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in route.methods and cookie_free_get(route.path)
    }
    assert waved, "no route matched; the prefixes have drifted from the routers"
    exempt_gets = {path for (method, path) in COOKIE_EXEMPT if method == "GET"}
    assert waved <= exempt_gets, sorted(waved - exempt_gets)
    assert not cookie_free_get("/api/feed/appointments")
    assert not cookie_free_get("/api/feed/appointments/rotate")


def test_a_listed_dashboard_origin_still_cannot_open_a_shell(repo: FakeAuthRepo) -> None:
    settings = Settings(
        secure_cookies=False, database_url=_DB, dashboard_allowed_origins="https://dash.example"
    )
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.auth_repo = repo
        token = _owner_token(repo)
        headers = {
            **_cookie(token),
            "origin": "https://dash.example",
            "sec-fetch-site": "same-site",
        }
        for path in ("/api/jcode/sessions/abc/terminal", "/api/jlaunch/jobs/abc/terminal"):
            with (
                pytest.raises(WebSocketDisconnect) as exc,
                c.websocket_connect(path, headers=headers),
            ):
                pass
            assert exc.value.code == 4403, path
