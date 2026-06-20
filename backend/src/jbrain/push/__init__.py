"""Content-free push (JBrain360 M6).

The `fcm_token` registry plus the routing/sender that turns a location event into a
*content-free* poke to the members who may see it. The poke carries no PII — it wakes
the app, which fetches the real notification over its authenticated channel. The
sender is an abstraction (faked in tests); the routing is view-scope-aware.
"""

from jbrain.push.repo import FcmTokenRepo, SqlFcmTokenRepo

__all__ = ["FcmTokenRepo", "SqlFcmTokenRepo"]
