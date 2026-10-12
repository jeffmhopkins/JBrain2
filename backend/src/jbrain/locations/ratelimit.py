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
    # Public limiters key on client IPs, which an attacker can cycle (one IPv6 /64 is
    # endless), so the table is bounded: past this many keys, buckets that have refilled
    # to full are dropped (forgetting them changes nothing), then the stalest.
    max_keys: int = 4096
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def allow(self, key: str, *, now: float | None = None, cost: int = 1) -> bool:
        """Consume `cost` tokens (one per fix — a batch upload costs its fix count).
        Returns False without consuming when fewer than `cost` tokens remain (so an
        over-capacity request never drives the bucket negative)."""
        moment = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_keys:
                self._evict(moment)
            bucket = _Bucket(tokens=self.capacity, updated_at=moment)
            self._buckets[key] = bucket
        elapsed = max(0.0, moment - bucket.updated_at)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_sec)
        bucket.updated_at = moment
        if bucket.tokens < cost:
            return False
        bucket.tokens -= cost
        return True

    def _evict(self, moment: float) -> None:
        def full(b: _Bucket) -> bool:
            return b.tokens + (moment - b.updated_at) * self.refill_per_sec >= self.capacity

        for key in [k for k, b in self._buckets.items() if full(b)]:
            del self._buckets[key]
        overflow = len(self._buckets) - self.max_keys + 1
        if overflow > 0:
            stalest = sorted(self._buckets, key=lambda k: self._buckets[k].updated_at)
            for key in stalest[:overflow]:
                del self._buckets[key]
