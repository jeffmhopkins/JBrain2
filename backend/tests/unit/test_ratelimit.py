"""Per-key token bucket for OwnTracks ingest."""

from jbrain.locations.ratelimit import TokenBucket


def test_bucket_allows_a_burst_up_to_capacity_then_denies() -> None:
    bucket = TokenBucket(capacity=3, refill_per_sec=0.0)
    # Capacity 3: three allowed at the same instant, the fourth denied.
    assert [bucket.allow("dev", now=0.0) for _ in range(4)] == [True, True, True, False]


def test_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=2, refill_per_sec=1.0)
    assert bucket.allow("dev", now=0.0) is True
    assert bucket.allow("dev", now=0.0) is True
    assert bucket.allow("dev", now=0.0) is False  # drained
    # One second later one token has refilled.
    assert bucket.allow("dev", now=1.0) is True
    assert bucket.allow("dev", now=1.0) is False


def test_buckets_are_per_key() -> None:
    bucket = TokenBucket(capacity=1, refill_per_sec=0.0)
    assert bucket.allow("a", now=0.0) is True
    assert bucket.allow("a", now=0.0) is False
    # A different device has its own bucket.
    assert bucket.allow("b", now=0.0) is True
