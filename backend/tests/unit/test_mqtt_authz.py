"""The M0 own-namespace ACL floor (deny-by-default).

Security path: a device may touch only its own OwnTracks namespace; every other
topic — another subject's tree, a bare wildcard, a non-owntracks topic — is denied
until view-scope (M2) widens it.
"""

import pytest

from jbrain.mqtt.authz import authorize_ingest_subscribe, authorize_topic, topic_namespace_owner


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


@pytest.mark.parametrize(
    ("topic", "owner"),
    [
        ("owntracks/dad/phone", "dad"),
        ("owntracks/dad/+", "dad"),  # a subscribe filter keeps a concrete owner
        ("owntracks/dad", "dad"),
        ("owntracks/+/+", None),  # wildcard owner is not a single namespace
        ("owntracks/#", None),
        ("owntracks//phone", None),
        ("system/dad/phone", None),
        ("dad", None),
    ],
)
def test_topic_namespace_owner(topic: str, owner: str | None) -> None:
    assert topic_namespace_owner(topic) == owner
