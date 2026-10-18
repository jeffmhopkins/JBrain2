"""JShare's links through the real Caddyfile in a real Caddy (the stack from test_owner_site).

What the api's sandbox promises only holds if the proxy delivers it: `/s/*` reaches the api
on the public site alone, without the owner's cookie, and the sandbox CSP the api answers
with reaches the browser intact beside the app's own frame-ancestors policy.
"""

from __future__ import annotations

import json
import socket

import pytest

from jbrain.api.jshare_public import SANDBOX
from tests.unit.test_owner_site import _Echo, _Stack, key_dirs, stack  # noqa: F401

pytestmark = pytest.mark.usefixtures("_sandboxing_upstream")


@pytest.fixture
def _sandboxing_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    def do_get(self: _Echo) -> None:
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Security-Policy", SANDBOX)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    monkeypatch.setattr(_Echo, "do_GET", do_get)


def _get(s: _Stack, port: str, host: str, path: str) -> tuple[str, list[str], bytes]:
    raw = socket.create_connection(("127.0.0.1", s.ports[port]), timeout=5)
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
        "Cookie: __Host-jbrain_session=owner-secret; jbrain_session=legacy\r\n"
        "Connection: close\r\n\r\n"
    )
    with raw:
        raw.sendall(request.encode())
        data = b""
        while chunk := raw.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    return lines[0], lines[1:], body


def test_the_public_site_proxies_shares_without_the_cookie(stack: _Stack) -> None:  # noqa: F811
    stack.start()
    status, headers, body = _get(stack, "public", "localhost", "/s/" + "a" * 26 + "/x.js")
    assert status.startswith("HTTP/1.1 200"), status
    seen = json.loads(body)
    assert "cookie" not in seen
    assert seen["x-jbrain-ingress"] == "public"
    csp = [h.split(":", 1)[1].strip() for h in headers if h.lower().startswith("content-security")]
    assert SANDBOX in csp, csp


def test_the_lan_site_does_not_serve_shares(stack: _Stack) -> None:  # noqa: F811
    import ssl

    stack.start()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", stack.ports["lan"]), timeout=5)
    with tls.wrap_socket(raw, server_hostname="jbrain.local") as conn:
        path = "/s/" + "a" * 26 + "/"
        conn.sendall(
            f"GET {path} HTTP/1.1\r\nHost: jbrain.local\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
    # The SPA's index, from the file server: the request never reached the api.
    assert data.endswith(b"app"), data[-200:]
