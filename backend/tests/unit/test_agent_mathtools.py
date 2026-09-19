"""The `calculate` tool: exactness, the closed grammar, and errors a model can act on."""

import pytest

from jbrain.agent.loop import ToolContext
from jbrain.agent.mathtools import (
    KNOWN_NAMES,
    MAX_BRIEF_CHARS,
    MAX_EXPRESSION_CHARS,
    MAX_FACTORIAL_ARG,
    MAX_RESULT_DIGITS,
    MathError,
    build_math_handlers,
    calc_view,
    evaluate,
)
from jbrain.db.session import SessionContext


def _ctx() -> ToolContext:
    return ToolContext(session=SessionContext(principal_id="p", principal_kind="owner"), scopes=())


async def _call(**arguments: object) -> str:
    return str(await build_math_handlers()["calculate"](dict(arguments), _ctx()))


def _exact(rendered: str) -> str:
    lines = rendered.splitlines()
    return next(line.split(":", 1)[1].strip() for line in lines if line.startswith("exact:"))


def _decimal(rendered: str) -> str | None:
    for line in rendered.splitlines():
        if line.startswith("decimal:"):
            return line.split(":", 1)[1].strip()
    return None


# --- Exactness — the whole reason the tool exists ---------------------------


def test_large_integer_multiplication_is_exact() -> None:
    """The headline case: a product no model reliably predicts, to the last digit."""
    assert _exact(evaluate("987654321 * 123456789")) == "121932631112635269"


def test_float_literals_enter_as_rationals_so_there_is_no_binary_artifact() -> None:
    """`0.1 + 0.2` is the canonical float embarrassment. Entering the literals as the
    decimals they were written as — not as their binary approximations — is what makes it
    `3/10`, and the decimal line `0.3` rather than `0.30000000000000004`."""
    rendered = evaluate("0.1 + 0.2")
    assert _exact(rendered) == "3/10"
    assert _decimal(rendered) == "0.3"


def test_an_irrational_result_stays_symbolic_and_carries_an_approximation() -> None:
    rendered = evaluate("sqrt(2) / 3")
    assert _exact(rendered) == "sqrt(2)/3"
    assert _decimal(rendered) == "0.471404520791032"


def test_a_whole_number_gets_no_decimal_line() -> None:
    """`3.00000000000000` reads as a rounding the tool did not do."""
    assert _decimal(evaluate("6 / 2")) is None


def test_division_stays_a_fraction_rather_than_rounding() -> None:
    rendered = evaluate("1 / 3")
    assert _exact(rendered) == "1/3"
    assert _decimal(rendered) == "0.333333333333333"


def test_big_powers_are_exact() -> None:
    assert _exact(evaluate("2**64")) == "18446744073709551616"


def test_percentage_change_is_exact() -> None:
    """The everyday shape the tool is for — and a float path would give 27.55102040816327."""
    assert _exact(evaluate("(1250 - 980) / 980 * 100")) == "1350/49"


def test_digits_controls_only_the_approximation() -> None:
    rendered = evaluate("sqrt(2)", digits=30)
    assert _exact(rendered) == "sqrt(2)"
    assert _decimal(rendered) == "1.41421356237309504880168872421"


# --- The closed grammar -----------------------------------------------------


def test_dunder_import_is_an_unknown_name_not_a_blocked_call() -> None:
    """`__import__('os')` never gets near an evaluator: there is no name table entry for it,
    so it fails at the walk. The message names the whole allowlist, which is how a model
    fixes its own next call."""
    with pytest.raises(MathError) as exc:
        evaluate("__import__('os')")
    assert "'__import__' is not a known function or constant" in str(exc.value)
    assert "sqrt" in str(exc.value)


@pytest.mark.parametrize(
    "expression",
    [
        "().__class__",
        "os.system('ls')",
        "open('/etc/passwd')",
        "[x for x in range(10)]",
        "lambda: 1",
        "(1).bit_length()",
        "eval('1+1')",
        "exec('x=1')",
        "globals()",
        "x := 4",
        "'a' * 3",
        "f'{1}'",
        "1 if True else 2",
        "print(1)",
        "sqrt(2); print(1)",
    ],
)
def test_every_escape_shape_is_refused(expression: str) -> None:
    """Attributes, subscripts, comprehensions, lambdas, walrus, f-strings, strings, calls
    outside the table, and a second statement — the language is closed, not filtered."""
    with pytest.raises(MathError):
        evaluate(expression)


def test_a_string_literal_is_not_a_number() -> None:
    with pytest.raises(MathError, match="str is not a number"):
        evaluate("2 + 'two'")


def test_bitwise_operators_are_named_rather_than_silently_dropped() -> None:
    with pytest.raises(MathError, match=r"the \^ operator is not allowed"):
        evaluate("2 ^ 3")


def test_constants_and_functions_are_exactly_the_published_table() -> None:
    """`KNOWN_NAMES` is what the error message advertises, so every name on it has to
    actually work — an allowlist that names a function the walker rejects teaches the model
    a call it will then keep retrying."""
    assert "pi" in KNOWN_NAMES and "sqrt" in KNOWN_NAMES
    assert "__import__" not in KNOWN_NAMES and "eval" not in KNOWN_NAMES
    binary = {"atan2", "root", "gcd", "lcm", "binomial"}
    constants = {"pi", "e", "tau", "phi"}
    for name in KNOWN_NAMES:
        if name in constants:
            evaluate(name)
        else:
            evaluate(f"{name}(2, 3)" if name in binary else f"{name}(1)")


# --- Errors the model can act on --------------------------------------------


def test_a_syntax_error_names_the_character_and_the_position() -> None:
    message = _error("(2 + 3))")
    assert message.startswith("SyntaxError:")
    assert "unexpected ')'" in message
    assert "at position 7" in message


def test_division_by_zero_is_an_error_not_a_symbol() -> None:
    """sympy answers 1/0 with `zoo`, which would reach the model as a mystery."""
    assert _error("1 / 0") == "ZeroDivisionError: division by zero"
    assert _error("5 % 0") == "ZeroDivisionError: modulo by zero"


def test_errors_are_one_short_line_never_a_traceback() -> None:
    for expression in ("(2 + 3))", "1 / 0", "__import__('os')", "sqrt(1, 2, 3)", "2 ^ 3"):
        message = _error(expression)
        assert "\n" not in message, f"{expression} produced a multi-line error"
        assert "Traceback" not in message
        assert len(message) < 400, f"{expression} produced {len(message)} characters"


def test_a_wrong_arity_call_reports_the_function_not_the_internals() -> None:
    message = _error("sqrt(1, 2, 3)")
    assert message.startswith("ValueError: sqrt()")


def _error(expression: str) -> str:
    with pytest.raises(MathError) as exc:
        evaluate(expression)
    return str(exc.value)


# --- Cost bounds ------------------------------------------------------------


def test_an_enormous_power_is_refused_before_it_is_computed() -> None:
    """`2**10**10` is one token wide and would allocate for the rest of the day. The
    refusal has to come from the static guard, not from a timeout after the fact."""
    message = _error("2**10**10")
    assert "digits" in message and "limit" in message


def test_factorial_is_capped_with_the_real_limit_in_the_message() -> None:
    assert f"{MAX_FACTORIAL_ARG:,}" in _error("factorial(10**9)")
    assert _exact(evaluate("factorial(20)")) == "2432902008176640000"


def test_an_over_long_expression_is_refused_with_both_numbers() -> None:
    message = _error("1+" * MAX_EXPRESSION_CHARS + "1")
    assert "too long" in message and f"{MAX_EXPRESSION_CHARS:,}" in message


def test_a_deeply_nested_expression_is_refused() -> None:
    """Redundant parentheses produce no AST nodes, so the guard has to be tripped by real
    nesting — which is also the only shape that can actually blow the recursive walk."""
    assert "nests too deeply" in _error("(1+" * 40 + "1" + ")" * 40)


def test_an_enormous_exact_result_is_refused_rather_than_truncated() -> None:
    """The deliberate asymmetry with `run_python`, which truncates its stdout: the first
    4,000 digits of a 31,000-digit integer is not a shortened answer, it is a different
    number, and a model handed one quotes it as the result. The refusal names the size.

    It is also what keeps CPython's own 4,300-digit int-to-string ceiling from surfacing as
    an error about `sys.set_int_max_str_digits`."""
    message = _error("factorial(9000)")
    assert "digits" in message and f"{MAX_RESULT_DIGITS:,}" in message
    # Just under the cap still answers — the bound is on the result, not on ambition.
    assert _exact(evaluate("factorial(1000)")).startswith("402387260077093773543702433923")


# --- The handler ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_handler_returns_the_rendered_result() -> None:
    out = await _call(expression="123456 * 789")
    assert "exact:   97406784" in out


@pytest.mark.asyncio
async def test_the_handler_returns_an_error_as_an_observation_never_a_raise() -> None:
    """A tool error is something the model recovers from on the next step (loop.py
    `_dispatch`), so a bad expression must come back as text, not as an exception."""
    out = await _call(expression="__import__('os')")
    assert out.startswith("NameError:")


@pytest.mark.asyncio
async def test_an_empty_expression_says_so() -> None:
    assert "no expression" in await _call(expression="  ")


@pytest.mark.asyncio
@pytest.mark.parametrize("digits", [0, -5, 9999, "lots", None])
async def test_a_bad_digits_argument_is_clamped_not_refused(digits: object) -> None:
    """Never fail a correct expression over a malformed optional argument."""
    out = await _call(expression="sqrt(2)", digits=digits)
    assert "exact:   sqrt(2)" in out


# --- the Worked row's answer (SHOW_THE_WORKING_PLAN.md W1) -------------------


def test_the_render_carries_the_exact_answer_as_a_value() -> None:
    """The handler must not have to parse the text it just built — that would be the very
    mistake `result_brief` exists to stop, one layer further down."""
    rendered = evaluate("(2847 - 2633) * 12")
    assert rendered.brief == "2568"
    assert rendered.splitlines()[0] == "(2847 - 2633) * 12"


def test_the_answer_is_the_exact_form_not_the_decimal() -> None:
    """Being exact is what this tool is for. A row reading `3/10` is the whole argument for
    having it; `0.3` is what any calculator would have said."""
    assert evaluate("0.1 + 0.2").brief == "3/10"


def test_a_huge_result_is_capped_to_a_row() -> None:
    """The row is one line on a phone. The full value stays in the step's result text, which
    is where the cap is meant to send the reader."""
    brief = evaluate("factorial(200)").brief
    assert len(brief) == MAX_BRIEF_CHARS
    assert brief.endswith("…")


def test_calculate_renders_the_same_view_run_python_does() -> None:
    """One component, two tools — they are the same act, and a second component would be a
    second place for them to disagree."""
    view = calc_view("1/3", evaluate("1/3"))
    assert view.view == "code_run"
    assert view.data["language"] == "expression"
    assert view.data["code"] == "1/3"
    assert view.data["result"] == "1/3"
    assert view.data["decimal"] == "0.333333333333333"


def test_calculate_does_not_borrow_the_sandbox_containment() -> None:
    """It runs in-process on a restricted AST and never reaches the sandbox. Claiming "no
    network · scratch only" would be describing a container it never entered."""
    seals = calc_view("2+2", evaluate("2+2")).data["containment"]
    assert seals == ["exact arithmetic", "no code executed"]
