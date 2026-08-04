# es-census — native Erdős–Straus minimal-R census

A from-scratch Rust reimplementation of the Erdős–Straus certificate census
(companion to `github.com/jeffmhopkins/Erd-s-Straus-attack`), built to **rerun
everything faster and more correctly** than the Python path.

For every hard prime `n` (residues {1,121,169,289,361,529} mod 840) up to a
bound, it finds the minimal residual `R` with an explicit certificate
`4/n = 1/a + 1/b + 1/c`, and emits the same artifact set as the Python
`bulk_generate --store rseq` path so outputs are directly comparable.

## Why it exists

Two problems with the Python census this fixes:

1. **Speed.** The hot cost is factoring `a = (n+R)/4` for every prime. The Python
   path trial-divides each `a` from scratch. Here, a **windowed
   divide-as-you-sieve** factors every `a` in a segment at once, so a per-prime
   factorization is an O(1) lookup — and the whole thing is native + parallel.
2. **A correctness cliff.** The Python path factors against a fixed 180000-prime
   table; its own comment notes that only covers `n` up to ~1.3×10¹¹, but the
   published 10¹² run used it anyway. Above the cliff, `a`'s factorization can be
   incomplete, so some recorded minimal-R values are **not actually minimal**
   (the verifier re-derives with the same undersized table, so it doesn't catch
   it). This sieve uses base primes up to √a, so factorization is always
   complete — no cliff.

## Build & run

```sh
cargo build --release
# quick test run to 1e8 (writes prefix.rvals.u8.gz / .meta.json / .tail.json):
./target/release/es-census --max 100000000 --out out/es_1e8
# the real rerun to 1e12 (all cores; ~10 GB of primes+rvals held in memory):
./target/release/es-census --max 1e12 --out out/es_1e12
```

Flags: `--max N` (bound, accepts `1e12`), `--lo/--hi` (a sub-range/segment),
`--seg S` (prime-segment size, bounds per-worker memory; default 4.19M),
`--workers W` (default: all cores), `--max-r R` (default 400),
`--tail T` (store explicit triples for R≥T; default 43),
`--out PREFIX` (write artifacts; omit for a stats-only dry run).

## Correctness — how to verify

The census **exact-checks every certificate** (`4abc = n(bc+ac+ab)`, integer)
as it generates, so every emitted triple is valid by construction. Two
independent checks on top:

**(a) Byte-identical to the reference, below the cliff.** `meta.json` records
`sha256_rvals` and `sha256_primes` over the raw little-endian bytes, matching
numpy `.tobytes()`. Run the Python path at the same bound and compare — they
match exactly where the Python path is correct (≤~1.3×10¹¹):

```sh
# in the Erd-s-Straus-attack checkout:
python -m erdos_straus.bulk_generate --max 1e8 --store rseq --out /tmp/py_1e8
# here:
./target/release/es-census --max 1e8 --out /tmp/rs_1e8
# compare /tmp/py_1e8.meta.json vs /tmp/rs_1e8.meta.json: sha256_rvals & sha256_primes
```

Confirmed match at 1e8: `sha256_rvals=473f8be7…`, `sha256_primes=74fef475…`
(179,468 hard primes, max_R=107 at 8,803,369, 0 unsolved).

**(b) R-distribution cross-check** against the Python reference solver on a
shared range (needs the Erd-s-Straus-attack repo on `PYTHONPATH`):

```sh
PYTHONPATH=/path/to/Erd-s-Straus-attack/src python xcheck.py 3000000
./target/release/es-census --max 3000000     # compare R_distribution / max_R
```

Confirmed identical on [2, 3×10⁶]: 6,628 hard primes, max_R=59 at 118801,
matching R-distribution bucket-for-bucket.

**Above the cliff**, Rust and Python `sha256_rvals` will *differ* — that is the
point: the Rust values are the corrected, truly-minimal ones.

## Output format

`PREFIX.rvals.u8.gz` — gzip of uint8 minimal-R values in ascending-prime order
(0 = unsolved). `PREFIX.meta.json` — bound, counts, max_R + prime, R
distribution, `sha256_rvals`, `sha256_primes`. `PREFIX.tail.json` — explicit
`(a,b,c)` triples for R ≥ the tail threshold. Primes regenerate deterministically
from the hard-residue segmented sieve to the bound.
