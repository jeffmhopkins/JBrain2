"""The `calculate` tool: one expression in, the EXACT value out.

A language model does arithmetic by predicting digits, and it is wrong often enough
that `DEEP_RESEARCH_SCRATCHPAD_PLAN.md` documents number-invention as a recurring,
unfixed failure class after three prompt versions. `JERV_CONTEXT_BUDGET_PLAN.md` §5
rejected a Python sandbox as the answer to that but named the exception explicitly —
"the only surviving argument is the deterministic-arithmetic one" — and this is that
argument built: a mechanical backstop, so a multiplication is computed rather than
recalled.

**Exact means exact.** Every literal enters as a rational (`0.1` is `1/10`, not the
binary float that is 0.1000000000000000055…), so `0.1 + 0.2` is `3/10` and not the
`0.30000000000000004` that makes a model second-guess a correct answer. An irrational
result stays symbolic — `sqrt(2)/3` is returned as `sqrt(2)/3` — with a decimal
approximation alongside, because the model usually wants a number it can put in a
sentence and the owner usually wants the form that is actually true.

**Why this is not code execution.** ASSISTANT.md refuses code execution in the agent, and
that refusal is intact here: this is not `eval`, and it is not sympy's `sympify`/`parse_expr`
either — both of those `eval()` their input and are a full escape surface. The expression is
parsed to an AST by the stdlib and then walked by `_evaluate` against a closed allowlist of
node types, operators, functions and constants. There are no names, no attributes, no
subscripts, no comprehensions and no assignment, so `__import__('os')` is not a blocked call
— it is a name that was never in the table. Arbitrary code lives in the `pysandbox` sidecar
(`run_python`), behind process and network isolation; nothing here can reach it.
"""

from __future__ import annotations

import ast
import asyncio
import operator
from collections.abc import Callable
from decimal import Decimal, localcontext
from itertools import islice
from typing import Any

import sympy

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput

# Every bound below exists because sympy is happy to be asked for something that never
# returns, and this tool runs inside a live chat turn. They are deliberately generous for
# real questions and hostile to the shapes that hang: `2**10**10` is one token wide and
# would allocate for the rest of the day.
MAX_EXPRESSION_CHARS = 2_000
MAX_AST_NODES = 500
MAX_AST_DEPTH = 25
# How many digits an exact result may have. Past this the tool refuses rather than
# truncates, which is the opposite of what `run_python` does with its stdout — and
# deliberately so: half of a 31,000-digit integer is not a shortened answer, it is a
# DIFFERENT number, and a model handed one will quote it as if it were the result. There is
# nothing useful to say about a 4,000-digit answer in a chat anyway, so the refusal costs
# nothing real and names the limit so the model can ask something smaller.
#
# It also sits just under CPython's own 4,300-digit int-to-string ceiling (PEP 7 / the
# CVE-2020-10735 mitigation), so rendering can never trip that and surface as a ValueError
# about `sys.set_int_max_str_digits` — a message about interpreter internals is exactly the
# kind of dead end this tool's errors are supposed to not be.
MAX_RESULT_DIGITS = 4_000
# A separate, much cruder guard: `factorial(10**9)` would not return today, so the argument
# is bounded before it is computed. The digit cap above is what refuses a merely large
# factorial; this one exists so a hostile argument cannot hang the thread at all.
MAX_FACTORIAL_ARG = 10_000

DEFAULT_DIGITS = 15
MAX_DIGITS = 50

# The last line of defence, and an honest one: the caps above bound every shape we know
# hangs, so this should be unreachable. If it ever fires, the evaluating thread is orphaned
# rather than killed (Python cannot interrupt one) — it finishes on its own under those same
# caps while the turn recovers. That trade is why the caps are static and tight.
EVAL_TIMEOUT_SECONDS = 5.0


class MathError(ValueError):
    """An expression the tool refuses or cannot evaluate. The message is the whole point:
    it is one short line the model can act on, never a stack trace."""


def _rational(value: int | float) -> sympy.Expr:
    """A numeric literal as an EXACT rational.

    `Rational(0.1)` would faithfully convert the binary float and hand back
    3602879701896397/36028797018963968 — technically exact, and exactly the artifact this
    tool exists to remove. `repr` gives the shortest decimal that round-trips (`'0.1'`), and
    `Decimal.as_integer_ratio` turns that into 1/10, which is what the owner typed and meant.
    """
    if isinstance(value, int):
        return sympy.Integer(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise MathError("ValueError: infinity and NaN are not valid numbers here")
    return sympy.Rational(*Decimal(repr(value)).as_integer_ratio())


def _checked_div(left: sympy.Expr, right: sympy.Expr) -> sympy.Expr:
    # sympy answers 1/0 with `zoo` (complex infinity) rather than raising, which would
    # surface to the model as a mysterious symbol instead of the mistake it made.
    if right.is_zero:
        raise MathError("ZeroDivisionError: division by zero")
    return left / right


def _checked_mod(left: sympy.Expr, right: sympy.Expr) -> sympy.Expr:
    if right.is_zero:
        raise MathError("ZeroDivisionError: modulo by zero")
    return sympy.Mod(left, right)


def _checked_floordiv(left: sympy.Expr, right: sympy.Expr) -> sympy.Expr:
    if right.is_zero:
        raise MathError("ZeroDivisionError: division by zero")
    return sympy.floor(left / right)


def _digit_estimate(value: sympy.Expr) -> int:
    """Roughly how many digits `value` prints as.

    Counted from the BIT LENGTH, never from `str()`: this is used to refuse oversized
    results, and `str()` on a large enough integer is itself the thing being guarded against
    (CPython raises past 4,300 digits). log10(2) converts bits to digits."""
    if value.is_Integer:
        return int(int(value).bit_length() * 0.30103) + 1
    if value.is_Rational:
        p, q = value.p, value.q  # type: ignore[attr-defined]
        return _digit_estimate(sympy.Integer(p)) + _digit_estimate(sympy.Integer(q))
    return 1


def _checked_pow(base: sympy.Expr, exponent: sympy.Expr) -> sympy.Expr:
    # The one operator that turns a short expression into an unbounded computation. Guarded
    # BEFORE evaluation: by the time sympy has computed 2**10**10 the damage is done.
    if exponent.is_Integer and base.is_Rational:
        digits = _digit_estimate(base) * abs(int(exponent))
        if digits > MAX_RESULT_DIGITS:
            raise MathError(
                f"ValueError: that power has about {digits:,} digits "
                f"(limit {MAX_RESULT_DIGITS:,}) — try a smaller exponent"
            )
    return base**exponent


def _checked_factorial(value: sympy.Expr) -> sympy.Expr:
    if not value.is_Integer or value < 0:
        raise MathError("ValueError: factorial needs a non-negative whole number")
    if int(value) > MAX_FACTORIAL_ARG:
        raise MathError(f"ValueError: factorial is capped at {MAX_FACTORIAL_ARG:,}")
    return sympy.factorial(value)


_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: _checked_div,
    ast.FloorDiv: _checked_floordiv,
    ast.Mod: _checked_mod,
    ast.Pow: _checked_pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Named constants, exact. `e` is Euler's number rather than a variable — there are no
# variables here, so the name is unambiguous.
_CONSTANTS: dict[str, sympy.Expr] = {
    "pi": sympy.pi,
    "e": sympy.E,
    "tau": 2 * sympy.pi,
    "phi": sympy.GoldenRatio,
}

# The closed function table. A name absent from here is not a function — there is no
# fallback lookup into sympy's namespace, which is what would reopen the escape surface.
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sqrt": sympy.sqrt,
    "cbrt": sympy.cbrt,
    "root": sympy.root,
    "exp": sympy.exp,
    "log": sympy.log,
    "ln": sympy.log,
    "log10": lambda x: sympy.log(x, 10),
    "log2": lambda x: sympy.log(x, 2),
    "sin": sympy.sin,
    "cos": sympy.cos,
    "tan": sympy.tan,
    "asin": sympy.asin,
    "acos": sympy.acos,
    "atan": sympy.atan,
    "atan2": sympy.atan2,
    "sinh": sympy.sinh,
    "cosh": sympy.cosh,
    "tanh": sympy.tanh,
    "abs": sympy.Abs,
    "floor": sympy.floor,
    "ceiling": sympy.ceiling,
    "ceil": sympy.ceiling,
    "round": lambda x, n=0: sympy.Rational(round(sympy.Rational(x), int(n))),
    "factorial": _checked_factorial,
    "gcd": sympy.gcd,
    "lcm": sympy.lcm,
    "binomial": sympy.binomial,
    "min": lambda *a: sympy.Min(*a),
    "max": lambda *a: sympy.Max(*a),
    "degrees": lambda x: x * 180 / sympy.pi,
    "radians": lambda x: x * sympy.pi / 180,
}

KNOWN_NAMES = tuple(sorted(set(_FUNCTIONS) | set(_CONSTANTS)))


def _guard_shape(tree: ast.Expression) -> None:
    """Refuse a pathological expression before evaluating any of it — a deeply nested or
    enormous tree is a cost attack, and the walk below is recursive."""
    # islice, not len(list(walk)): a hostile expression's whole point is being enormous, so
    # the count must stop at the cap rather than materialize the tree to discover it.
    if sum(1 for _ in islice(ast.walk(tree), MAX_AST_NODES + 1)) > MAX_AST_NODES:
        raise MathError(f"ValueError: that expression is too complex (over {MAX_AST_NODES} parts)")
    if _depth(tree) > MAX_AST_DEPTH:
        raise MathError(
            f"ValueError: that expression nests too deeply (over {MAX_AST_DEPTH} levels)"
        )


def _depth(node: ast.AST, level: int = 0) -> int:
    if level > MAX_AST_DEPTH:
        return level
    children = list(ast.iter_child_nodes(node))
    if not children:
        return level
    return max(_depth(child, level + 1) for child in children)


def _evaluate(node: ast.AST) -> sympy.Expr:
    """Walk one node against the allowlist. Every branch is an explicit admission; the
    final `raise` is what makes this a closed language rather than a filtered one."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise MathError(f"ValueError: {type(node.value).__name__} is not a number")
        return _rational(node.value)
    if isinstance(node, ast.BinOp):
        handler = _BINARY_OPS.get(type(node.op))
        if handler is None:
            raise MathError(f"ValueError: the {_op_name(node.op)} operator is not allowed here")
        return handler(_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPS.get(type(node.op))
        if unary is None:
            raise MathError(f"ValueError: the {_op_name(node.op)} operator is not allowed here")
        return unary(_evaluate(node.operand))
    if isinstance(node, ast.Name):
        constant = _CONSTANTS.get(node.id)
        if constant is None:
            raise MathError(_unknown_name(node.id))
        return constant
    if isinstance(node, ast.Call):
        return _call(node)
    # Attributes, subscripts, comprehensions, lambdas, walrus, f-strings, starred args —
    # everything an escape would need — land here, named rather than silently dropped.
    raise MathError(f"ValueError: {type(node).__name__} is not allowed in an expression")


def _call(node: ast.Call) -> sympy.Expr:
    if not isinstance(node.func, ast.Name):
        raise MathError("ValueError: only plain function calls like sqrt(2) are allowed")
    func = _FUNCTIONS.get(node.func.id)
    if func is None:
        raise MathError(_unknown_name(node.func.id))
    if node.keywords:
        raise MathError(f"ValueError: {node.func.id}() does not take keyword arguments")
    args = [_evaluate(arg) for arg in node.args]
    try:
        return func(*args)
    except MathError:
        raise
    except Exception as exc:  # noqa: BLE001 — any sympy complaint becomes one short line
        raise MathError(f"ValueError: {node.func.id}() {_brief(exc)}") from exc


def _unknown_name(name: str) -> str:
    """The most common error the model will hit, so it carries the whole table — a model
    that can see the allowlist fixes its own call on the next step instead of guessing."""
    known = ", ".join(KNOWN_NAMES)
    return f"NameError: '{name}' is not a known function or constant. Known: {known}"


def _op_name(op: ast.AST) -> str:
    return {
        ast.BitAnd: "&",
        ast.BitOr: "|",
        ast.BitXor: "^",
        ast.LShift: "<<",
        ast.RShift: ">>",
        ast.MatMult: "@",
        ast.Invert: "~",
        ast.Not: "not",
    }.get(type(op), type(op).__name__)


def _brief(exc: Exception) -> str:
    """One short clause from an exception — the model needs the mistake, not the trace."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:160]


def _syntax_message(exc: SyntaxError, expression: str) -> str:
    """A syntax error the model can act on: what, and where. `offset` is 1-based and can
    run past the string on an unexpected EOF, so it is clamped to a position that exists."""
    offset = exc.offset or 0
    position = max(0, min(offset - 1, len(expression) - 1)) if expression else 0
    char = expression[position] if expression else ""
    where = f" at position {position}" if expression else ""
    if char and char.strip():
        return f"SyntaxError: unexpected '{char}'{where}"
    return f"SyntaxError: {_brief(exc)}{where}"


def _decimal(value: sympy.Expr, digits: int) -> str | None:
    """The decimal approximation, or None when the exact form already IS the decimal.

    A whole number needs no approximation, and printing `3` as `3.00000000000000` reads as
    a rounding the tool did not do."""
    if value.is_Integer:
        return None
    approx = sympy.N(value, digits)
    if not approx.is_real:
        return None
    # `N` pads to the requested precision, so 3/10 comes back `0.300000000000000`. The
    # trailing zeros are noise the model would otherwise copy into its answer.
    #
    # `normalize()` rounds to the ACTIVE decimal context, which defaults to 28 significant
    # digits — so stripping the padding this way silently truncated a digits=30 request to
    # 28, which is the one thing that argument exists to control. The local context sizes
    # itself to the request instead, with headroom for the exponent forms.
    with localcontext() as context:
        context.prec = digits + 5
        return f"{Decimal(str(approx)).normalize():f}"


def evaluate(expression: str, *, digits: int = DEFAULT_DIGITS) -> str:
    """Evaluate `expression` exactly and render it for the model. Raises MathError with a
    single readable line for anything it will not or cannot do."""
    expression = expression.strip()
    if not expression:
        raise MathError("ValueError: no expression to calculate")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise MathError(
            f"ValueError: that expression is too long ({len(expression):,} characters, "
            f"limit {MAX_EXPRESSION_CHARS:,})"
        )
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise MathError(_syntax_message(exc, expression)) from exc
    except ValueError as exc:  # null bytes and friends never reach SyntaxError
        raise MathError(f"ValueError: {_brief(exc)}") from exc
    _guard_shape(tree)
    try:
        value = _evaluate(tree)
    except MathError:
        raise
    except RecursionError as exc:
        raise MathError("ValueError: that expression nests too deeply") from exc
    except Exception as exc:  # noqa: BLE001 — a sympy failure is the model's to recover from
        raise MathError(f"ValueError: {_brief(exc)}") from exc
    return _render(expression, value, digits)


def _render(expression: str, value: sympy.Expr, digits: int) -> str:
    if value.has(sympy.zoo) or value.has(sympy.oo) or value.has(sympy.nan):
        raise MathError("ValueError: that has no finite value")
    # Checked before printing, not after: see MAX_RESULT_DIGITS on why this refuses instead
    # of truncating, and why `str()` cannot be the thing that discovers the size.
    size = _digit_estimate(value)
    if size > MAX_RESULT_DIGITS:
        raise MathError(
            f"ValueError: that result has about {size:,} digits "
            f"(limit {MAX_RESULT_DIGITS:,}) — ask for something smaller"
        )
    exact = sympy.sstr(value)
    lines = [expression, f"exact:   {exact}"]
    approx = _decimal(value, digits)
    if approx is not None and approx != exact:
        lines.append(f"decimal: {approx}")
    return "\n".join(lines)


def build_math_handlers() -> dict[str, ToolHandler]:
    """The `calculate` tool — arithmetic only, no owner data, no domain. `read`-class, so
    curator's wildcard picks it up; every closed-allowlist persona names it explicitly."""

    async def calculate_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        expression = str(arguments.get("expression", ""))
        digits = _requested_digits(arguments.get("digits"))
        try:
            # Off the event loop: an expression inside the caps returns in milliseconds, but
            # the api serves every other chat turn from this same loop and must not stall on
            # the one that does not.
            rendered = await asyncio.wait_for(
                asyncio.to_thread(evaluate, expression, digits=digits), EVAL_TIMEOUT_SECONDS
            )
        except MathError as exc:
            return ToolOutput(str(exc))
        except TimeoutError:
            return ToolOutput(
                "TimeoutError: that expression took too long to evaluate — try breaking it up"
            )
        return ToolOutput(rendered)

    return {"calculate": calculate_tool}


def _requested_digits(raw: object) -> int:
    """Clamp rather than refuse: a bad `digits` is never worth failing a correct expression
    over, and the model learns nothing useful from being told its 200 became 50."""
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_DIGITS
    return max(1, min(value, MAX_DIGITS))
