"""The fail-closed memory-domain classifier (docs/ASSISTANT.md "Domain
classification", invariants #3/#4). Pure policy — every default leans
restrictive."""

from jbrain.agent.classifier import (
    behavioral_domain,
    behavioral_needs_review,
    episodic_scopes,
)


class TestEpisodicScopes:
    def test_stamps_exactly_the_domains_touched(self) -> None:
        assert episodic_scopes(["health"], ["general", "health", "finance"]) == ("health",)

    def test_a_multi_domain_turn_keeps_every_touched_scope(self) -> None:
        # The episode then needs ALL of these to read (the #4 firewall).
        assert episodic_scopes(["general", "health"], ["general", "health"]) == (
            "general",
            "health",
        )

    def test_observed_is_bounded_by_the_session_scopes(self) -> None:
        # A tool can't have read outside the session, but bound defensively.
        assert episodic_scopes(["health", "finance"], ["health"]) == ("health",)

    def test_nothing_touched_falls_back_to_the_full_session_scope(self) -> None:
        # Never a bare `general` row for a multi-scope session (#4, fail-closed).
        assert episodic_scopes([], ["general", "health"]) == ("general", "health")

    def test_result_is_sorted_and_deduplicated(self) -> None:
        assert episodic_scopes(["health", "health", "general"], ["general", "health"]) == (
            "general",
            "health",
        )


class TestBehavioralDomain:
    def test_rejects_a_write_that_is_not_owner_confirmed(self) -> None:
        # Behavioral memory has no autonomous write path (invariant #3).
        assert behavioral_domain(["general"], owner_confirmed=False) is None

    def test_generic_write_is_general(self) -> None:
        assert behavioral_domain(["general"], owner_confirmed=True) == "general"
        assert behavioral_domain([], owner_confirmed=True) == "general"

    def test_defaults_into_the_most_sensitive_domain_touched(self) -> None:
        # Asymmetric rule: into sensitive is cheap, out of it is a leak.
        assert behavioral_domain(["general", "health"], owner_confirmed=True) == "health"
        assert behavioral_domain(["general", "finance"], owner_confirmed=True) == "finance"
        assert behavioral_domain(["finance", "health"], owner_confirmed=True) == "health"

    def test_unknown_domain_is_treated_as_maximally_sensitive(self) -> None:
        assert behavioral_domain(["general", "mystery"], owner_confirmed=True) == "mystery"


class TestBehavioralNeedsReview:
    def test_two_sensitive_domains_route_to_review(self) -> None:
        assert behavioral_needs_review(["health", "finance"]) is True

    def test_single_sensitive_or_general_is_unambiguous(self) -> None:
        assert behavioral_needs_review(["health"]) is False
        assert behavioral_needs_review(["general", "health"]) is False
        assert behavioral_needs_review(["general"]) is False
        assert behavioral_needs_review([]) is False
