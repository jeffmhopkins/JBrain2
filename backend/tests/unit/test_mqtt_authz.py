"""The M0 own-namespace ACL floor (deny-by-default).

Security path: a device may touch only its own OwnTracks namespace; every other
topic — another subject's tree, a bare wildcard, a non-owntracks topic — is denied
until view-scope (M2) widens it.
"""

import pytest

from jbrain.mqtt.authz import authorize_ingest_subscribe, authorize_topic


@pytest.mark.parametrize(
    ("username", "topic", "allowed"),
    [
        # Own namespace: base, pub topic, cmd subtree, own subscribe filter.
        ("dad", "owntracks/dad", True),
        ("dad", "owntracks/dad/phone", True),
        ("dad", "owntracks/dad/phone/cmd", True),
        ("dad", "owntracks/dad/+", True),
        ("dad", "owntracks/dad/#", True),
        # Another subject, broad wildcards, foreign roots — all denied.
        ("dad", "owntracks/mom/phone", False),
        ("dad", "owntracks/+/+", False),
        ("dad", "owntracks/#", False),
        ("dad", "#", False),
        ("dad", "system/secrets", False),
        # Prefix-confusion guard: "dad" must not match "daddio".
        ("dad", "owntracks/daddio/phone", False),
        # Degenerate inputs fail closed.
        ("dad", "", False),
        ("", "owntracks/dad/phone", False),
        ("", "", False),
    ],
)
def test_authorize_topic(username: str, topic: str, allowed: bool) -> None:
    assert authorize_topic(username, topic) is allowed


@pytest.mark.parametrize(
    ("topic", "acc", "allowed"),
    [
        ("owntracks/#", 4, True),  # subscribe the whole tree
        ("owntracks/dad/phone", 1, True),  # read a device's fixes
        ("owntracks/#", 2, False),  # the ingest consumer never publishes
        ("owntracks/dad/phone", 2, False),
        ("system/#", 4, False),  # only the owntracks tree
        ("#", 4, False),
    ],
)
def test_authorize_ingest_subscribe(topic: str, acc: int, allowed: bool) -> None:
    assert authorize_ingest_subscribe(topic, acc) is allowed
