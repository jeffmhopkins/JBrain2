"""The GrowBot site through the real Caddyfile in a real Caddy (the stack from test_owner_site).

The site reuses the LAN name on :8444, and browsers do not scope cookies by port, so the
owner's api session cookie arrives with every GrowBot request. What keeps it from the vendored
app is the proxy rebuilding the Cookie header; that is observed here at the upstream, not
inferred from config text. Security path (docs/plans/GROWBOT_PLAN.md §2).
"""

from __future__ import annotations

import json
import socket
import ssl

from tests.unit.test_owner_site import _Echo, _Stack, key_dirs, stack  # noqa: F401


def _get(s: _Stack, path: str, cookie: str | None) -> tuple[bytes, bytes]:
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", s.ports["growbot"]), timeout=5)
    sent = f"Cookie: {cookie}\r\n" if cookie is not None else ""
    with tls.wrap_socket(raw, server_hostname="jbrain.local") as conn:
        conn.sendall(
            f"GET {path} HTTP/1.1\r\nHost: jbrain.local:{s.ports['growbot']}\r\n{sent}"
            "Connection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


def test_only_the_growbot_cookie_reaches_growbot(stack: _Stack) -> None:  # noqa: F811
    stack.start()
    owner = "__Host-jbrain_session=owner-secret; jbrain_session=legacy; gb_stage=agent"
    head, body = _get(stack, "/v3/next.html", f"{owner}; gb_session=abc.def.ghi")
    assert head.startswith(b"HTTP/1.1 200"), head
    seen = json.loads(body)
    assert seen["cookie"] == "gb_session=abc.def.ghi"
    assert "x-jbrain-ingress" not in seen
    # growbot's same-origin check compares Origin with the Host the browser sent.
    assert seen["host"] == f"jbrain.local:{stack.ports['growbot']}"

    head, body = _get(stack, "/v3/next.html", owner)
    assert "cookie" not in json.loads(body)
    assert "owner-secret" not in body.decode()

    # A name that merely ends in gb_session is not growbot's cookie.
    head, body = _get(stack, "/v3/next.html", "xgb_session=owner-secret; " + owner)
    assert "cookie" not in json.loads(body)

    head, body = _get(stack, "/", None)
    assert "cookie" not in json.loads(body)


def test_the_growbot_site_never_routes_internal(stack: _Stack) -> None:  # noqa: F811
    stack.start()
    before = _Echo.hits
    for path in ("/internal/growbot/ticket/redeem", "/internal", "/internal/../internal/x"):
        head, _ = _get(stack, path, "gb_session=x")
        assert head.startswith(b"HTTP/1.1 404"), (path, head)
    assert _Echo.hits == before


def test_the_growbot_site_does_not_serve_the_app(stack: _Stack) -> None:  # noqa: F811
    # Everything goes to growbot: the PWA's /api is not reachable on this origin.
    stack.start()
    head, body = _get(stack, "/api/auth/session", "gb_session=x")
    assert head.startswith(b"HTTP/1.1 200")
    assert "x-jbrain-ingress" not in json.loads(body)


def _away(s: _Stack, path: str, cookie: str | None, host: str = "growbot.preview.test") -> bytes:
    """A request as the tunnel delivers it: plain HTTP on Caddy's HTTP port, by name."""
    raw = socket.create_connection(("127.0.0.1", s.ports["http"]), timeout=5)
    sent = f"Cookie: {cookie}\r\n" if cookie is not None else ""
    with raw:
        raw.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{sent}Connection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := raw.recv(65536):
            data += chunk
    return data


def test_the_away_name_serves_growbot_with_its_cookie_alone(stack: _Stack) -> None:  # noqa: F811
    stack.start()
    data = _away(stack, "/v3/next.html", "jbrain_x=owner-secret; gb_session=abc.def.ghi")
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head
    seen = json.loads(body)
    assert seen["cookie"] == "gb_session=abc.def.ghi"
    # growbot's same-origin check compares Origin with this Host.
    assert seen["host"] == "growbot.preview.test"
    assert "x-jbrain-ingress" not in seen
    head, _, body = _away(stack, "/", "jbrain_x=owner-secret").partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200") and "cookie" not in json.loads(body)


def test_the_away_name_shuts_the_home_only_doors(stack: _Stack) -> None:  # noqa: F811
    stack.start()
    before = _Echo.hits
    # The setup page and certificate are for a phone at home, and /internal is never routed.
    for path in ("/internal/growbot/ticket/redeem", "/setup.html", "/setup.js", "/box-root.crt"):
        for cookie in (None, "gb_session=x"):
            assert _away(stack, path, cookie).startswith(b"HTTP/1.1 404"), (path, cookie)
    # A robot dials the relay on the LAN; through the tunnel only a signed-in phone may.
    assert _away(stack, "/d/gb-abc123", None).startswith(b"HTTP/1.1 404")
    assert _Echo.hits == before
    assert _away(stack, "/d/gb-abc123", "gb_session=x").startswith(b"HTTP/1.1 200")
    # A session-less door the phone needs to sign in still opens.
    assert _away(stack, "/open?ticket=t", None).startswith(b"HTTP/1.1 200")
    # The wildcard still answers every other name with nothing.
    assert _away(stack, "/", None, host="other.preview.test").startswith(b"HTTP/1.1 404")
