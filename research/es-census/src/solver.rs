//! The residual certificate search, mirroring `bulk_generate.solve_residual` /
//! `minimal_certificate`, but taking a precomputed factorization of `a` and
//! doing the (big) divisor arithmetic in `num-bigint` so it is exact at any
//! range the census can reach.

use num_bigint::BigUint;
use num_traits::{One, Zero};

use crate::sieve::WindowFactors;

/// A solved certificate: minimal R and the triple `(a, b, c)` with `b <= c`.
pub struct Certificate {
    pub r: u64,
    pub a: BigUint,
    pub b: BigUint,
    pub c: BigUint,
}

/// All divisors of `m^2 = n^2 * a^2`, built from the factorization of `a`
/// (`n` is a known prime and `a < n`, so `n` is not among `a`'s factors).
fn divisors_of_m2(a_factors: &[(u64, u32)], n: u64) -> Vec<BigUint> {
    let mut divs = vec![BigUint::one()];
    let mut mul_in = |base: u64, exp: u32, divs: &mut Vec<BigUint>| {
        let bp = BigUint::from(base);
        let mut powers = Vec::with_capacity(exp as usize + 1);
        let mut pk = BigUint::one();
        for _ in 0..=exp {
            powers.push(pk.clone());
            pk *= &bp;
        }
        let mut next = Vec::with_capacity(divs.len() * powers.len());
        for d in divs.iter() {
            for w in &powers {
                next.push(d * w);
            }
        }
        *divs = next;
    };
    mul_in(n, 2, &mut divs); // n^2
    for &(p, e) in a_factors {
        mul_in(p, 2 * e, &mut divs);
    }
    divs
}

/// Try to solve for a fixed residual `R` with `a = (n+R)/4` already known.
fn solve_residual(
    n: u64,
    r: u64,
    a: u64,
    a_factors: &[(u64, u32)],
) -> Option<(BigUint, BigUint, BigUint)> {
    let m = BigUint::from(n) * BigUint::from(a);
    let rr = BigUint::from(r);
    let want = (&rr - (&m % &rr)) % &rr; // (-m) mod R
    let m2 = &m * &m;

    // Smallest divisor k of m^2 with k ≡ want (mod R) — gives the smallest b.
    let mut best: Option<BigUint> = None;
    for k in divisors_of_m2(a_factors, n) {
        if &k % &rr == want {
            match &best {
                Some(b) if *b <= k => {}
                _ => best = Some(k),
            }
        }
    }
    let k = best?;

    if (&k + &m) % &rr != BigUint::zero() {
        return None;
    }
    let complement = &m2 / &k; // exact: k divides m^2
    if (&complement + &m) % &rr != BigUint::zero() {
        return None;
    }
    let mut b = (&k + &m) / &rr;
    let mut c = (&complement + &m) / &rr;
    if b > c {
        std::mem::swap(&mut b, &mut c);
    }
    let ba = BigUint::from(a);
    let bn = BigUint::from(n);
    // Exact identity check: 4*a*b*c == n*(b*c + a*c + a*b).
    let lhs = BigUint::from(4u32) * &ba * &b * &c;
    let rhs = &bn * (&b * &c + &ba * &c + &ba * &b);
    if lhs != rhs {
        return None;
    }
    Some((ba, b, c))
}

/// Minimal admissible R (and its certificate) for a hard prime `n`, factoring
/// `a` by window lookup. Hard primes are `≡ 1 (mod 4)`, so `R ≡ 3 (mod 4)`.
pub fn minimal_certificate(
    n: u64,
    wf: &WindowFactors,
    max_r: u64,
) -> Option<Certificate> {
    let mut r = (4 - (n % 4)) % 4;
    if r == 0 {
        r = 4;
    }
    while r <= max_r {
        if (n + r) % 4 == 0 {
            let a = (n + r) / 4;
            let af = wf.factor(a);
            if let Some((a, b, c)) = solve_residual(n, r, a, &af) {
                return Some(Certificate { r, a, b, c });
            }
        }
        r += 4;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sieve::{base_primes, factor_window, hard_primes_in};

    #[test]
    fn certificates_hold_over_a_small_range() {
        let base = base_primes(2000);
        let hard = hard_primes_in(2, 5000, &base);
        // a = (n+R)/4 <= (5000+400)/4 = 1350
        let wf = factor_window(2, 1400, &base);
        assert!(!hard.is_empty());
        for &n in &hard {
            let cert = minimal_certificate(n, &wf, 400)
                .unwrap_or_else(|| panic!("n={n} unsolved"));
            assert_eq!((n + cert.r) % 4, 0);
            // 4*a*b*c == n*(b*c + a*c + a*b), exact.
            let four = BigUint::from(4u32);
            let bn = BigUint::from(n);
            let lhs = &four * &cert.a * &cert.b * &cert.c;
            let rhs = &bn * (&cert.b * &cert.c + &cert.a * &cert.c + &cert.a * &cert.b);
            assert_eq!(lhs, rhs, "identity for n={n}");
        }
    }
}
