"""The E1 scope-carrier helper: `narrowed_context` fail-closed semantics.

The no-confused-deputy property (docs/archive/WORKFLOW_ENGINE_PLAN.md §2 E1, ASSISTANT.md
I-8) hinges on this helper never silently widening a partial stamp to the
all-domains system scope. These are pure unit tests; the RLS proof that the
narrowed context can't cross a firewall is the integration suite."""

import pytest

from jbrain.db.session import ScopeStampError, narrowed_context


def test_complete_stamp_yields_owner_narrowed_single_domain() -> None:
    ctx = narrowed_context("principal-1", "health")
    assert ctx.principal_id == "principal-1"
    assert ctx.principal_kind == "owner"
    assert ctx.owner_scoped is True
    assert tuple(ctx.domain_scopes) == ("health",)
    # The GUCs the firewall reads: owner_scoped on, exactly one domain in scope.
    gucs = ctx.gucs()
    assert gucs["app.owner_scoped"] == "true"
    assert gucs["app.domain_scopes"] == "health"


@pytest.mark.parametrize(
    ("principal_id", "domain_code"),
    [
        ("principal-1", None),  # principal without a domain
        (None, "health"),  # domain without a principal
        ("principal-1", ""),  # empty-string domain is not a domain
        ("", "health"),  # empty-string principal is not a principal
        (None, None),  # both absent: a system decision the worker owns, not us
        ("", ""),
    ],
)
def test_partial_or_empty_stamp_fails_closed(
    principal_id: str | None, domain_code: str | None
) -> None:
    """A half/empty stamp raises rather than downgrading to a system context: a
    confused deputy can never widen its scope by dropping one half of the stamp."""
    with pytest.raises(ScopeStampError):
        narrowed_context(principal_id, domain_code)
