"""Session capability helpers (no DB): the owner_scoped GUC, the narrowed read
context, and the action-policy lookup."""

from jbrain.agent.session import outcome_for, read_context
from jbrain.db.session import SessionContext


def test_owner_scoped_emits_guc() -> None:
    narrowed = SessionContext(principal_kind="owner", domain_scopes=("health",), owner_scoped=True)
    assert narrowed.gucs()["app.owner_scoped"] == "true"
    assert narrowed.gucs()["app.domain_scopes"] == "health"
    # Default off — backward compatible with every existing context.
    assert SessionContext(principal_kind="owner").gucs()["app.owner_scoped"] == "false"


def test_read_context_is_owner_but_narrowed() -> None:
    ctx = read_context("p1", ("health", "general"))
    assert ctx.principal_kind == "owner"  # owner-only tables stay visible
    assert ctx.owner_scoped is True  # but domain data is narrowed
    assert ctx.domain_scopes == ("health", "general")


def test_outcome_for_default_owner_policy() -> None:
    assert outcome_for("read") == "direct"
    assert outcome_for("mutate") == "staged"
    assert outcome_for("sensitive") == "staged"
    # Every off-box call stages an egress Proposal.
    assert outcome_for("external") == "staged"


def test_outcome_for_respects_a_custom_policy() -> None:
    locked = {"read": "direct", "mutate": "denied", "sensitive": "denied", "external": "denied"}
    assert outcome_for("mutate", locked) == "denied"  # type: ignore[arg-type]
