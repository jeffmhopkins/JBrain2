---
name: calculate
version: 1
permission: read
params:
  type: object
  properties:
    expression:
      type: string
      description: >-
        The expression to evaluate, e.g. "123456 * 789", "sqrt(2) / 3",
        "(1250 - 980) / 980 * 100", "2**64". Numbers and operators only — there are no
        variables, so write the numbers out.
    digits:
      type: integer
      description: >-
        How many significant digits the decimal approximation carries. Defaults to 15;
        anything above 50 is clamped. Does not affect the exact result, which is always
        exact.
  required: [expression]
examples:
  - {expression: "987654321 * 123456789"}
  - {expression: "(1250 - 980) / 980 * 100"}
  - {expression: "sqrt(2) / 3", digits: 20}
---
Compute a value exactly. Use this for ANY arithmetic beyond a single-digit sum — a
multiplication, a percentage, a unit conversion, a total, a ratio, a power. It is
cheaper than being wrong, and being wrong about a number is the one mistake the owner
cannot spot by reading your answer.

Exact really means exact: `0.1 + 0.2` is `3/10`, not `0.30000000000000004`, and
`987654321 * 123456789` is the whole integer, not a rounded one. An irrational result
stays in its true form and comes back with a decimal alongside it — `sqrt(2) / 3`
returns `exact: sqrt(2)/3` and `decimal: 0.471404520791032` — so quote whichever the
question wants, and prefer the decimal when the owner asked for "how much".

Operators: `+ - * / // % **`, with parentheses. Functions: sqrt, cbrt, root, exp, log
(natural), ln, log10, log2, sin, cos, tan, asin, acos, atan, atan2, sinh, cosh, tanh,
abs, floor, ceiling, round, factorial, gcd, lcm, binomial, min, max, degrees, radians.
Constants: pi, e, tau, phi. Trig is in radians — wrap degrees with `radians(30)`.

There are no variables and no assignment: this evaluates one expression, so substitute
the numbers yourself. It reads nothing — not the owner's notes, not the web, not a
clock — so it can answer only what you type into it. For a calculation with steps,
loops, dates, or a table of numbers to work through, use run_python instead.

Say what you are about to compute before you call it — which numbers, which formula —
then call it and report what it returned. Do not restate a result from memory; a number
you retype is a number you can get wrong.
