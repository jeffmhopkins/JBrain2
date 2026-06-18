"""MQTT topic authorization — the M0 deny-by-default floor.

The full family-group view-scope ACL is M2 (it extends this with the subjects a
device is permitted to *view*, read from `view_scope` under RLS — plan T3). M0
enforces only the floor: a device may touch **its own OwnTracks namespace and
nothing else**, so a stolen URL or a valid key used under a forged identity sees
no one. The namespace is keyed on the device's authenticated identity — the MQTT
username, which `/internal/mqtt-auth` binds to the device principal id, so by the
time the ACL runs `username` is trusted (go-auth does not re-send the password to
the ACL check).
"""

OWNTRACKS_ROOT = "owntracks"


def _own_prefix(username: str) -> str:
    # Trailing slash is load-bearing: it stops a prefix-confusion match where
    # username "dad" would otherwise authorize "owntracks/daddio/...".
    return f"{OWNTRACKS_ROOT}/{username}/"


def authorize_topic(username: str, topic: str) -> bool:
    """True iff `topic` is within the caller's own OwnTracks namespace.

    Allows the device's base topic `owntracks/<username>` and anything beneath it
    (its `<device>` pub topic, the `/cmd` subtree, a `owntracks/<username>/+`
    subscribe filter). Denies everything else — another subject's namespace, a
    bare `owntracks/+/+` / `#` wildcard, a non-owntracks topic — deny-by-default.

    `acc` (read vs write) is intentionally not consulted in M0: both directions
    are confined to the own namespace, so the publish/subscribe split adds no
    security here and arrives meaningfully only with view-scope in M2.
    """
    if not username or not topic:
        return False
    return topic == f"{OWNTRACKS_ROOT}/{username}" or topic.startswith(_own_prefix(username))
