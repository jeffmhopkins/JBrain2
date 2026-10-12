"""The session cookie's name, reading and writing — one place for every reader and setter.

With `secure_cookies` on (every real deploy) the cookie is `__Host-<session_cookie>`. The
prefix makes the browser refuse it unless it is Secure, Path=/ and host-only, so a page on
a sibling subdomain (a jcode preview at `<slug>-preview.<host>`) can neither set nor
shadow it with a parent-domain `jbrain_session` of its own (cookie tossing).

Cookies minted before the rename still carry the legacy name and keep working until the
`legacy_session_cookie_until` sunset: an owner or member request that authenticates by the
legacy cookie alone is marked here, and `SameSiteGuard` re-issues the same token under the
`__Host-` name and deletes the legacy one on the way out, so no device signs in again.

The legacy name is exactly what a sibling subdomain CAN set, so it is read defensively:
- once the `__Host-` cookie is present the legacy name is never read;
- two legacy values in one Cookie header mean one was tossed onto the parent domain (a
  host-only delete cannot remove that one), so neither is trusted;
- only an owner or member session is promoted to `__Host-`. Share and intake sessions are
  what an attacker can mint for themselves, so promoting one would pin the victim's browser
  to the attacker's session (fixation); they keep the legacy name until they lapse.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import Response

from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings

SameSite = Literal["lax", "strict", "none"]

_HOST_PREFIX = "__Host-"
# Both migratable kinds were first issued for a year (api/auth.py, api/session_bridge.py).
_YEAR = 60 * 60 * 24 * 365
_MIGRATION_KEY = "session_cookie_migration"
_MIGRATABLE: dict[str, SameSite] = {"owner": "lax", "device_key": "strict"}


@dataclass(frozen=True)
class _Reissue:
    token: str
    samesite: SameSite


def host_cookie_name(settings: Settings) -> str:
    return f"{_HOST_PREFIX}{settings.session_cookie}"


def cookie_name(settings: Settings) -> str:
    """The name new cookies are written under. The prefix needs Secure, so plain-HTTP dev
    and the test suite keep the legacy name."""
    return host_cookie_name(settings) if settings.secure_cookies else settings.session_cookie


def session_cookie_names(settings: Settings) -> tuple[str, str]:
    return host_cookie_name(settings), settings.session_cookie


def _legacy_honoured(settings: Settings) -> bool:
    if not settings.secure_cookies:
        return True
    return datetime.now(UTC).date() <= settings.legacy_session_cookie_until


def _raw_count(conn: HTTPConnection, name: str) -> int:
    # `conn.cookies` keeps one value per name, hiding a duplicate; the raw header does not.
    raw = conn.headers.get("cookie", "")
    return sum(1 for part in raw.split(";") if part.split("=", 1)[0].strip() == name)


def session_token(conn: HTTPConnection, settings: Settings) -> str:
    """The session token on a request or WS handshake: the `__Host-` cookie when present,
    else a single, unambiguous legacy cookie while the legacy name is honoured."""
    token = conn.cookies.get(host_cookie_name(settings))
    if token is not None:
        return token
    if not _legacy_honoured(settings) or _raw_count(conn, settings.session_cookie) != 1:
        return ""
    return conn.cookies.get(settings.session_cookie, "")


def set_session_cookie(
    response: Response,
    settings: Settings,
    token: str,
    *,
    samesite: SameSite,
    max_age: int | None,
) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite=samesite,
    )


def _delete_legacy(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie,
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )


def delete_session_cookies(response: Response, settings: Settings) -> None:
    """Expire both names. The `__Host-` deletion must itself be Secure with Path=/, or the
    browser rejects it like any other `__Host-` cookie and the session cookie survives."""
    response.delete_cookie(
        host_cookie_name(settings), path="/", httponly=True, secure=True, samesite="lax"
    )
    _delete_legacy(response, settings)


def mark_for_migration(
    conn: HTTPConnection, settings: Settings, token: str, principal: PrincipalInfo
) -> None:
    """Record that this request authenticated an owner or member by the legacy cookie
    alone, so the response re-issues it under the `__Host-` name. `session_token` has
    already refused an ambiguous (duplicated) legacy cookie, so `token` is the only one."""
    samesite = _MIGRATABLE.get(principal.kind)
    if samesite is None or not settings.secure_cookies or not token:
        return
    if host_cookie_name(settings) in conn.cookies:
        return
    conn.scope.setdefault("state", {})[_MIGRATION_KEY] = _Reissue(token, samesite)


def apply_migration(
    scope_state: dict[str, object], headers: MutableHeaders, settings: Settings
) -> None:
    """Append the re-issue + legacy-delete `Set-Cookie` headers for a marked request —
    unless the route already wrote a session cookie itself (a login or logout), whose
    intent wins."""
    marked = scope_state.get(_MIGRATION_KEY)
    if not isinstance(marked, _Reissue):
        return
    names = tuple(f"{n}=" for n in session_cookie_names(settings))
    if any(v.startswith(names) for v in headers.getlist("set-cookie")):
        return
    carrier = Response()
    set_session_cookie(carrier, settings, marked.token, samesite=marked.samesite, max_age=_YEAR)
    _delete_legacy(carrier, settings)
    for key, value in carrier.raw_headers:
        if key == b"set-cookie":
            headers.append("set-cookie", value.decode("latin-1"))
