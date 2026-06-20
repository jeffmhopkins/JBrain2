"""Content-free push (JBrain360 M6).

The `fcm_token` registry (M6a) plus the routing/sender (M6b) that turns a location
event into a *content-free* poke to the members who may see it. The poke carries no
PII — it wakes the app, which fetches the real notification over its authenticated
channel. The sender is an abstraction (faked in tests); the routing is
view-scope-aware.
"""

from jbrain.push.repo import FcmTokenRepo, SqlFcmTokenRepo
from jbrain.push.router import PushRouter
from jbrain.push.sender import FcmNotifier, NullNotifier, PushNotifier, fcm_message

__all__ = [
    "FcmNotifier",
    "FcmTokenRepo",
    "NullNotifier",
    "PushNotifier",
    "PushRouter",
    "SqlFcmTokenRepo",
    "fcm_message",
]
