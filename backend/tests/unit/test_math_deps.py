"""Dependency smoke test for the `calculate` tool (docs/archive/EXACT_MATH_TOOLS_PLAN.md;
CLAUDE.md rule #8 single-source-of-truth). Fails fast if `sympy` is missing from a synced
environment — so a broken `uv sync` / dev-setup step reddens here instead of deep in the
tool wiring."""

from __future__ import annotations


def test_sympy_importable_and_exact() -> None:
    import sympy

    # The surface jbrain.agent.mathtools relies on: exact rationals, a symbolic irrational
    # that does NOT collapse to a float, and arbitrary-precision evaluation on demand.
    assert sympy.Rational(1, 10) + sympy.Rational(1, 5) == sympy.Rational(3, 10)
    root = sympy.sqrt(sympy.Integer(2))
    assert not root.is_Rational
    # 1.4142135623730950…, so the 15th significant digit ROUNDS UP. Written out rather
    # than derived, because a smoke test that computes its own expectation checks nothing.
    assert str(sympy.N(root, 15)) == "1.41421356237310"


def test_the_parser_we_deliberately_do_not_use_is_the_one_that_evals() -> None:
    """A guard against a future refactor reaching for the obvious-looking API.

    `sympify` evaluates its input — it is a full escape surface, which is exactly why
    `mathtools` parses with the stdlib `ast` and walks a closed allowlist instead. This
    asserts the property that makes that choice necessary, so the reason survives in the
    suite rather than only in a comment."""
    import sympy

    from jbrain.agent.mathtools import MathError, evaluate

    # sympify happily builds something out of a name the allowlist has never heard of.
    assert sympy.sympify("foo") is not None
    # The tool refuses it.
    try:
        evaluate("foo")
    except MathError as exc:
        assert "not a known function or constant" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("evaluate() accepted an unknown name")
