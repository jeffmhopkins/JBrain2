# Correctness bug in the Python census above ~1.3×10¹¹ (non-minimal R)

A note for the Erdős–Straus paper/repo work. **The existence result is fine — every
hard prime still has a valid certificate. What is wrong is the *minimality* of the
recorded R for a fraction of primes above ~1.3×10¹¹.** This was found while building a
native reimplementation (`research/es-census`) and is reproducible.

## The bug

`bulk_generate.factorize(a)` factors `a = (n+R)/4` by trial division against a fixed
prime table `_SMALL_PRIMES` whose default bound is **180000** (`bulk_generate.py:53`,
`_init_small_primes(bound=180000)`). Completeness of trial division requires primes up
to **√a**. The code's own comment says the 180000 table only covers `n` up to
**~1.29×10¹¹** (`bulk_generate.py:46-49`).

But `run_1e12.sh` calls `generate_rseq` / `_worker_rseq`, both of which call
`_init_small_primes()` at the **default 180000** — no override anywhere. The run went to
**10¹²**, where `a ≈ n/4 ≈ 2.5×10¹¹` and `√a ≈ 5×10⁵ ≫ 180000`.

Consequence: for a hard prime `n` above the cliff whose `a` has a prime factor in
`(180000, √a]`, trial division cannot fully factor `a` — the leftover cofactor (a
product of two primes both > 180000, or one such prime) is recorded as if prime. The
divisor set of `m² = n²a²` is then **incomplete**: it misses the divisors that split
that cofactor. `solve_residual` scans that reduced divisor set for the smallest
`k ≡ -m (mod R)`, so it can **miss the true small-R solution** and only find one at a
larger R.

**Direction of the error:** incomplete factorization can only ever *overstate* the
minimal R (it never invents a smaller one — every certificate it returns is exact-checked
`4abc = n(bc+ac+ab)` and is genuinely valid). So:

- Recorded minimal-R values above the cliff are **≥** the true minimal R for the affected
  primes.
- The **R-distribution is inflated** toward higher R above ~1.3×10¹¹.
- **R≥87 tail entries above the cliff may be artifacts** (a prime whose true minimal R is
  small but got pushed into the tail). The deep tail is exactly the scientifically
  interesting set, so this matters.

**Why verification did not catch it:** `verify.py::verify_npz` re-derives with
`solve_residual` under the **same** default 180000 table, and its minimality check
(`for smaller in range(3, R, 4): if solve_residual(p, smaller) …`) uses the same
incomplete factorization — so it agrees with the wrong value. `VERIFICATION OK` is **not**
evidence of minimality above the cliff. (It *is* still evidence that every stored triple
is a valid solution — existence holds.)

## Evidence (reproducible)

Using a complete factorization (base primes to √a) over one **2×10⁶-wide window at 10¹²**
(`[10¹², 10¹²+2×10⁶]`, 2,282 hard primes):

- **31 primes (~1.4%) have a non-minimal recorded R** vs the corrected value.
- Every corrected certificate is exact-checked and valid.

Concrete examples (default-table R → true minimal R):

| n | Python (180000 table) R | true minimal R |
|---|---|---|
| 1000000268569 | 23 | **3** |
| 1000000270081 | 7 | **3** |
| 1000000334881 | 11 | **3** |
| 1000000429801 | 11 | **7** |

These are large errors, not off-by-one — e.g. R=23 recorded where R=3 solves.

## What is *not* affected

- **Existence / the conjecture check**: unaffected. Every hard prime has a valid
  certificate at its recorded R.
- **Everything below ~1.3×10¹¹**: correct (√a ≤ 180000, factorization complete). The
  published record **max minimal R = 111 at p = 119,945,383,009 (≈1.199×10¹¹) is below the
  cliff, so it is real.** The older R=107 record at p=8,803,369 is likewise fine.
- Only `n ∈ (~1.3×10¹¹, 10¹²]` minimal-R values (and the R-distribution / R≥87 tail over
  that sub-range) are suspect.

## The fix

Factor `a` with base primes up to **√a** so factorization is always complete. Two ways:

1. **Minimal patch (keeps the Python path):** size the table to the range —
   `_init_small_primes(isqrt(a_max) + 1)` (≈ `isqrt(limit // 4)`), or factor the residual
   cofactor with Pollard-rho when it exceeds the table. This restores correctness (still
   super-linear in cost).
2. **The native tool (`research/es-census`):** a windowed *divide-as-you-sieve* factors
   every `a` in a segment with base primes to √a — complete by construction, linear, and
   parallel. This is what the `erdos_straus_1e12_native` jlaunch spec runs for the rerun.

## How to reproduce / verify

- **Show the cliff** (needs the `Erd-s-Straus-attack` checkout on `PYTHONPATH`): the
  `spf_prototype.py coverage` demo compares the default-table solver vs a complete sieve
  over a window > 1.3×10¹¹ and lists the disagreements.
- **Confirm the native tool is correct below the cliff:** `es-census` and the Python
  `bulk_generate --store rseq` produce **byte-identical** `sha256_rvals` / `sha256_primes`
  at ≤10¹¹ (checked at 1e8: `sha256_rvals=473f8be7…`, `sha256_primes=74fef475…`). Above
  the cliff the hashes **diverge** — that divergence is the set of corrections.
</content>
