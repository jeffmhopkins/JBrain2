"""How far a local turn has got through eating its prompt, read from llama-server's `/slots`.

Prefill is the longest silence this box produces. A cold 12k-token prompt on gpt-oss-120b
spends tens of seconds in prompt evaluation before the model says a word, and the priming
warm-up a model load ends with is the same thing at a larger size — MEASURED at 118 s of a
198 s load span, which is 60% of the wait the owner sits through with nothing on screen.

THE SCHEMA, measured on the box 2026-08-20 rather than guessed (the previous three attempts
at a load percentage were all guesses at a log format, and all shipped dead):

    is_processing              bool  — true for the whole prefill, false once the slot settles
    n_prompt_tokens_processed  int   — tokens eaten so far; exact, monotonic, advances in
                                       whole batches (2048 on this box's `-b`)
    n_prompt_tokens            int   — NOT the total. While busy it reads
                                       `processed + batch + cache`, i.e. a moving window that
                                       trails the work by one batch; the true total appears
                                       only once the slot settles. Using it as the denominator
                                       reads 0.75 when the real answer is 0.50.
    n_prompt_tokens_cache      int   — the reused prefix, and the reason the numerator is not
                                       measured against the whole prompt: these tokens are
                                       already in the KV cache, so they are never processed
                                       and `_fraction` takes them out of the denominator too
    next_token[0].n_decoded    int   — 0 throughout prefill, >0 once tokens are coming, which
                                       is what separates "still eating" from "answering"

So the numerator is exact and the DENOMINATOR IS NOT IN THE BODY. It comes from us instead:
we know how many characters we sent, and every completed turn returns its own
`usage.prompt_tokens`, so the chars-per-token ratio is measured from real traffic and gets
better with use (`calibrate`). Seeded with a general-purpose default so the first turn on a
cold process is approximate rather than absent.

What the bar therefore measures is THE WORK THIS TURN HAS TO DO, not the size of the prompt:
an agent turn deep in a tool loop sends 38k tokens and has to eat the 9k of web page it just
fetched, and a bar scaled to the 38k tops out at 22% and disappears (`_fraction`).

Approximate on purpose, and clamped: an estimate that runs ahead reads as arrived rather
than as 118%, and one that runs behind leaves the bar short of full, which is the honest
reading — we do not know the rest. The alternative, an exact count from llama-server's
`/tokenize`, was rejected: it costs a second full send of the prompt per slow turn to save an
error that a handful of turns' calibration removes anyway.

A cache-hit turn never gets here: an identical repeat prompt returns from the prompt cache
before the first sample is due (measured — the second run of a 12k-token prompt answered
instantly), and `watch` starts nothing until a turn has already been slow for `_FIRST_DELAY_S`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import structlog

log = structlog.get_logger()

# Reads `/slots` for one served model — `LocalGatewayClient.slots`, narrowed to a callable so
# the router does not have to depend on the gateway (or widen `LocalAdmitter`, which every
# structural test fake would then have to grow).
SlotsReader = Callable[[str], Awaitable[list[dict[str, object]]]]

# What a turn must be silent for before this starts reading. A warm turn on a primed prefix
# answers in well under a second, so three seconds is comfortably past anything the KV cache
# can serve and safely inside the tens of seconds a cold long prompt actually takes. It is
# also what keeps a cache hit from ever drawing a bar.
_FIRST_DELAY_S = 3.0

# How often to re-read once prefill is confirmed. The counter advances one batch at a time —
# measured at roughly a batch every 2.4 s on the 120B — so a second is fine enough to look
# continuous and coarse enough to cost almost nothing (one small local HTTP GET).
_POLL_S = 1.0

# The read is instrumentation and may never join the wait it measures.
_READ_TIMEOUT_S = 5.0

# Chars per token before this process has seen a completed turn. English prose through a
# BPE tokenizer sits near four; the models here run a little denser, and the value only has
# to be close — one real turn replaces it.
_DEFAULT_CHARS_PER_TOKEN = 3.7

# How hard a new measurement pulls the running ratio. Deliberately slow: prompt shape varies
# (a tool-heavy turn tokenizes differently from prose), and the ratio should track the model,
# not the last thing someone typed.
_CALIBRATION_WEIGHT = 0.25

# Learned chars-per-token, keyed by served model. Process-local and unpersisted by intent:
# it converges within a turn or two, and a wrong value costs a slightly-off bar, never a
# wrong answer — which is not worth a migration or a settings row.
_ratio: dict[str, float] = {}


def calibrate(served_model: str, prompt_chars: int, prompt_tokens: int) -> None:
    """Fold one completed turn's true token count into the estimate for the next.

    Called with the closing turn's `usage.prompt_tokens`, which is exact and already on the
    wire — no probe, no extra call. Guards the degenerate inputs (a turn that reported no
    usage, an empty prompt) rather than letting them poison the ratio."""
    if prompt_chars <= 0 or prompt_tokens <= 0:
        return
    measured = prompt_chars / prompt_tokens
    current = _ratio.get(served_model, _DEFAULT_CHARS_PER_TOKEN)
    _ratio[served_model] = current + (measured - current) * _CALIBRATION_WEIGHT


def estimate_tokens(served_model: str, prompt_chars: int) -> float:
    """Prompt tokens `prompt_chars` is expected to become on `served_model`."""
    return prompt_chars / _ratio.get(served_model, _DEFAULT_CHARS_PER_TOKEN)


def _fraction(slots: Sequence[dict[str, object]], estimated_tokens: float) -> float | None:
    """The busy slot's prefill fraction, or None when nothing is prefilling.

    Both halves of the division are about the work THIS request has to do, which on an agent
    turn is almost never the whole prompt. `n_prompt_tokens_processed` counts only tokens
    actually evaluated — the reused KV prefix is not in it — so the denominator has to have
    the prefix taken out of it too, or the bar reads the cache-miss share of the prompt and
    stops there. MEASURED on the box 2026-08-29, the same prompt sent twice:

        cold      47,318 chars → 18,057 tokens, cache 0,      processed 0 → 16,384
        +suffix   59,168 chars → 22,561 tokens, cache 18,054, processed 0 →  4,096

    The second round ate 4,507 new tokens; against the old denominator it could only ever
    reach 4,507/22,561 = 20%. That is exactly what the owner saw on a turn after a
    `web_fetch` (a 38,207-token prompt whose page was ~8.7k new tokens): the line read
    "Reading your prompt… 11%", climbed to 22%, and vanished — a bar that never finishes
    reads as a box that gave up.

    `n_prompt_tokens` earns its place here as a FLOOR on the total. While the slot is busy it
    reads `processed + batch + cache`, but clamped at the true total — measured above, the
    window stopped at 18,057 and 22,561 rather than running past them — so it can only
    correct our estimate upward, toward a number the engine knows and we are guessing at.

    None covers the states that all mean "do not draw a bar": no slot is busy, the busy one
    is already generating (`n_decoded` past zero), the body did not carry the counter (a
    build whose field names moved — this box floats on a digest off master, so the rename is
    a when), or the prefix accounts for the whole prompt, which leaves nothing to divide by."""
    if estimated_tokens <= 0:
        return None
    for slot in slots:
        if not slot.get("is_processing"):
            continue
        next_token = slot.get("next_token")
        if isinstance(next_token, list) and next_token:
            head = next_token[0]
            if isinstance(head, dict) and (head.get("n_decoded") or 0) > 0:
                return None  # past prefill — the model is answering
        processed = slot.get("n_prompt_tokens_processed")
        if not isinstance(processed, int):
            return None
        cached = slot.get("n_prompt_tokens_cache")
        window = slot.get("n_prompt_tokens")
        reused = cached if isinstance(cached, int) and cached > 0 else 0
        total = estimated_tokens
        if isinstance(window, int) and window > total:
            total = float(window)
        remaining = total - reused
        if remaining <= 0:
            return None
        return max(0.0, min(1.0, processed / remaining))
    return None


async def _answered_within(arrived: asyncio.Event, seconds: float) -> bool:
    """True when the turn produced something inside `seconds` — i.e. stop watching."""
    try:
        await asyncio.wait_for(arrived.wait(), seconds)
    except TimeoutError:
        return False
    return True


async def _poll(
    read: SlotsReader,
    served_model: str,
    estimated_tokens: float,
    on_progress: Callable[[float], Awaitable[None]],
    arrived: asyncio.Event,
    first_delay: float,
) -> None:
    """Read `/slots` on a cadence until the turn produces something, publishing what it finds."""
    if await _answered_within(arrived, first_delay):
        return  # answered inside the window — never prefill worth drawing
    while True:
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                slots = await read(served_model)
        except Exception:  # noqa: BLE001 — an instrument may never fail the turn it watches
            # Loud rather than swallowed: a reader that reports nothing is indistinguishable
            # from a box that never went slow, which is the failure this module exists to end.
            log.warning("llm.prefill_read_failed", model=served_model, exc_info=True)
            return
        fraction = _fraction(slots, estimated_tokens)
        if fraction is not None:
            with contextlib.suppress(Exception):
                await on_progress(fraction)
        if await _answered_within(arrived, _POLL_S):
            return


@asynccontextmanager
async def watch(
    read: SlotsReader | None,
    served_model: str,
    *,
    prompt_chars: int,
    on_progress: Callable[[float], Awaitable[None]] | None = None,
    first_delay: float | None = None,
) -> AsyncIterator[Callable[[], None]]:
    """Publish how far `served_model` has got through its prompt, while it is still eating it.

    Yields the callback that ends the watch — the caller invokes it on the first streamed
    part, and the polling stops there. `read=None` (a cloud route, local hosting off, a bare
    test router) or no `on_progress` starts no task at all, so this costs nothing on every
    box that cannot use it.

    `first_delay` is resolved here rather than defaulted in the signature, so the module
    constant stays patchable — a default argument binds once at import and a test that moves
    the constant would silently keep waiting the real three seconds."""
    if read is None or on_progress is None:
        yield lambda: None
        return
    arrived = asyncio.Event()
    task = asyncio.create_task(
        _poll(
            read,
            served_model,
            estimate_tokens(served_model, prompt_chars),
            on_progress,
            arrived,
            _FIRST_DELAY_S if first_delay is None else first_delay,
        )
    )
    try:
        yield arrived.set
    finally:
        # Cancelled rather than awaited: this runs on the turn's way out, including when the
        # consumer abandons the stream mid-answer, and a diagnostic must not hold that up.
        arrived.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
