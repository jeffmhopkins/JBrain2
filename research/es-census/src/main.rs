//! Native Erdos-Straus minimal-R census.
//!
//! Sieves the hard-residue primes in `[lo, hi]`, and for each finds the minimal
//! residual R with an explicit certificate, factoring `a = (n+R)/4` via a
//! windowed smallest-prime-factor sieve (see `sieve.rs`). Segments run in
//! parallel; memory is bounded by the per-segment window AND by streaming the
//! output in ascending order — primes/rvals are never accumulated whole (a 1e13
//! run would otherwise hold ~86 GB of primes in RAM), so the census scales to
//! arbitrarily large ranges on a fixed memory budget.
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
//!             [--max-r R] [--tail T] [--out PREFIX] [--verify-log PATH]

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

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// Running accumulators over the streamed segments — everything here is bounded
/// (histogram, hashers, the R>=tail tail, counters), never the full prime list.
struct Sink {
    gz: Option<GzEncoder<File>>,
    hr: Sha256,
    hp: Sha256,
    hist: Vec<u64>,
    tail: Vec<(u64, u64, String, String, String)>,
    unsolved: Vec<u64>,
    max_r: u64,
    max_r_prime: u64,
    total_hard: u64,
    first_prime: u64,
    last_prime: u64,
}

impl Sink {
    fn absorb(&mut self, out: SegOut) {
        if let Some(g) = self.gz.as_mut() {
            g.write_all(&out.rvals).expect("write rvals");
        }
        self.hr.update(&out.rvals);
        for &p in &out.primes {
            self.hp.update((p as i64).to_le_bytes());
        }
        if self.first_prime == 0 {
            if let Some(&f) = out.primes.first() {
                self.first_prime = f;
            }
        }
        if let Some(&l) = out.primes.last() {
            self.last_prime = l;
        }
        self.total_hard += out.primes.len() as u64;
        for (i, c) in out.hist.iter().enumerate() {
            self.hist[i] += c;
        }
        if out.max_r > self.max_r
            || (out.max_r == self.max_r
                && out.max_r_prime != 0
                && (self.max_r_prime == 0 || out.max_r_prime < self.max_r_prime))
        {
            self.max_r = out.max_r;
            self.max_r_prime = out.max_r_prime;
        }
        self.tail.extend(out.tail);
        self.unsolved.extend(out.unsolved);
    }
}

#[allow(clippy::too_many_arguments)]
fn write_meta_tail(
    prefix: &str,
    limit: u64,
    sink: &Sink,
    dist: &[(u64, u64)],
    sha_rvals: &str,
    sha_primes: &str,
    sieve_secs: f64,
    total_secs: f64,
) -> std::io::Result<()> {
    // PREFIX.tail.json
    let mut tf = File::create(format!("{prefix}.tail.json"))?;
    write!(tf, "[")?;
    for (i, (p, r, a, b, c)) in sink.tail.iter().enumerate() {
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
        sink.unsolved.iter().take(50).map(|u| u.to_string()).collect();
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
        nhp = sink.total_hard,
        nun = sink.unsolved.len(),
        usample = usample.join(", "),
        maxr = sink.max_r,
        maxrp = sink.max_r_prime,
        dist = dist_str.join(", "),
        first = sink.first_prime,
        last = sink.last_prime,
        shr = sha_rvals,
        shp = sha_primes,
        ss = sieve_secs,
        es = total_secs,
    )?;
    Ok(())
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
    let mut verify_log: Option<String> = None;
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
            "--verify-log" => {
                verify_log = Some(val(i));
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
    let total_segs = segments.len();
    // Parallel batch: bounds how many segment outputs are buffered before being
    // streamed out in order. Keeps peak memory ~O(batch) regardless of range.
    let batch = (rayon::current_num_threads() * 4).max(4);
    let step = (total_segs / 100).max(1);

    let mut sink = Sink {
        gz: out.as_ref().map(|p| {
            GzEncoder::new(
                File::create(format!("{p}.rvals.u8.gz")).expect("create rvals.u8.gz"),
                Compression::new(9),
            )
        }),
        hr: Sha256::new(),
        hp: Sha256::new(),
        hist: vec![0u64; max_r as usize + 1],
        tail: Vec::new(),
        unsolved: Vec::new(),
        max_r: 0,
        max_r_prime: 0,
        total_hard: 0,
        first_prime: 0,
        last_prime: 0,
    };

    let t1 = Instant::now();
    let mut done = 0usize;
    for chunk in segments.chunks(batch) {
        // Process a batch in parallel (order preserved by collect), then drain in
        // ascending segment order so the streamed rvals stay in prime order.
        let outs: Vec<SegOut> = chunk
            .par_iter()
            .map(|&(a, b)| process_segment(a, b, &base, max_r, tail_thr))
            .collect();
        for o in outs {
            sink.absorb(o);
            done += 1;
            if done % step == 0 || done == total_segs {
                eprintln!(
                    "[census] segment {done}/{total_segs} ({}%), {} hard primes, {:.0}s",
                    done * 100 / total_segs,
                    sink.total_hard,
                    t1.elapsed().as_secs_f64()
                );
            }
        }
    }
    if let Some(g) = sink.gz.take() {
        g.finish().expect("finish gz");
    }
    let solve_secs = t1.elapsed().as_secs_f64();
    let total_secs = t0.elapsed().as_secs_f64();
    let sha_rvals = hex(&sink.hr.clone().finalize());
    let sha_primes = hex(&sink.hp.clone().finalize());

    let mut dist: Vec<(u64, u64)> = sink
        .hist
        .iter()
        .enumerate()
        .filter(|(_, &c)| c > 0)
        .map(|(r, &c)| (r as u64, c))
        .collect();
    dist.sort_by_key(|&(r, _)| r);

    // Headline block for the public results page — verbatim lines matching the
    // Python run output the share page renders.
    let verdict = if sink.max_r > 107 {
        "(RECORD BREAKS 107!)"
    } else {
        "(record R=107 stands)"
    };
    let tail87: Vec<String> = dist
        .iter()
        .filter(|(r, _)| *r >= 87)
        .map(|(r, c)| format!("{r}: {c}"))
        .collect();
    let headline = [
        format!("hard primes: {}", sink.total_hard),
        format!(
            "max minimal R: {} at p = {} {verdict}",
            sink.max_r, sink.max_r_prime
        ),
        format!("R >= 87 counts: {{{}}}", tail87.join(", ")),
    ];

    if let Some(prefix) = &out {
        if let Err(e) = write_meta_tail(
            prefix, hi, &sink, &dist, &sha_rvals, &sha_primes, sieve_secs, total_secs,
        ) {
            eprintln!("[out] failed to write meta/tail: {e}");
        } else {
            eprintln!(
                "[out] wrote {prefix}.{{rvals.u8.gz,meta.json,tail.json}}  \
                 sha256_rvals={sha_rvals}  sha256_primes={sha_primes}"
            );
        }
    }

    let dist_str: Vec<String> =
        dist.iter().map(|(r, c)| format!("\"{r}\":{c}")).collect();
    let rate = sink.total_hard as f64 / solve_secs.max(1e-9);
    println!("{{");
    println!("  \"lo\": {lo},");
    println!("  \"hi\": {hi},");
    println!("  \"num_hard_primes\": {},", sink.total_hard);
    println!("  \"num_unsolved\": {},", sink.unsolved.len());
    println!("  \"max_R\": {},", sink.max_r);
    println!("  \"max_R_prime\": {},", sink.max_r_prime);
    println!("  \"R_distribution\": {{{}}},", dist_str.join(","));
    println!("  \"tail_count(R>={tail_thr})\": {},", sink.tail.len());
    println!("  \"sieve_secs\": {sieve_secs:.3},");
    println!("  \"solve_secs\": {solve_secs:.3},");
    println!("  \"total_secs\": {total_secs:.3},");
    println!("  \"hard_primes_per_sec\": {rate:.0}");
    println!("}}");

    // Headline lines to stdout (captured into the run's console log; the launcher
    // greps these markers for the public results page).
    for line in &headline {
        println!("{line}");
    }

    // Optional verify log for the launcher's success gate: exact-checked at
    // generation, so a clean run (no unsolved primes) writes the sentinel.
    if let Some(vpath) = &verify_log {
        let ok = sink.unsolved.is_empty();
        let mut report = String::from("native es-census verification\n");
        for line in &headline {
            report.push_str(line);
            report.push('\n');
        }
        report.push_str(&format!("unsolved: {}\n", sink.unsolved.len()));
        report.push_str(
            "every certificate exact-checked (4abc = n(bc+ac+ab)) during generation\n",
        );
        report.push_str(if ok {
            "VERIFICATION OK\n"
        } else {
            "VERIFICATION FAILED\n"
        });
        if let Err(e) = std::fs::write(vpath, &report) {
            eprintln!("[verify] write failed: {e}");
        }
        if !ok {
            std::process::exit(1);
        }
    }

    if !sink.unsolved.is_empty() {
        let sample: Vec<u64> = sink.unsolved.iter().take(10).copied().collect();
        eprintln!(
            "[warn] {} unsolved (max_R={max_r} too small?): {sample:?}",
            sink.unsolved.len()
        );
    }
}
