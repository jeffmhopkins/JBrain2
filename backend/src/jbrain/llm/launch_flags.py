"""The llama-server flags an operator may set remotely, and the one validator for them.

Shared by the settings API (which refuses a bad flag with a 422) and the config renderer
(`llama_swap_config.render`, which re-checks what is STORED before it reaches a launch
command): a value that bypassed the API — a hand-edited settings row, a future write path —
must never become an argv llama-swap executes."""

import re

# The llama-server flags an operator may set remotely. An ALLOWLIST, not a filter: llama-server
# REFUSES TO START on an unknown flag, and the flag lands in that model's launch command, so an
# unrestricted argv would let one API call make a model permanently unloadable — on a box with no
# terminal to fix it from. Each entry is a flag we have a reason to want to try live:
#   --swa-full        keep full history on sliding-window layers (roughly doubles gpt-oss's KV)
#   -b / -ub          logical / physical prompt batch — the prompt-processing throughput knobs
#   --spec-type            which speculative-decoding mode to serve with (e.g. draft-mtp)
#   --spec-draft-n-max     how many tokens a draft proposes per round
#   --spec-draft-n-min     the floor below which a draft is discarded
#   --spec-draft-p-min     confidence gate that stops a draft early; llama.cpp's own default is
#                          0.00 (ungated), so the useful value is one nobody can guess in advance
# The speculative four are here because their right values are EMPIRICAL and hardware-specific:
# published Strix Halo numbers disagree on n-max, and p-min's payoff depends on generation
# length. Without them a single tuning iteration costs a catalog edit, a release and an
# Ops → Update — which is how a knob ends up never being tuned at all. Turning speculation on
# also pins the model to one slot, and the config generator derives that from the flags it is
# about to write (`llama_swap_config._is_speculative`), so an operator flag gets the same clamp
# a catalog flag does. A bad VALUE here can stop a model loading, same as a bad `-ub`; clearing
# is the same call with no args and does not require the model to be loadable.
#   --image-min-tokens     FLOOR on how many tokens an image is encoded to
#   --image-max-tokens     CEILING on the same (llama.cpp defaults to 4096 for this projector
#                          family and the catalog pins only the floor, at 2048)
# The image pair is here for the same reason as the speculative four: the right value is
# empirical and only observable against real images. The floor is what decides whether small
# text in a photo survives to the model — raise it and OCR on a curved bottle label or a
# receipt gets legible, at the cost of prefill and KV — and no amount of reading can say where
# that threshold sits for a given camera and subject. Pinning the ceiling bounds the CLIP
# workspace, which matters much less now that flash attention is confirmed on (the term is
# linear in patches, not quadratic), but it remains the lever if a build ever loses `-fa`.
#   --ctx-checkpoints  how many per-slot context checkpoints llama-server keeps
#   --cache-reuse      minimum chunk size worth salvaging from a matching prompt prefix
# MEASURED 2026-08-18: prompt caching WORKS on the hybrid — a warm prime is 0.99 s reusing
# 32,485 of 32,489 tokens. What costs a cold prefill (~101 s, which is this hardware's ~243
# tok/s, not a fault) is anything that INVALIDATES the checkpoint. Sweeping the checkpoint
# count is still the right experiment, but 2 vs 8 measured identical — because if no checkpoint
# MATCHES, the count cannot matter. Diagnose with `-lv 4` first (docs/runbooks/STRIX_HALO_SETUP.md).
# The cache pair is here because the SLOW-PREFILL investigation cannot start without it, and on
# a HYBRID model our shipped values are the prime suspects. Qwen3.8 runs 48 of its 65 layers as
# Gated DeltaNet, which carries a recurrent state: that state cannot be KV-shifted or partially
# rewound, so `--cache-reuse` can only ever salvage the 16 attention layers, and checkpoints are
# the ONLY mechanism that lets such a model resume mid-sequence at all. We serve
# `--ctx-checkpoints 2` (down from llama.cpp's 32, to save ~4.7 GiB/slot), which is close to
# "no restore points" — plausibly why every turn re-prefills. Whether that trade is right is an
# empirical question about THIS box, and without these two flags answering it costs a catalog
# edit, a release and an Ops → Update, i.e. it never gets answered.
#
# `--ctx-checkpoints` carries a WORSE failure mode than the rest of this list, so raise it in
# small steps. A checkpoint on a hybrid is a full copy of the recurrent state (~150 MiB for
# Qwen3.8) and is device-resident, so a large value costs GB per slot — and `footprint_gb` does
# NOT model checkpoint memory, so the residency budget will not see it coming. On a box that has
# hard-locked under memory pressure the risk is not "the model fails to load" (the recoverable
# failure the flags above assume) but the host going down. Clearing is still the same call with
# no args, which does not require the model to be loadable.
#   -ngl               how many layers are offloaded to the iGPU
#   -fa                flash attention on/off/auto
#   --reasoning-format how llama.cpp splits a thinking trace out of `content`
# The first two are the "is it the GPU?" bisect. When a model emits garbage or dies on this
# gfx1151 — the exact failure class behind our `-ub 1024` (llama.cpp #27237) — the first move is
# "does it still happen with fewer layers offloaded, or with flash attention off?", and that move
# was unavailable. Neither can make a model unloadable: a wrong value costs speed or a CPU
# fallback. `--reasoning-format` is the remedy for the OTHER common breakage after a llama.cpp
# rebuild on master — `<think>` tags leaking into `content`, or an empty reasoning channel —
# which is a one-string fix (`deepseek` vs `auto`) that otherwise costs a release.
#
# `--no-mmap` is deliberately NOT here and cannot be: llama.cpp has no positive `--mmap`
# counterpart, so an allowlist entry could not undo the flag we already pass. An entry would be a
# silent no-op, which is worse than an absent one. Same for `--jinja`, which is unconditional and
# would need a `--chat-template-file` (a file on the box) to be worth overriding.
#   -lv                     llama-server log verbosity
#   --checkpoint-min-step   minimum token spacing between context checkpoints
# These are what the NEXT prefill investigation needs, and neither was reachable.
#
# `--cache-ram` USED to be here and deliberately is not any more. The gateway now serves
# `-cram 0` and `local_catalog.CACHE_RAM_GB` is 0.0 to match, so an operator turning the cache
# back on from the PWA would serve up to 32 GiB of host RAM that the residency budget believes
# does not exist — under-reserving on the one path this box has hard-locked on. The flag and
# the budget term are one decision; the allowlist cannot be a second, unbudgeted way to move it.
#
# `-lv 4` (trace) is the decisive diagnostic and there is no substitute: whether checkpoints are
# being created and MATCHED is only visible in llama-server's own `created context checkpoint` /
# `restored context checkpoint` / `forcing full prompt re-processing due to lack of cache data`
# lines, which are TRC-level. Without it, a checkpoint sweep cannot distinguish "the count is
# wrong" from "nothing is ever restored" — the two produce identical timings, which is exactly
# how a 2-vs-8 sweep here measured nothing and was misread as the flag being inert.
#
# `--checkpoint-min-step` defaults to 8192, so across a ~24k prompt only ~3 checkpoints are
# permitted by SPACING no matter how high the count goes — tuning the count alone can be
# pointless.
#
# Flags taking a value are allowed to carry one; the value itself is NOT interpreted here.
EXTRA_ARG_FLAGS: frozenset[str] = frozenset(
    {
        "--swa-full",
        "-b",
        "-ub",
        "--spec-type",
        "--spec-draft-n-max",
        "--spec-draft-n-min",
        "--spec-draft-p-min",
        "--image-min-tokens",
        "--image-max-tokens",
        "--ctx-checkpoints",
        "--cache-reuse",
        "-ngl",
        "-fa",
        "--reasoning-format",
        "-lv",
        "--checkpoint-min-step",
        # KV cache quantisation. `-fa` is already served unconditionally (the gfx1151
        # stability/perf flag), which is llama.cpp's prerequisite for a quantised cache, so
        # this is reachable on this box today.
        #
        # It is the largest lever on demand that exists here: MEASURED 2026-08-21, the served
        # KV is 8.0 GiB per 128k on a Qwen3.8 27B at f16 — bigger than half the weights — and
        # q8_0 halves the cache proper (MODEL_PROMPTING.md derives 4.25). On a box whose
        # roster runs windows up to 262144 that is tens of GB of admission pressure.
        #
        # Allowlisted rather than simply switched on because the quality cost is empirical:
        # q8_0 is near-lossless in the literature and q4_0 is not, and neither claim has been
        # tested against THIS box's models. Exactly the case this list exists for — try it,
        # measure it, revert it, without a catalog edit and a release per iteration.
        "-ctk",
        "-ctv",
        "--cache-type-k",
        "--cache-type-v",
        # llama.cpp's replacement for the deprecated `--mmap`/`--no-mmap`/`--mlock` family:
        # auto | none | mmap | mlock | mmap+mlock | dio. Allowlisted so the ONE unexamined
        # decision behind this box's memory behaviour can be measured instead of asserted.
        #
        # We hardcode `--no-mmap`, justified in the code and the runbook by the phrase "a
        # gfx1151 stability flag" and nothing else — no measurement, no issue, no date, and it
        # appears to be inherited from the strix-halo-toolbox recipe, the same source as the
        # kernel settings already found wrong for this box. It is also the direct cause of the
        # weights being resident TWICE (GTT plus the page cache the read fills), which is the
        # 2026-08-19 freeze mechanism and the transient that took the box to 115/121 GB during
        # one gpt-oss load.
        #
        # llama.cpp's own default is `auto` — "mmap, unless a device does not support it" — so
        # the engine already handles the case our flag was presumably added for. `dio` is the
        # other candidate: DirectIO bypasses the page cache entirely, which would remove the
        # second copy at its source rather than reclaiming it afterwards.
        #
        # Allowlisted rather than changed: the flag may have been added for a real crash nobody
        # wrote down, and finding out the hard way costs a power cycle on a box with no terminal.
        # Setting it here supersedes the hardcoded `--no-mmap` (llama_swap_config._SUPERSEDES)
        # so exactly one of the two reaches the command line.
        "--load-mode",
        "-lm",
        # Tensor placement (`<regex>=<buffer type>`), value-taking. Flash-Next pins its 26.8 GiB
        # engram tensor to CPU this way (it exceeds Vulkan's 4 GiB binding limit), and F2 tunes
        # placement through this route with no release (FLASH_NEXT_ENGINE_PLAN §5). An operator
        # value REPLACES the catalog's rule (llama_swap_config._SUPERSEDES), so it must restate
        # `per_layer_token_embd=CPU`; a placement that puts the table on the GPU fails to load,
        # which is recoverable — clearing does not need the model to be loadable.
        "-ot",
        "--override-tensor",
    }
)


# The one flag on the list whose bad value is not self-limiting. Everything else fails by not
# loading — recoverable, because clearing does not need the model to be loadable. A checkpoint on
# a hybrid is a full copy of the recurrent state (MEASURED 275-284 MiB for Qwen3.8) and it is per
# slot, while `footprint_gb` budgets it only at the SERVED count — so everything above that is
# unbudgeted and the residency evictor cannot see it.
#
# It is HOST RAM, not device memory, which is what an earlier version of this comment got wrong
# and used to justify a tighter ceiling: `common_prompt_checkpoint` holds `std::vector<uint8_t>`
# buffers, and raising the served count from 2 to 16 on the box left GTT unchanged at 26.21 GiB.
# On unified memory it still comes out of the same pool, so the bound stays — but it does not add
# to the GTT-cap pressure that is this box's documented hang mode.
_EXTRA_ARG_BOUNDS: dict[str, tuple[int, int]] = {
    # 32 is llama.cpp's OWN default; we serve 2. An earlier bound of 0..8 put the upstream
    # default out of reach — i.e. the one value most worth trying could not be tried, which is
    # precisely the gap the allowlist exists to close. The ceiling is now that default, so a
    # sweep can reach it and no further: on a hybrid each checkpoint is a full recurrent-state
    # copy (~150 MiB for Qwen3.8, upstream #27211 measures 149.6), device-resident and per slot,
    # so 32 is ~4.7 GiB/slot that `footprint_gb` budgets only at the SERVED count.
    "--ctx-checkpoints": (0, 32),
    # `_vision_resident_gb` sizes the CLIP workspace at a hardcoded 4096 image tokens
    # (llama.cpp's ceiling for this projector family). Raising the ceiling past that grows the
    # workspace the budget does not follow, so cap it at the figure the budget assumes.
    "--image-max-tokens": (1, 4096),
    # Verbosity is a log-volume knob, not a memory one; 4 is TRC, llama.cpp's most verbose.
    # Bounded so a typo cannot ask for a level that does not exist.
    "-lv": (0, 4),
    # Token spacing between checkpoints. 0 lets llama.cpp choose; the floor of 64 upstream
    # rejects anything denser, and a very large value silently disables checkpointing.
    "--checkpoint-min-step": (0, 131072),
}

# What each allowlisted flag's VALUE may look like. The value lands verbatim in the
# space-joined llama-server command inside llama-swap's YAML, so a value carrying whitespace
# smuggles extra flags past the allowlist (`-ot "x=CPU --rpc host:port"`), and one carrying a
# newline writes a second model entry with an arbitrary `cmd:` — command execution on the box.
# A value must therefore match its flag's own shape, not merely "not start with `-`".
_INT = re.compile(r"^[0-9]{1,9}$")
_FLOAT = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,6})?$")
_WORD = re.compile(r"^[A-Za-z0-9_+.-]{1,32}$")
# `-ot`: one or more comma-separated `<tensor-name regex>=<buffer type>` rules. The regex half
# allows only the characters tensor-name patterns use; no whitespace, quotes, `:` or `#`.
_TENSOR_RULE = r"[A-Za-z0-9_.\\|*+?()\[\]-]+=[A-Za-z0-9_]+"
_OVERRIDE_TENSOR = re.compile(rf"^{_TENSOR_RULE}(,{_TENSOR_RULE})*$")
_EXTRA_ARG_VALUE: dict[str, re.Pattern[str]] = {
    "-b": _INT,
    "-ub": _INT,
    "--spec-type": _WORD,
    "--spec-draft-n-max": _INT,
    "--spec-draft-n-min": _INT,
    "--spec-draft-p-min": _FLOAT,
    "--image-min-tokens": _INT,
    "--image-max-tokens": _INT,
    "--ctx-checkpoints": _INT,
    "--cache-reuse": _INT,
    "-ngl": re.compile(r"^([0-9]{1,4}|auto|all)$"),
    "-fa": _WORD,
    "--reasoning-format": _WORD,
    "-lv": _INT,
    "--checkpoint-min-step": _INT,
    "-ctk": _WORD,
    "-ctv": _WORD,
    "--cache-type-k": _WORD,
    "--cache-type-v": _WORD,
    "--load-mode": _WORD,
    "-lm": _WORD,
    "-ot": _OVERRIDE_TENSOR,
    "--override-tensor": _OVERRIDE_TENSOR,
}

# Values of `-fa` that turn flash attention OFF. Not a style question: with `-fa` on, the CLIP
# attention workspace is LINEAR in patches (~0.47 GiB at the 4096-token ceiling, measured); with
# it off llama.cpp materialises the full [n_patches, n_patches] matrix and the same encode
# reaches ~16 GiB. `_vision_resident_gb` hardcodes the flash-attention branch, so the residency
# budget would under-reserve by ~15.5 GiB — and the allocation lands on the first
# full-resolution image, LONG after the load guard passed and after the watchdog stopped
# watching. That is the unrecoverable host hang, arriving through a flag whose whole purpose is
# diagnosing a different problem.
_FLASH_ATTENTION_OFF = {"0", "off", "false", "no", "disabled"}


class LaunchFlagError(ValueError):
    """An operator launch flag, or its value, that is not allowed."""


def validate(args: list[str] | tuple[str, ...]) -> list[str]:
    """`args` with blanks dropped, or LaunchFlagError on anything not on EXTRA_ARG_FLAGS. A value
    is accepted positionally (a token following a flag that takes one) and must match its
    flag's shape, so `--cache-reuse 256` passes while a bare `256` or an unknown `--foo` is
    refused. `_EXTRA_ARG_BOUNDS` additionally range-checks the flags whose bad value takes the
    host down rather than just failing to load."""
    cleaned: list[str] = []
    expect_value = False
    flag = ""
    for raw in args:
        token = raw.strip()
        if not token:
            continue
        if expect_value and not token.startswith("-"):
            if not _EXTRA_ARG_VALUE[flag].fullmatch(raw):
                raise LaunchFlagError(f"{flag} does not take the value {raw!r}")
            bounds = _EXTRA_ARG_BOUNDS.get(flag)
            if bounds is not None:
                low, high = bounds
                try:
                    value = int(token)
                except ValueError:
                    raise LaunchFlagError(f"{flag} takes an integer, got {token!r}") from None
                if not low <= value <= high:
                    raise LaunchFlagError(
                        f"{flag} must be {low}..{high} — a larger value costs device memory "
                        "the residency budget does not model, and can hang the box"
                    )
            cleaned.append(token)
            expect_value = False
            continue
        if token not in EXTRA_ARG_FLAGS:
            raise LaunchFlagError(
                f"flag {token!r} is not settable; allowed: {sorted(EXTRA_ARG_FLAGS)}"
            )
        cleaned.append(token)
        flag = token
        expect_value = token != "--swa-full"  # the only boolean flag on the list
    return cleaned
