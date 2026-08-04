//! Native Erdos-Straus minimal-R census.
//!
//! Sieves the hard-residue primes in `[lo, hi]`, and for each finds the minimal
//! residual R with an explicit certificate, factoring `a = (n+R)/4` via a
//! windowed smallest-prime-factor sieve (see `sieve.rs`). Segments run in
//! parallel; memory is bounded by the per-segment window.
//!
//! With `--out PREFIX` it writes the same artifact set as the Python
//! `bulk_generate --store rseq` path, so outputs are directly comparable and
//! the existing verifier applies:
//!   PREFIX.rvals.u8.gz  gzip of the uint8 minimal-R values, ascending-prime order
//!   PREFIX.meta.json    limit, counts, max R, R distribution, sha256(rvals|primes)
//!   PREFIX.tail.json    explicit (a,b,c) triples for R >= tail threshold
//! `sha256_rvals` / `sha256_primes` are over the raw little-endian bytes, matching
//! numpy `.tobytes()`, so a matching sha proves byte-identical output.
//!
//! Usage:
//!   es-census --max 100000000 [--lo L --hi H] [--seg S] [--workers W]
//!             [--max-r R] [--tail T] [--out PREFIX]

mod sieve;
mod solver;

use std::fs::File;
use std::io::Write;
use std::time::Instant;

use flate2::write::GzEncoder;
use flate2::Compression;
use rayon::prelude::*;
use sha2::{Digest, Sha256};

const TAIL_DEFAULT: u64 = 43; // store explicit triples for R >= this (as Python)

/// Per-segment output: the ascending primes and their minimal-R values (0 =
/// unsolved), plus tail triples and summary counters.
struct SegOut {
    seg_lo: u64,
    primes: Vec<u64>,
    rvals: Vec<u8>, // clamped to 255, 0 = unsolved
    tail: Vec<(u64, u64, String, String, String)>, // n, R, a, b, c
    unsolved: Vec<u64>,
    hist: Vec<u64>, // true-R histogram (index = R)
    max_r: u64,
    max_r_prime: u64,
}

fn process_segment(
    seg_lo: u64,
    seg_hi: u64,
    base: &[u64],
    max_r: u64,
    tail_thr: u64,
) -> SegOut {
    let hard = sieve::hard_primes_in(seg_lo, seg_hi, base);
    let a_lo = (seg_lo + 3) / 4;
    let a_hi = (seg_hi + max_r) / 4 + 1;
    let wf = sieve::factor_window(a_lo, a_hi, base);

    let mut out = SegOut {
        seg_lo,
        primes: Vec::with_capacity(hard.len()),
        rvals: Vec::with_capacity(hard.len()),
        tail: Vec::new(),
        unsolved: Vec::new(),
        hist: vec![0; max_r as usize + 1],
        max_r: 0,
        max_r_prime: 0,
    };
    for &n in &hard {
        out.primes.push(n);
        match solver::minimal_certificate(n, &wf, max_r) {
            Some(cert) => {
                out.rvals.push(if cert.r > 255 { 255 } else { cert.r as u8 });
                out.hist[cert.r as usize] += 1;
                if cert.r > out.max_r || (cert.r == out.max_r && n < out.max_r_prime) {
                    out.max_r = cert.r;
                    out.max_r_prime = n;
                }
                if cert.r >= tail_thr {
                    out.tail.push((
                        n,
                        cert.r,
                        cert.a.to_string(),
                        cert.b.to_string(),
                        cert.c.to_string(),
                    ));
                }
            }
            None => {
                out.rvals.push(0);
                out.unsolved.push(n);
            }
        }
    }
    out
}

fn parse_u64(s: &str) -> u64 {
    if let Some(pos) = s.find(['e', 'E']) {
        let base: f64 = s[..pos].parse().expect("bad number");
        let exp: i32 = s[pos + 1..].parse().expect("bad exponent");
        (base * 10f64.powi(exp)).round() as u64
    } else {
        s.replace('_', "").parse().expect("bad number")
    }
}

fn write_artifacts(
    prefix: &str,
    limit: u64,
    primes: &[u64],
    rvals: &[u8],
    tail: &[(u64, u64, String, String, String)],
    max_r: u64,
    max_r_prime: u64,
    unsolved: &[u64],
    dist: &[(u64, u64)],
    sieve_secs: f64,
    total_secs: f64,
) -> std::io::Result<(String, String)> {
    // sha256 of raw rvals bytes (uint8) == numpy rvals.tobytes()
    let mut hr = Sha256::new();
    hr.update(rvals);
    let sha_rvals = hex(&hr.finalize());
    // sha256 of primes as little-endian int64 == numpy int64 primes.tobytes()
    let mut hp = Sha256::new();
    for &p in primes {
        hp.update((p as i64).to_le_bytes());
    }
    let sha_primes = hex(&hp.finalize());

    // PREFIX.rvals.u8.gz
    let f = File::create(format!("{prefix}.rvals.u8.gz"))?;
    let mut gz = GzEncoder::new(f, Compression::new(9));
    gz.write_all(rvals)?;
    gz.finish()?;

    // PREFIX.tail.json
    let mut tf = File::create(format!("{prefix}.tail.json"))?;
    write!(tf, "[")?;
    for (i, (p, r, a, b, c)) in tail.iter().enumerate() {
        if i > 0 {
            write!(tf, ",")?;
        }
        write!(
            tf,
            "\n {{\"p\": {p}, \"R\": {r}, \"a\": {a}, \"b\": {b}, \"c\": {c}}}"
        )?;
    }
    writeln!(tf, "\n]")?;

    // PREFIX.meta.json
    let dist_str: Vec<String> =
        dist.iter().map(|(r, c)| format!("\"{r}\": {c}")).collect();
    let usample: Vec<String> =
        unsolved.iter().take(50).map(|u| u.to_string()).collect();
    let mut mf = File::create(format!("{prefix}.meta.json"))?;
    write!(
        mf,
        concat!(
            "{{\n",
            "  \"limit\": {limit},\n",
            "  \"num_hard_primes\": {nhp},\n",
            "  \"num_unsolved\": {nun},\n",
            "  \"unsolved_sample\": [{usample}],\n",
            "  \"max_R\": {maxr},\n",
            "  \"max_R_prime\": {maxrp},\n",
            "  \"R_distribution\": {{{dist}}},\n",
            "  \"first_prime\": {first},\n",
            "  \"last_prime\": {last},\n",
            "  \"sha256_rvals\": \"{shr}\",\n",
            "  \"sha256_primes\": \"{shp}\",\n",
            "  \"sieve_secs\": {ss:.3},\n",
            "  \"elapsed_secs\": {es:.3},\n",
            "  \"format\": \"uint8 minimal-R values in ascending-prime order; ",
            "primes regenerate via the hard-residue segmented sieve to `limit`\"\n",
            "}}\n"
        ),
        limit = limit,
        nhp = primes.len(),
        nun = unsolved.len(),
        usample = usample.join(", "),
        maxr = max_r,
        maxrp = max_r_prime,
        dist = dist_str.join(", "),
        first = primes.first().copied().unwrap_or(0),
        last = primes.last().copied().unwrap_or(0),
        shr = sha_rvals,
        shp = sha_primes,
        ss = sieve_secs,
        es = total_secs,
    )?;
    Ok((sha_rvals, sha_primes))
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut lo: u64 = 2;
    let mut hi: u64 = 100_000_000;
    let mut seg: u64 = 1 << 22;
    let mut workers: usize = 0;
    let mut max_r: u64 = 400;
    let mut tail_thr: u64 = TAIL_DEFAULT;
    let mut out: Option<String> = None;
    let mut i = 1;
    while i < args.len() {
        let val = |i: usize| args.get(i + 1).expect("missing value").clone();
        match args[i].as_str() {
            "--max" => {
                hi = parse_u64(&val(i));
                lo = 2;
                i += 2;
            }
            "--lo" => {
                lo = parse_u64(&val(i));
                i += 2;
            }
            "--hi" => {
                hi = parse_u64(&val(i));
                i += 2;
            }
            "--seg" => {
                seg = parse_u64(&val(i));
                i += 2;
            }
            "--workers" => {
                workers = parse_u64(&val(i)) as usize;
                i += 2;
            }
            "--max-r" => {
                max_r = parse_u64(&val(i));
                i += 2;
            }
            "--tail" => {
                tail_thr = parse_u64(&val(i));
                i += 2;
            }
            "--out" => {
                out = Some(val(i));
                i += 2;
            }
            other => panic!("unknown arg: {other}"),
        }
    }

    if workers > 0 {
        rayon::ThreadPoolBuilder::new()
            .num_threads(workers)
            .build_global()
            .unwrap();
    }

    let t0 = Instant::now();
    let base = sieve::base_primes((hi as f64).sqrt() as u64 + 2);
    let sieve_secs = t0.elapsed().as_secs_f64();

    let mut segments = Vec::new();
    let mut s = lo;
    while s <= hi {
        let e = (s + seg - 1).min(hi);
        segments.push((s, e));
        s = e + 1;
    }

    let t1 = Instant::now();
    let mut parts: Vec<SegOut> = segments
        .par_iter()
        .map(|&(a, b)| process_segment(a, b, &base, max_r, tail_thr))
        .collect();
    let solve_secs = t1.elapsed().as_secs_f64();
    parts.sort_by_key(|p| p.seg_lo);

    // Assemble ordered arrays + merged summary.
    let mut primes: Vec<u64> = Vec::new();
    let mut rvals: Vec<u8> = Vec::new();
    let mut tail: Vec<(u64, u64, String, String, String)> = Vec::new();
    let mut unsolved: Vec<u64> = Vec::new();
    let mut hist = vec![0u64; max_r as usize + 1];
    let mut max_r_seen = 0u64;
    let mut max_r_prime = 0u64;
    for p in parts {
        primes.extend_from_slice(&p.primes);
        rvals.extend_from_slice(&p.rvals);
        tail.extend(p.tail);
        unsolved.extend(p.unsolved);
        for (idx, c) in p.hist.iter().enumerate() {
            hist[idx] += c;
        }
        if p.max_r > max_r_seen
            || (p.max_r == max_r_seen && p.max_r_prime != 0
                && (max_r_prime == 0 || p.max_r_prime < max_r_prime))
        {
            max_r_seen = p.max_r;
            max_r_prime = p.max_r_prime;
        }
    }

    let mut dist: Vec<(u64, u64)> = hist
        .iter()
        .enumerate()
        .filter(|(_, &c)| c > 0)
        .map(|(r, &c)| (r as u64, c))
        .collect();
    dist.sort_by_key(|&(r, _)| r);
    let total_secs = t0.elapsed().as_secs_f64();

    if let Some(prefix) = &out {
        match write_artifacts(
            prefix, hi, &primes, &rvals, &tail, max_r_seen, max_r_prime,
            &unsolved, &dist, sieve_secs, total_secs,
        ) {
            Ok((shr, shp)) => eprintln!(
                "[out] wrote {prefix}.{{rvals.u8.gz,meta.json,tail.json}}  \
                 sha256_rvals={shr}  sha256_primes={shp}"
            ),
            Err(e) => eprintln!("[out] failed to write artifacts: {e}"),
        }
    }

    let dist_str: Vec<String> =
        dist.iter().map(|(r, c)| format!("\"{r}\":{c}")).collect();
    let rate = primes.len() as f64 / solve_secs.max(1e-9);
    println!("{{");
    println!("  \"lo\": {lo},");
    println!("  \"hi\": {hi},");
    println!("  \"num_hard_primes\": {},", primes.len());
    println!("  \"num_unsolved\": {},", unsolved.len());
    println!("  \"max_R\": {max_r_seen},");
    println!("  \"max_R_prime\": {max_r_prime},");
    println!("  \"R_distribution\": {{{}}},", dist_str.join(","));
    println!("  \"tail_count(R>={tail_thr})\": {},", tail.len());
    println!("  \"sieve_secs\": {sieve_secs:.3},");
    println!("  \"solve_secs\": {solve_secs:.3},");
    println!("  \"total_secs\": {total_secs:.3},");
    println!("  \"hard_primes_per_sec\": {rate:.0}");
    println!("}}");
    if !unsolved.is_empty() {
        let sample: Vec<u64> = unsolved.iter().take(10).copied().collect();
        eprintln!("[warn] {} unsolved (max_R={max_r} too small?): {sample:?}",
            unsolved.len());
    }
}
