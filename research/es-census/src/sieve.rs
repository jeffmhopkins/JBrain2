//! Prime sieving and the windowed smallest-prime-factor factorization.
//!
//! The census factors `a = (n+R)/4` for every hard prime `n`. Instead of
//! trial-dividing each `a` from scratch (the Python path, which also silently
//! loses completeness once sqrt(a) outruns its fixed prime table), we factor
//! every `a` in a whole segment window in one divide-as-you-sieve pass, so a
//! per-prime factorization is an O(1) lookup. Base primes up to sqrt(a_hi)
//! guarantee a complete factorization: any composite's least factor is
//! <= its square root <= sqrt(a_hi), so what remains is 1 or one large prime.

/// The six hard residue classes mod 840 (all `≡ 1 (mod 4)`).
pub const HARD_RESIDUES: [u64; 6] = [1, 121, 169, 289, 361, 529];

#[inline]
fn is_hard(n: u64) -> bool {
    matches!(n % 840, 1 | 121 | 169 | 289 | 361 | 529)
}

/// All primes `<= bound` via a simple boolean sieve.
pub fn base_primes(bound: u64) -> Vec<u64> {
    if bound < 2 {
        return Vec::new();
    }
    let n = bound as usize;
    let mut is_p = vec![true; n + 1];
    is_p[0] = false;
    is_p[1] = false;
    let mut i = 2usize;
    while i * i <= n {
        if is_p[i] {
            let mut j = i * i;
            while j <= n {
                is_p[j] = false;
                j += i;
            }
        }
        i += 1;
    }
    (2..=n).filter(|&k| is_p[k]).map(|k| k as u64).collect()
}

/// Hard-residue primes in `[lo, hi]` (inclusive). `base` must hold all primes
/// up to `sqrt(hi)`.
pub fn hard_primes_in(lo: u64, hi: u64, base: &[u64]) -> Vec<u64> {
    let size = (hi - lo + 1) as usize;
    let mut seg = vec![true; size];
    for &p in base {
        if p * p > hi {
            break;
        }
        let mut start = ((lo + p - 1) / p) * p;
        if start < p * p {
            start = p * p;
        }
        let mut j = start;
        while j <= hi {
            seg[(j - lo) as usize] = false;
            j += p;
        }
    }
    let mut out = Vec::new();
    for (i, &alive) in seg.iter().enumerate() {
        if alive {
            let n = lo + i as u64;
            if n >= 2 && is_hard(n) {
                out.push(n);
            }
        }
    }
    out
}

/// Complete factorizations of every integer in `[a_lo, a_hi]`.
pub struct WindowFactors {
    a_lo: u64,
    /// Prime factors `<= sqrt(a_hi)`, as (prime, exponent), per slot.
    small: Vec<Vec<(u64, u32)>>,
    /// 1, or the single remaining prime `> sqrt(a_hi)`, per slot.
    rem: Vec<u64>,
}

impl WindowFactors {
    /// The full factorization of `a` (must lie in the window).
    #[inline]
    pub fn factor(&self, a: u64) -> Vec<(u64, u32)> {
        let i = (a - self.a_lo) as usize;
        let mut f = self.small[i].clone();
        let r = self.rem[i];
        if r > 1 {
            f.push((r, 1));
        }
        f
    }
}

/// Factor every integer in `[a_lo, a_hi]` in one divide-as-you-sieve pass.
/// `base` must hold all primes up to `sqrt(a_hi)`.
pub fn factor_window(a_lo: u64, a_hi: u64, base: &[u64]) -> WindowFactors {
    let size = (a_hi - a_lo + 1) as usize;
    let mut rem: Vec<u64> = (a_lo..=a_hi).collect();
    let mut small: Vec<Vec<(u64, u32)>> = vec![Vec::new(); size];
    for &p in base {
        if p * p > a_hi {
            break;
        }
        let mut j = ((a_lo + p - 1) / p) * p; // first multiple of p in window
        while j <= a_hi {
            let i = (j - a_lo) as usize;
            let mut e = 0u32;
            // p divides rem[i] at least once: j is a multiple of p and only
            // smaller primes have been removed so far.
            while rem[i] % p == 0 {
                rem[i] /= p;
                e += 1;
            }
            small[i].push((p, e));
            j += p;
        }
    }
    WindowFactors { a_lo, small, rem }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn factor_window_reconstructs_every_value() {
        let base = base_primes(1000);
        let wf = factor_window(2, 4000, &base);
        for a in 2..4000u64 {
            let mut prod = 1u64;
            for (p, e) in wf.factor(a) {
                for _ in 0..e {
                    prod *= p;
                }
            }
            assert_eq!(prod, a, "reconstruction of {a}");
        }
    }

    #[test]
    fn hard_primes_are_prime_and_in_class() {
        let base = base_primes(100);
        let hp = hard_primes_in(2, 3000, &base);
        assert!(hp.contains(&1009)); // 1009 ≡ 169 (mod 840), prime
        assert!(hp.iter().all(|&n| is_hard(n)));
        // ascending + no even numbers
        assert!(hp.windows(2).all(|w| w[0] < w[1]));
    }
}
