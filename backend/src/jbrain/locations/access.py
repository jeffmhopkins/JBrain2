"""The full-owner gate shared by every location read surface.

Two of the tables the location reads touch — `app.events` and `place_geofence` —
are gated in RLS by only `has_domain_scope`, which passes for ANY owner session
(including a narrowed `owner_scoped` agent context) and even a non-owner holding
the `location` scope. RLS therefore does NOT fail-close those reads: a missed
check is a real leak, not a harmless empty result. So every method touching them
calls `require_full_owner` first — the primary barrier, not a backstop (mirrors
`app.is_full_owner()` at the RLS layer and `geocodetools._is_full_owner`).
"""

from jbrain.db.session import SessionContext


class LocationToolRefusal(Exception):
    """A location read was attempted from a non-full-owner session. Its message
    is safe to surface to the model: location is owner-only and the narrowed
    session simply does not have it."""

    def __init__(
        self,
        message: str = "location is owner-only and isn't available in this session.",
    ) -> None:
        super().__init__(message)


def require_full_owner(ctx: SessionContext) -> None:
    """Raise `LocationToolRefusal` unless `ctx` is a *full* owner — owner identity
    that is not also `owner_scoped`. The one barrier the weak-table reads rely on."""
    if not (ctx.principal_kind == "owner" and not ctx.owner_scoped):
        raise LocationToolRefusal()
