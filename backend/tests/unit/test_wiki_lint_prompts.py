"""Digest pins for the wiki_lint (Wave B) verifier prompts.

The contradiction/stale system prompts are versioned module constants (no rendered `.prompt`
file). This is the same drift guard the `.prompt` files get from their sha256 pins: editing the
prose or the JSON schema without bumping the `*_PROMPT_VERSION` fails this test — so a prompt change
is always a deliberate, versioned migration, never silent drift.
"""

import hashlib
import json

from jbrain.wiki.lint import (
    _CONTRADICTION_SCHEMA,
    _STALE_SCHEMA,
    CONTRADICTION_PROMPT_VERSION,
    CONTRADICTION_SYSTEM,
    STALE_PROMPT_VERSION,
    STALE_SYSTEM,
    card_domain,
)


def test_card_domain_routes_every_firewall_case() -> None:
    # The security-critical stamp router: equal → shared; one general → the restricted side (BOTH
    # orderings); two distinct restricted → None (suppress, never a review card). Keying a
    # cross-firewall finding via ratchet_domain instead would be order-dependent and leak.
    assert card_domain("general", "general") == "general"
    assert card_domain("health", "health") == "health"
    assert card_domain("general", "health") == "health"
    assert card_domain("health", "general") == "health"  # the reverse ordering must match
    assert card_domain("health", "finance") is None
    assert card_domain("finance", "health") is None


def _digest(system: str, schema: dict) -> str:
    blob = system + "\x00" + json.dumps(schema, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def test_contradiction_prompt_is_pinned_to_its_version() -> None:
    assert (CONTRADICTION_PROMPT_VERSION, _digest(CONTRADICTION_SYSTEM, _CONTRADICTION_SCHEMA)) == (
        "wiki-lint-contradiction-v1",
        "60830634c55473d990943a663e0a9556ed60a8b268a517d906c1571578fdbc22",
    )


def test_stale_prompt_is_pinned_to_its_version() -> None:
    assert (STALE_PROMPT_VERSION, _digest(STALE_SYSTEM, _STALE_SCHEMA)) == (
        "wiki-lint-stale-v1",
        "ca829b7bc0354e3abc92dc1c5b03eefde5121ba0d3a0fe32b68e338799f99fba",
    )
