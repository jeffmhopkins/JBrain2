"""Queue logic that needs no database: the retry backoff schedule."""

from datetime import timedelta

from jbrain.queue import BACKOFF_CAP, SYSTEM_CTX, backoff


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


def test_system_context_is_owner_kind() -> None:
    # The jobs RLS policy admits only app.is_owner(); the worker's context
    # must satisfy it or the whole pipeline silently sees nothing.
    assert SYSTEM_CTX.principal_kind == "owner"
