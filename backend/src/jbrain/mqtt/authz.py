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

# go-auth / mosquitto plugin ACL access levels.
ACC_READ = 1
ACC_SUBSCRIBE = 4


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


def topic_namespace_owner(topic: str) -> str | None:
    """The owner segment of an `owntracks/<owner>/...` topic or subscribe filter, or
    None if it isn't a single concrete owner namespace.

    Used to widen a device's ACL to a *group member's* namespace: the owner segment
    is that member's principal id, which the endpoint resolves to a subject and runs
    the view-scope check on. A wildcard in the owner position (`owntracks/+/+`) or a
    foreign root yields None — those are never group-member subscribes.
    """
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == OWNTRACKS_ROOT and parts[1] and parts[1] not in ("+", "#"):
        return parts[1]
    return None


def authorize_ingest_subscribe(topic: str, acc: int) -> bool:
    """The server-side ingest consumer's ACL: read/subscribe the whole `owntracks`
    tree, never publish. It is a trusted internal subscriber (authenticated by a
    service secret, not a device key), so it gets the broad read the per-device floor
    forbids — but a write (`acc` other than read/subscribe) is always denied."""
    if acc not in (ACC_READ, ACC_SUBSCRIBE):
        return False
    return topic == f"{OWNTRACKS_ROOT}/#" or topic.startswith(f"{OWNTRACKS_ROOT}/")
