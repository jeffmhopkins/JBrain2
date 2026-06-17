"""A small per-key token bucket for bounding OwnTracks ingest rate.

In-memory and per-process — adequate at personal scale (one API process). A
flooding or misconfigured device gets a 429 and backs off, while a normal
move-mode device (a fix every few seconds) never trips it.
"""

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class TokenBucket:
    """`capacity` tokens, refilling at `refill_per_sec`. One bucket per key."""

    capacity: float
    refill_per_sec: float
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(tokens=self.capacity - 1, updated_at=moment)
            return True
        elapsed = max(0.0, moment - bucket.updated_at)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_sec)
        bucket.updated_at = moment
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True
