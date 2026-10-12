"""Every route against every non-owner session kind: refused unless explicitly allowed.

Postgres RLS protects the rows, not the side effects around them — loading a model,
spending the Tavily key, writing a blob, reading the Gmail credential under an owner
context. A route written against bare `PrincipalDep` admits ANY session cookie, so a
family phone, a jcode share recipient or an intake-link stranger reached those side
effects (docs/plans/SECURITY_REVIEW_FIXES_PLAN.md F3). This walks the whole app, with
every conditional router mounted, and presents a session cookie of each non-owner kind
to each route. A route must refuse it (HTTP 401/403, a WebSocket closed before accept)
unless the (route, kind) pair is on `ALLOWED`. A new route that is not owner-gated fails
here until someone decides, in this file, who it is for.

Requests are refused before any handler runs, so the fakes need no database; the
allow-listed pairs and the cookie-exempt routes are skipped rather than executed.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jbrain.auth import keys
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakePrincipal, FakeSession

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_COOKIE = "jbrain_session"

# Every principal kind `app.principals.kind` admits (0107's CHECK) except the owner.
NON_OWNER_KINDS = (
    "device_key",
    "jcode_share_link",
    "intake_link",
    "capability_token",
    "external_llm",
)

# Every path parameter is filled with this, and the share principal is scoped to it, so a
# jcode share is presented to its OWN session's routes — the strongest case for the gate.
_PARAM = "1"

_WS = "WS"
_SESSION_HOLDERS = frozenset({"device_key", "jcode_share_link", "intake_link"})

# (method, path) -> the non-owner kinds that route legitimately serves (the flow table in
# the security plan). Anything absent here must refuse every non-owner cookie.
ALLOWED: dict[tuple[str, str], frozenset[str]] = {
    # Every scoped app probes who it is; a capability / external-LLM key never holds a
    # session, so it is not here.
    ("GET", "/api/auth/me"): _SESSION_HOLDERS,
    # Logout authenticates nothing: it revokes whatever cookie it is handed.
    ("DELETE", "/api/auth/session"): frozenset(NON_OWNER_KINDS),
    # The member dashboard (/dash): its map, its reads, its live feed.
    ("GET", "/api/tiles/{scheme}/{z}/{x}/{y}.png"): frozenset({"device_key"}),
    ("GET", "/api/member/roster"): frozenset({"device_key"}),
    ("GET", "/api/member/positions"): frozenset({"device_key"}),
    ("GET", "/api/member/places"): frozenset({"device_key"}),
    ("GET", "/api/member/timeline"): frozenset({"device_key"}),
    ("PUT", "/api/member/fcm-token"): frozenset({"device_key"}),
    ("DELETE", "/api/member/fcm-token"): frozenset({"device_key"}),
    (_WS, "/api/locations/live"): frozenset({"device_key"}),
    # The guided-intake stepper.
    ("POST", "/api/intake/chat"): frozenset({"intake_link"}),
    ("POST", "/api/intake/confirm"): frozenset({"intake_link"}),
    # A jcode share recipient, on its own session only (test_jcode_share_routes pins that a
    # share for another session is refused on these).
    ("GET", "/api/jcode/sessions/{sid}"): frozenset({"jcode_share_link"}),
    ("GET", "/api/jcode/sessions/{sid}/preview"): frozenset({"jcode_share_link"}),
    ("POST", "/api/jcode/sessions/{sid}/preview"): frozenset({"jcode_share_link"}),
    ("DELETE", "/api/jcode/sessions/{sid}/preview"): frozenset({"jcode_share_link"}),
    (_WS, "/api/jcode/sessions/{sid}/terminal"): frozenset({"jcode_share_link"}),
}

# Routes that do not authenticate by the session cookie at all, so a cookie neither opens
# nor closes them and presenting one would run the handler. Each is credentialed some
# other way (or deliberately public) and is tested where that credential is.
# Routes credentialed by a bearer / Basic header are NOT listed: they refuse a cookie-only
# request on their own, and the matrix proves it.
COOKIE_EXEMPT: dict[tuple[str, str], str] = {
    ("GET", "/api/healthz"): "public liveness probe",
    ("GET", "/api/readyz"): "public readiness probe",
    ("POST", "/api/auth/session"): "login: the owner key in the body is the credential",
    ("POST", "/api/session/mint"): "device key in the body mints the member cookie",
    ("POST", "/api/pairing/redeem"): "pairing code in the body; rate-limited",
    ("POST", "/api/intake/redeem"): "intake secret in the body mints the intake cookie",
    ("POST", "/api/jcode/share/redeem"): "share secret in the body mints the share cookie",
    ("GET", "/api/install/grok.ps1"): "public setup script; carries no secret",
    ("GET", "/api/feed/appointments.ics"): "?token= feed secret, constant-time compared",
    ("GET", "/api/research-share/{token}"): "token in path",
    ("GET", "/api/research-share/{token}/reports/{report_id}"): "token in path",
    ("GET", "/api/jlaunch-share/{token}"): "token in path",
    ("GET", "/api/jlaunch-share/{token}/artifact"): "token in path",
    ("GET", "/api/mc-share/{token}"): "token in path",
    ("GET", "/api/mc-share/{token}/map/info"): "token in path",
    ("GET", "/api/mc-share/{token}/map/tile/{dim}/{zoom}/{tx}/{tz}.png"): "token in path",
    ("GET", "/api/mc-share/{token}/map/point"): "token in path",
    ("GET", "/api/mc-share/{token}/map/players"): "token in path",
    ("GET", "/api/mc-share/{token}/map/structures"): "token in path",
    ("GET", "/api/mc-share/{token}/timeline"): "token in path",
    ("GET", "/api/chat-share/{token}"): "token in path",
    ("GET", "/api/chat-share/{token}/favicon"): "token in path",
    ("POST", "/api/ext/llm/{sid}/v1/chat/completions"): "external-LLM bearer; reads no cookie",
    ("GET", "/api/ext/llm/{sid}/v1/models"): "external-LLM bearer; reads no cookie",
    (_WS, "/__jcode_preview/{slug}"): "unguessable slug in path; cookies stripped",
    (_WS, "/__jcode_preview/{slug}/{path:path}"): "unguessable slug in path; cookies stripped",
}
# The HTTP preview proxy answers every method; the slug is its credential.
_PREVIEW_HTTP = ("/__jcode_preview/{slug}", "/__jcode_preview/{slug}/{path:path}")
_FRAMEWORK = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


@dataclass(frozen=True)
class Probe:
    method: str
    path: str


def _settings() -> Settings:
    # Every flag main.create_app mounts a router behind, switched on.
    return Settings(
        secure_cookies=False,
        database_url=_DB,
        session_cookie=_COOKIE,
        debug_access_enabled=True,
        comfyui_url="http://comfyui.invalid:8188",
    )


def _probes(app: FastAPI) -> list[Probe]:
    probes: list[Probe] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            if route.path in _PREVIEW_HTTP:
                continue
            probes.extend(Probe(m, route.path) for m in sorted(route.methods))
        elif isinstance(route, APIWebSocketRoute):
            probes.append(Probe(_WS, route.path))
        else:
            assert getattr(route, "path", None) in _FRAMEWORK, f"unclassified route {route!r}"
    return probes


def _concrete(path: str) -> str:
    out, i = [], 0
    while i < len(path):
        if path[i] == "{":
            i = path.index("}", i) + 1
            out.append(_PARAM)
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def _session(repo: FakeAuthRepo, kind: str) -> str:
    principal = FakePrincipal(
        id=f"p-{kind}",
        kind=kind,
        key_hash=f"k-{kind}",
        label=kind,
        subject_id="subject-1" if kind == "device_key" else "",
        jcode_session_id=_PARAM if kind == "jcode_share_link" else "",
    )
    repo.principals.append(principal)
    token = f"session-{kind}"
    repo.sessions.append(FakeSession(principal.id, keys.hash_token(token), kind))
    return token


@pytest.fixture(scope="module")
def harness() -> Iterator[tuple[FastAPI, TestClient, dict[str, str]]]:
    app = create_app(_settings())
    repo = FakeAuthRepo()
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.auth_repo = repo
        tokens = {kind: _session(repo, kind) for kind in NON_OWNER_KINDS}
        yield app, client, tokens


def _refused(client: TestClient, probe: Probe, token: str) -> str | None:
    """None when the cookie is refused; otherwise what happened instead."""
    url = _concrete(probe.path)
    headers = {"cookie": f"{_COOKIE}={token}"}
    if probe.method == _WS:
        try:
            with client.websocket_connect(url, headers=headers):
                return "WebSocket accepted"
        except WebSocketDisconnect:
            return None
    status = client.request(probe.method, url, headers=headers).status_code
    return None if status in (401, 403) else f"HTTP {status}"


def test_every_conditional_router_is_mounted(
    harness: tuple[FastAPI, TestClient, dict[str, str]],
) -> None:
    # A route that is not mounted is not checked; prove the flags above took.
    paths = {getattr(r, "path", "") for r in harness[0].routes}
    assert "/api/debug/whoami" in paths  # debug_access_enabled
    assert "/api/images/generate" in paths  # comfyui_url


def test_allow_and_exempt_lists_name_real_routes(
    harness: tuple[FastAPI, TestClient, dict[str, str]],
) -> None:
    present = {(p.method, p.path) for p in _probes(harness[0])}
    stale = (set(ALLOWED) | set(COOKIE_EXEMPT)) - present
    assert not stale, f"entries for routes that no longer exist: {sorted(stale)}"
    assert not set(ALLOWED) & set(COOKIE_EXEMPT)


@pytest.mark.parametrize("kind", ["device_key", "jcode_share_link", "intake_link"])
def test_the_harness_cookies_really_authenticate(
    harness: tuple[FastAPI, TestClient, dict[str, str]], kind: str
) -> None:
    # The positive control: if the cookie name or the fake drifted, every probe below would
    # 401 and the matrix would pass while checking nothing.
    _, client, tokens = harness
    resp = client.get("/api/auth/me", headers={"cookie": f"{_COOKIE}={tokens[kind]}"})
    assert resp.status_code == 200


def test_no_route_admits_a_non_owner_cookie_it_was_not_built_for(
    harness: tuple[FastAPI, TestClient, dict[str, str]],
) -> None:
    app, client, tokens = harness
    leaks: list[str] = []
    for probe in _probes(app):
        key = (probe.method, probe.path)
        if key in COOKIE_EXEMPT:
            continue
        for kind in NON_OWNER_KINDS:
            if kind in ALLOWED.get(key, frozenset()):
                continue
            outcome = _refused(client, probe, tokens[kind])
            if outcome is not None:
                leaks.append(f"{probe.method} {probe.path} [{kind}] -> {outcome}")
    assert not leaks, "non-owner cookies admitted:\n" + "\n".join(leaks)


@pytest.mark.parametrize(
    "probe",
    [
        Probe("GET", "/api/jcode/sessions/other"),
        Probe("POST", "/api/jcode/sessions/other/preview"),
        Probe("DELETE", "/api/jcode/sessions/other/preview"),
    ],
)
def test_a_share_cookie_is_refused_on_another_sessions_routes(
    harness: tuple[FastAPI, TestClient, dict[str, str]], probe: Probe
) -> None:
    # The allow-list grants a share its OWN session; the scope check does the rest.
    _, client, tokens = harness
    headers = {"cookie": f"{_COOKIE}={tokens['jcode_share_link']}"}
    assert client.request(probe.method, probe.path, headers=headers).status_code == 403


def test_a_share_cookie_cannot_open_another_sessions_terminal(
    harness: tuple[FastAPI, TestClient, dict[str, str]],
) -> None:
    _, client, tokens = harness
    headers = {"cookie": f"{_COOKIE}={tokens['jcode_share_link']}"}
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/jcode/sessions/other/terminal", headers=headers),
    ):
        pass
