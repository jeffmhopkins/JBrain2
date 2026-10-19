"""The box's public base URL: what links that leave the box start with
(docs/plans/OWNER_TUNNEL_PLAN.md §3.6, wave A5).

The owner sets it in Settings → Devices, so it needs no host step; `JBRAIN_PUBLIC_BASE_URL`
stays the fallback for a box that set it before. It is changed only from the home network:
the debug-token payload and the share links point people at it, so a stolen session that
could repoint it would send a handed-off token to a host of its choosing.
"""

import re
from typing import Any
from urllib.parse import urlsplit

import structlog

from jbrain.config import Settings
from jbrain.db.session import SessionContext

log = structlog.get_logger()

PUBLIC_BASE_URL_KEY = "public_base_url"
# A DNS name or an IPv6 literal. urlsplit keeps a space, `\\` or `%` in the host, and a browser
# reads `https://evil.example\\x` as that host plus a path.
_HOST = re.compile(r"[A-Za-z0-9.-]+|[0-9A-Fa-f:]+")
# The setting is the owner's; this is read on routes (the Gmail callback, the box's screen)
# that run before or without the owner's own principal.
_OWNER = SessionContext(principal_kind="owner")


def normalize(raw: str) -> str:
    """An origin, `https://host[:port]`, or "" to fall back to the env. ValueError otherwise:
    a path, query or credentials in it would end up inside every link built from it."""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not _HOST.fullmatch(parts.hostname)
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("not an https origin")
    try:
        _ = parts.port  # raises on a port that is not a number
    except ValueError as exc:
        raise ValueError("not an https origin") from exc
    return value


def env_value(settings: Settings) -> str:
    return settings.public_base_url.strip().rstrip("/")


async def stored(store: Any) -> str:
    value = await store.get(_OWNER, PUBLIC_BASE_URL_KEY, "")
    return value if isinstance(value, str) else ""


async def resolve(store: Any, settings: Settings) -> str:
    """The owner's setting, else the env's, else "" (callers then use the request's origin).
    A failed read, or an app with no store, falls back to the env rather than failing the
    link."""
    try:
        value = "" if store is None else await stored(store)
    except Exception:
        log.warning("public_base.read_failed", exc_info=True)
        value = ""
    return value or env_value(settings)


async def for_request(request: Any, settings: Settings) -> str:
    return await resolve(getattr(request.app.state, "settings_store", None), settings)
