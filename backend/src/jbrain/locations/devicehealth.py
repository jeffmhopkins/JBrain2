"""Device liveness judgment: is an operated device's tracker still reporting, or has
it gone dark? The owner's read of "is my JBrain360 app failing in the background"
(it pairs with the Android revival watchdog on the device side).

Pure and deterministic — it takes `now` rather than reading the clock — so the
silence rule is unit-tested without a DB or wall-clock. The API layer feeds it the
device's last-fix time and stamps the result onto each device summary; nothing here
touches the database or the location firewall.
"""

from datetime import datetime

# A healthy device heartbeats at least every 15 min (the stationary `HEARTBEAT_MS`
# the Android service enforces), so a gap this long is four missed heartbeats — well
# past a brief indoor GPS lull, the point where "dark" is a real signal rather than
# noise. Deliberately longer than presence's 30-min coarse-staleness horizon: presence
# asks "is this fix fresh enough to report a place", silence asks "has the tracker
# stopped", and the latter wants to avoid crying wolf over a short outage.
SILENT_AFTER_SECONDS = 60 * 60.0


def fix_age_seconds(last_seen: datetime | None, now: datetime) -> float | None:
    """Seconds since the device's latest fix, or None when it has never reported.
    Clamped at zero so a fix stamped slightly in the future (clock skew) never reads
    as a negative age."""
    if last_seen is None:
        return None
    return max(0.0, (now - last_seen).total_seconds())


def is_silent(
    last_seen: datetime | None,
    now: datetime,
    *,
    revoked: bool,
    horizon_seconds: float = SILENT_AFTER_SECONDS,
) -> bool:
    """True when an active device that HAS reported has now been quiet past the
    horizon. A revoked device (intentionally retired) is never "silent" — its quiet is
    expected — and a device that has never reported is "not set up yet", not dark, so
    both return False; the summary's `revoked`/`last_seen` already tell those apart."""
    if revoked or last_seen is None:
        return False
    age = fix_age_seconds(last_seen, now)
    return age is not None and age > horizon_seconds
