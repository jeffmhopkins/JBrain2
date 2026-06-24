#!/usr/bin/env python3
"""One-time OAuth bootstrap for the Gmail `archivist` persona
(docs/EMAIL_ARCHIVIST_PLAN.md — see the "OAuth setup" appendix for the full
click-path).

Runs the authorization-code flow with a **loopback redirect** (Google deprecated the
OOB copy-paste flow): it starts a throwaway HTTP server on 127.0.0.1, opens the
consent page for the `gmail.modify` scope, captures the redirected code locally, and
exchanges it for a long-lived refresh token — which it prints for you to paste into
the box's config. Stdlib only, so it runs from a laptop without the project venv.

Usage:
    JBRAIN_GMAIL_CLIENT_ID=... JBRAIN_GMAIL_CLIENT_SECRET=... \\
        python scripts/gmail-oauth-bootstrap.py

The refresh token is the only long-lived secret; the local server and the access
token are ephemeral. If it ever stops working (revoke, ~6 months disuse, or an app
left in "Testing" status — 7-day expiry), just re-run this.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# A fixed loopback port keeps the redirect URI stable; loopback redirects on any port
# are allowed for "Desktop app" OAuth clients without pre-registration.
PORT = 8731
REDIRECT_URI = f"http://127.0.0.1:{PORT}/"

_DONE_PAGE = (
    b"<html><body><h2>JBrain archivist connected.</h2>"
    b"<p>You can close this tab and return to the terminal.</p></body></html>"
)


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    """Capture the `?code=` Google appends to the loopback redirect, then stop."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler signature)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (params.get("code") or [None])[0]
        _CodeCatcher.error = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *_args: object) -> None:
        pass  # silence the default per-request stderr logging


def _exchange_for_refresh_token(client_id: str, client_secret: str, code: str) -> str:
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as resp:
        payload = json.load(resp)
    token = payload.get("refresh_token")
    if not token:
        raise SystemExit(
            "No refresh_token returned. Ensure the consent was fresh (the script asks "
            "for access_type=offline & prompt=consent); revoke prior access at "
            "myaccount.google.com and retry."
        )
    return str(token)


def main() -> None:
    client_id = os.environ.get("JBRAIN_GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("JBRAIN_GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Set JBRAIN_GMAIL_CLIENT_ID and JBRAIN_GMAIL_CLIENT_SECRET (from the "
            "Google Cloud OAuth client) before running this."
        )

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # force it even if previously granted
        }
    )
    print(f"Opening the consent page in your browser:\n  {auth_url}\n")
    print("If it doesn't open, paste that URL in manually.\n")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("127.0.0.1", PORT), _CodeCatcher)
    server.handle_request()  # serve exactly the one redirect, then return
    server.server_close()

    if _CodeCatcher.error or not _CodeCatcher.code:
        raise SystemExit(f"Authorization failed: {_CodeCatcher.error or 'no code received'}")

    refresh_token = _exchange_for_refresh_token(
        client_id, client_secret, _CodeCatcher.code
    )
    print("\nSuccess. Paste these into the box's config / .env:\n")
    print(f"JBRAIN_GMAIL_CLIENT_ID={client_id}")
    print(f"JBRAIN_GMAIL_CLIENT_SECRET={client_secret}")
    print(f"JBRAIN_GMAIL_REFRESH_TOKEN={refresh_token}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
