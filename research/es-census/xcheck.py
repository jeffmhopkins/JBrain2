"""Cross-check: Python reference R-distribution over [2, hi] vs the Rust census.

Run: PYTHONPATH=../src python xcheck.py <hi>
"""
import math
import sys
from collections import Counter

from erdos_straus import bulk_generate as bg

hi = int(float(sys.argv[1])) if len(sys.argv) > 1 else 3_000_000
# Correctly sized prime table (primes to sqrt(a_max)) so the reference is exact.
bg._SMALL_PRIMES = []
bg._init_small_primes(int(math.isqrt(hi // 4 + 400)) + 1)

# hard primes in [2, hi]
sieve = bytearray([1]) * (hi + 1)
sieve[0] = sieve[1] = 0
for i in range(2, int(hi ** 0.5) + 1):
    if sieve[i]:
        sieve[i * i :: i] = bytearray(len(sieve[i * i :: i]))
HR = {1, 121, 169, 289, 361, 529}
hard = [n for n in range(2, hi + 1) if sieve[n] and n % 840 in HR]

hist = Counter()
max_r, max_r_p, unsolved = 0, 0, 0
for n in hard:
    res = bg.minimal_certificate(n)
    if res is None:
        unsolved += 1
        continue
    R = res[3]
    hist[R] += 1
    if R > max_r or (R == max_r and (max_r_p == 0 or n < max_r_p)):
        max_r, max_r_p = R, n

print(f"num_hard_primes={len(hard)} num_unsolved={unsolved} "
      f"max_R={max_r} max_R_prime={max_r_p}")
print("R_distribution=" + ",".join(f"{r}:{hist[r]}" for r in sorted(hist)))
