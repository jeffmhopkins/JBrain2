"""Queue logic that needs no database: backoff schedule and stale-reclaim
accounting."""

from datetime import timedelta

from jbrain.queue import (
    BACKOFF_CAP,
    INTEGRATION_BACKFILL_ORDER_BY,
    STALE_LOCK,
    SYSTEM_CTX,
    backoff,
    reclaim_attempts,
)


def test_backoff_doubles_per_attempt() -> None:
    assert backoff(1) == timedelta(minutes=2)
    assert backoff(2) == timedelta(minutes=4)
    assert backoff(3) == timedelta(minutes=8)
    assert backoff(4) == timedelta(minutes=16)


def test_backoff_is_capped() -> None:
    assert backoff(6) == BACKOFF_CAP
    assert backoff(50) == BACKOFF_CAP
    assert backoff(10_000) == BACKOFF_CAP  # exponent is clamped, no overflow


def test_backoff_handles_degenerate_attempt_counts() -> None:
    assert backoff(0) == timedelta(0)
    assert backoff(-3) == timedelta(0)


def test_reclaim_costs_an_attempt() -> None:
    assert reclaim_attempts(0, 5) == (1, False)
    assert reclaim_attempts(3, 5) == (4, False)


def test_reclaim_exhausts_at_max_attempts() -> None:
    # A job that keeps killing its worker must fail permanently, not loop.
    assert reclaim_attempts(4, 5) == (5, True)
    assert reclaim_attempts(9, 5) == (10, True)


def test_stale_lock_threshold_is_ten_minutes() -> None:
    assert timedelta(minutes=10) == STALE_LOCK


def test_system_context_is_owner_kind() -> None:
    # The jobs RLS policy admits only app.is_owner(); the worker's context
    # must satisfy it or the whole pipeline silently sees nothing.
    assert SYSTEM_CTX.principal_kind == "owner"


def test_integration_backfill_order_ranks_untrusted_origin_last() -> None:
    # N14 is LIVE since Phase 7 (guided intake): the leading rank term is the
    # untrusted-origin predicate, so owner/agent notes (false ⇒ sort 0) drain ahead
    # of stranger-authored intake notes (true ⇒ sort 1), with created_at the tiebreak.
    rank, tiebreak = (part.strip() for part in INTEGRATION_BACKFILL_ORDER_BY.split(","))
    assert rank == "(n.provenance = 'untrusted_origin')"
    assert tiebreak == "n.created_at"  # oldest-first within each trust tier
