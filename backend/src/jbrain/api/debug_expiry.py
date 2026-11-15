"""Telling a debug-token holder when its token dies.

A token minted with the default 24 h box lapsed at 04:00 UTC while an assistant was
running unattended training on it, and nothing on the surface had ever said when that
would happen: every call worked until the one that 401'd "invalid debug token". So every
authenticated `/api/debug/*` response now carries the expiry in headers, `GET /whoami`
carries it in the body, and inside the last day a plain-words warning rides along, early
enough for the holder to ask the owner for a fresh token before the work stalls.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

EXPIRES_AT_HEADER = "X-Debug-Token-Expires-At"
EXPIRES_IN_HEADER = "X-Debug-Token-Expires-In"
WARNING_HEADER = "X-Debug-Token-Warning"

# A day is long enough to finish what is running and get a new token handed over, and the
# owner may be asleep for a third of it.
WARN_WITHIN = timedelta(hours=24)

_RENEW = "ask the owner to mint a new one in Settings → Debug access (Claude)"


@dataclass(frozen=True)
class TokenExpiry:
    expires_at: datetime | None
    # Whole seconds left, floored at 0; None for a token that never expires.
    expires_in_seconds: int | None
    warning: str | None


def iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _span(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h {rem // 60}m" if hours else f"{rem // 60}m"


def token_expiry(expires_at: datetime | None, now: datetime | None = None) -> TokenExpiry:
    if expires_at is None:
        return TokenExpiry(None, None, None)
    left = max(0, int((expires_at - (now or datetime.now(UTC))).total_seconds()))
    warning = None
    if left < WARN_WITHIN.total_seconds():
        warning = f"debug token expires in {_span(left)} (at {iso(expires_at)}); {_RENEW}"
    return TokenExpiry(expires_at, left, warning)


def expiry_headers(expiry: TokenExpiry) -> dict[str, str]:
    if expiry.expires_at is None:
        return {EXPIRES_AT_HEADER: "never"}
    headers = {
        EXPIRES_AT_HEADER: iso(expiry.expires_at),
        EXPIRES_IN_HEADER: str(expiry.expires_in_seconds),
    }
    if expiry.warning:
        # HTTP header values are latin-1; the arrow in the PWA path is not.
        headers[WARNING_HEADER] = expiry.warning.replace("→", ">")
    return headers


def lapsed_detail(expired_at: datetime) -> str:
    return f"debug token expired at {iso(expired_at)}; {_RENEW}"
