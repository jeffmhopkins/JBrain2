"""The M0 own-namespace ACL floor (deny-by-default).

Security path: a device may touch only its own OwnTracks namespace; every other
topic — another subject's tree, a bare wildcard, a non-owntracks topic — is denied
until view-scope (M2) widens it.
"""

import pytest

from jbrain.mqtt.authz import authorize_topic


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
