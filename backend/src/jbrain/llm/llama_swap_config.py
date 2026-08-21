"""Generate the llama-swap gateway config (llama-swap.yaml).

The single source of truth for the gateway's per-model `llama-server` command —
crucially the `-c` context window, which must equal what the router reports to the
PWA's context meter (jbrain.llm.router.context_window). Two callers share it so the
two never drift:

  - scripts/local-llm-setup.sh + the deploy re-stamp (deploy/local-models-sync.sh),
    via `python -m jbrain.llm.llama_swap_config <models_dir>` reading the MANIFEST env.
    Its `_main` also loads the operator's SAVED per-model window/slot overrides from the
    settings store (`_saved_overrides`) and applies them — so an update never resets a raised
    `-c` back to the catalog default (the bug that let a 128k-configured model overflow at 32k);
  - the settings API, at runtime, to re-stamp a model's `-c` after the operator
    edits its context window — written atomically into the mounted models dir,
    which the gateway (run with `--watch-config`) hot-reloads. A `-c` change takes
    effect on the model's next (re)load, so the API unloads a resident model after
    rewriting so its next request reloads at the new window.

Both non-test callers now pass the saved overrides (the settings API directly, the deploy CLI
via `_saved_overrides`), so the served `-c` matches the meter without depending on the boot
reconcile (`api.llm_settings.reconcile_gateway_windows_on_boot`), which stays as a backstop.

Every model joins a single non-swapping group (`swap: false`), so llama-swap never
evicts one to load another — the app (jbrain.llm.residency) is the sole evictor,
freeing the fewest models to hold a free-RAM floor before each load. `exclusive:
false` lets an on-demand request still load a member.

Each manifest entry is a catalog dict (jbrain.llm.local_catalog.LocalModel
asdict): id, served_model, gguf_include, mmproj_include, context_window,
recommended. `windows` overrides a model's `-c` by catalog id.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import pathlib
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import yaml

from jbrain.llm import local_catalog

# Concrete, distinct upstream ports — llama-swap's ${PORT} macro isn't substituted
# by every build, and the non-swapping group runs models concurrently so they can't
# share a port. 127.0.0.1: llama-swap and llama-server share the container.
UPSTREAM_PORT_BASE = 9100


def resolve_weight(root: str, model_id: str, pattern: str) -> str:
    """The weight path (RELATIVE to the model dir) for a model's glob — the first
    shard for a multi-part GGUF (llama.cpp follows it to the rest). Raises if
    nothing matches or a shard set is incomplete, so a partial download fails here,
    not cryptically at gateway load.

    Searches recursively: some repos (Unsloth's UD-Q* quants) nest the shards in a
    quant subdirectory, so `hf download` saves them under `<id>/<quant>/`. The
    return value is relative to `<root>/<id>` (a bare filename at the top level, or
    `<quant>/<file>` when nested) so the gateway's `-m /models/<id>/<rel>` resolves
    either way. hf's `.cache/` download-staging dir is skipped."""
    base = os.path.join(root, model_id)
    matches = sorted(
        m
        for m in glob.glob(os.path.join(base, "**", pattern), recursive=True)
        if ".cache" not in os.path.relpath(m, base).split(os.sep)
    )
    if not matches:
        raise FileNotFoundError(
            f"no file matching {pattern!r} for {model_id} under {root} — download incomplete?"
        )
    rels = [os.path.relpath(m, base) for m in matches]
    shards = [r for r in rels if "-00001-of-" in os.path.basename(r)]
    if shards:
        first = shards[0]
        total = int(os.path.basename(first).split("-of-")[1].split(".gguf")[0])
        if len(matches) != total:
            raise FileNotFoundError(
                f"{model_id}: expected {total} shards for {pattern!r}, found {len(matches)}"
            )
        return first
    return rels[0]


def unresolved_ids(root: str, models: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """Catalog ids whose REQUIRED weight files don't all resolve under `root`, in manifest
    order — the set a provisioning run still has to download.

    Why the provisioning scripts must not use a bare `*.gguf` presence check instead: a model's
    required file set is a property of the CATALOG, and it changes between releases. When an
    entry gains a file it didn't need before — a vision projector added to a variant that was
    text-only — a directory holding the previous release's weights still matches `*.gguf` and
    reads as complete, so the download is skipped. `render` then resolves every glob for the
    WHOLE roster in one pass and raises on the one that's missing, which takes every other
    model's config down with it: the deploy re-stamp aborts, and the boot reconcile
    (`api.llm_settings.reconcile_gateway_config`, which swallows render failures so a bad glob
    can never block startup) silently applies nothing. Resolving exactly the globs `render`
    will is the only check that cannot drift from what the config generator demands."""
    missing: list[str] = []
    for m in models:
        model_id = str(m["id"])
        globs = [str(m["gguf_include"])]
        if m.get("mmproj_include"):
            globs.append(str(m["mmproj_include"]))
        for pattern in globs:
            try:
                resolve_weight(root, model_id, pattern)
            except FileNotFoundError:
                missing.append(model_id)
                break
    return tuple(missing)


# Flags an operator flag SUPERSEDES rather than duplicates. `--load-mode` is llama.cpp's
# replacement for the deprecated `--mmap`/`--no-mmap`/`--mlock` family, and it warns when both
# appear ("only the last flag on the command line will take effect"). Resting on argv order is
# exactly what #1152 refused to do for `--image-min-tokens`, so setting the new flag removes the
# old ones instead. Same reasoning generalised: a command line carrying two flags that contradict
# each other is unreadable as a record of what is actually served.
_SUPERSEDES: dict[str, tuple[str, ...]] = {
    "-lm": ("--mmap", "--no-mmap", "--mlock"),
    "--load-mode": ("--mmap", "--no-mmap", "--mlock"),
}


def _drop_operator_overridden(args: Sequence[str], operator_args: Sequence[str]) -> list[str]:
    """Strip from a base command every flag the operator has also set (and its value), so the
    operator's copy appended afterwards is the ONLY occurrence.

    The invariant is #1152's, generalised: a command line carrying the same flag twice is
    unreadable as a record of what is actually served, on a box whose only window into the
    engine is that string. #1152 established that for `--image-min-tokens` alone, by making it
    a field. But `-ub` and `-cram` are BOTH hardcoded in the shared command and on
    `EXTRA_ARG_FLAGS`, so overriding either already emitted it twice and left the result
    resting on llama.cpp taking the last one — true today, undocumented, and exactly what
    #1152 refused to rely on. Doing it here instead of per-flag means the next flag added to
    the allowlist inherits the guarantee rather than the bug.

    A value is a following token that does not start with `-`, matching how the settings API
    validates these args (every allowlisted flag takes one except the boolean `--swa-full`)."""
    overridden = {a for a in operator_args if a.startswith("-")}
    for flag in tuple(overridden):
        overridden.update(_SUPERSEDES.get(flag, ()))
    out: list[str] = []
    skip_value = False
    for token in args:
        if token in overridden:
            skip_value = True
            continue
        if skip_value:
            # Consume the dropped flag's value, but never swallow the next flag: a boolean
            # like `--swa-full` has no value to eat.
            skip_value = False
            if not token.startswith("-"):
                continue
        out.append(token)
    return out


_SPEC_FLAGS = ("--spec-type", "--spec-draft-n-max", "--spec-draft-n-min", "--spec-draft-p-min")


def _drop_speculative(args: Sequence[str]) -> list[str]:
    """Remove every speculative-decoding flag (and its value) from a command.

    Used when an operator asks for a second slot: the two cannot coexist, so the flags come out
    rather than the request being ignored. Dropping the whole family — not just `--spec-type` —
    because llama-server rejects a `--spec-draft-*` tuning flag with no mode to tune."""
    out: list[str] = []
    skip = False
    for token in args:
        if token in _SPEC_FLAGS:
            skip = True
            continue
        if skip:
            skip = False
            if not token.startswith("-"):
                continue
        out.append(token)
    return out


def _is_speculative(extra_server_args: Sequence[str]) -> bool:
    """Whether a model's serving flags turn on speculative decoding (`--spec-type <mode>`),
    which constrains it to a single sequence. Read off the flags rather than a catalog boolean
    so the constraint can't drift from the thing that causes it."""
    return any(a == "--spec-type" for a in extra_server_args)


def render(
    models: Sequence[Mapping[str, object]],
    root: str,
    *,
    windows: Mapping[str, int] | None = None,
    slots: Mapping[str, int] | None = None,
    extra_args: Mapping[str, Sequence[str]] | None = None,
    image_min_tokens: Mapping[str, int] | None = None,
) -> str:
    """The full llama-swap.yaml text for `models` (catalog manifest dicts). `root`
    is the host path to the weights (globbed to resolve filenames); `windows` maps
    catalog id → context-window override (absent = the model's catalog default);
    `slots` maps catalog id → llama-server `-np` parallel slot count (absent/1 = a
    single slot, today's behaviour). A model with 2 slots serves its `window` on EACH
    slot — so `-c` is set to `window * slots` (llama-server divides `-c` evenly across
    `-np`) to keep the per-slot window at the value the router reports to the meter.
    The second slot is the interactive keep-warm slot: the jerv prefix lives there,
    isolated from background/title traffic on the other slot (docs/runbooks/STRIX_HALO_SETUP.md).
    `extra_args` maps catalog id → EXTRA llama-server flags, appended after the model's
    static catalog `extra_server_args`. It is the operator's remote escape hatch for trying a
    launch flag on a live box with no terminal (CLAUDE.md #10); the API allowlists which flags
    may be set, because a bad one stops llama-server booting.
    Every model joins one `swap: false` group so the gateway never auto-evicts — the
    app is the sole evictor (jbrain.llm.residency)."""
    windows = windows or {}
    slots = slots or {}
    extra_args = extra_args or {}
    image_min_tokens = image_min_tokens or {}
    lines = ["# Generated by jbrain.llm.llama_swap_config — do not edit by hand.", "models:"]
    for i, m in enumerate(models):
        port = UPSTREAM_PORT_BASE + i
        model_id = str(m["id"])
        gguf = resolve_weight(root, model_id, str(m["gguf_include"]))
        window = windows.get(model_id, int(cast(int, m["context_window"])))
        catalog_args = tuple(
            str(a) for a in cast("Sequence[str]", m.get("extra_server_args") or ())
        )
        # The floor is a catalog FIELD, so an operator override simply wins — there is no flag
        # to rewrite and the command line carries exactly one --image-min-tokens.
        floor = image_min_tokens.get(model_id, cast("int | None", m.get("image_min_tokens")))
        operator_args = tuple(str(a) for a in extra_args.get(model_id, ()))
        n_slots = max(1, slots.get(model_id, 1))
        # Either source can turn speculation on: the catalog's static flags, or an operator
        # trying `--spec-type` remotely via the extra-args route. Both must pin the model to
        # one slot, so the test reads the flags actually going on the command line.
        # Speculation and parallel slots are MUTUALLY EXCLUSIVE: llama.cpp's speculative paths
        # serve ONE sequence — MTP takes no second parallel slot, and draft acceptance collapses
        # as concurrent sequences rise (reported on this exact gfx1151 SoC).
        #
        # This used to resolve the conflict by pinning n_slots to 1 and ignoring the operator.
        # It now resolves the other way: an explicit request for a second slot DROPS speculation.
        # Silently ignoring the request was the worse failure — a second slot is the only thing
        # that keeps a background task following the interactive model (`research.title`) out of
        # the slot holding a 32k primed prefix, and losing that prefix costs a ~100 s cold
        # prefill. The chat auto-titler used to be the loudest case; it is gone (jerv names its
        # own chat in-turn via `name_session`), but the class of task remains.
        # Whichever the operator picks, they get the one they asked for.
        speculative = _is_speculative(catalog_args + operator_args)
        if speculative and n_slots > 1:
            catalog_args = tuple(_drop_speculative(catalog_args))
            operator_args = tuple(_drop_speculative(operator_args))
            speculative = False
        cmd = [
            "llama-server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            # Total KV cells: llama-server splits `-c` evenly across `-np` slots, so
            # `window * n_slots` keeps each slot at `window` — the value the router reports
            # to the meter. n_slots==1 leaves this exactly `window` (unchanged behaviour).
            "-c",
            str(window * n_slots),
            # Tool calling needs the model's own chat template: --jinja makes
            # llama-server render the embedded (Hermes-style for Qwen) tool-use
            # template and parse `<tool_call>` blocks back into structured
            # `tool_calls`. Without it the OpenAI `tools` we send have no grammar
            # behind them and the model free-forms text (a narrated, never-called
            # image) — so the on-box image tools never fire.
            "--jinja",
            # gfx1151 stability/perf flags: flash attention + --no-mmap; -ngl 999
            # offloads every layer to the iGPU.
            "-fa",
            "1",
            "--no-mmap",
            # Prompt cache OFF. llama.cpp defaults `--cache-ram` to 8192 MiB, so every resident
            # model silently reserves 8 GiB of HOST memory for conversation state that survives
            # slot eviction — and on Strix Halo host and device draw on one physical pool, so
            # that is 8 GiB out of the same budget the weights come from.
            #
            # The goal it loses to is CO-RESIDENCY: gpt-oss-120b + a Qwen3.8 27B held together
            # with no swapping. MEASURED 2026-08-21, at the served windows, against a 124.0 GB
            # GTT cap with the guard holding 6 GB back:
            #
            #   with the default   109.3 GB resident   ->   8.7 GB headroom
            #   with `-cram 0`      93.3 GB resident   ->  24.7 GB headroom
            #
            # 8.7 GB is one wrong estimate away from the freeze this box has taken three times.
            # 16 GB is the whole difference between "marginal" and "comfortable", and it buys
            # the pair without shortening either model's context.
            #
            # The cost is real and accepted: an evicted conversation now re-prefills instead of
            # being restored from host RAM. Co-residency is what pays for it — a model that is
            # never swapped out is a model whose slots are not being fought over in the first
            # place.
            #
            # `local_catalog.CACHE_RAM_GB` MOVES WITH THIS. Serving `-cram 0` while budgeting
            # 8 GiB over-reserves every model and evicts pairs that fit; the inverse
            # under-reserves on the box's freeze path.
            "-cram",
            "0",
            # Prompt-prefix KV reuse (docs/archive/LLM_PROMPT_CACHE_PLAN.md W2): keep the KV of a
            # matching
            # leading prefix and salvage it via KV-shifting even after a later divergence. 256 is
            # the min
            # chunk worth reusing. Pairs with W1's cache-stable message layout.
            #
            # REAL on an attention model (gpt-oss). On a HYBRID it is worse than useless: you cannot
            # KV-shift a recurrent state, and the partial-range `seq_rm` this path calls returns
            # false for
            # recurrent memory, which reaches GGML_ABORT — the server dies. We have not hit it
            # because on
            # an identical prompt the reuse loop never executes (n_past already equals the prompt
            # length),
            # so it is a latent crash rather than a live one. It should be dropped for hybrid
            # entries;
            # that needs a per-model field and is not done here.
            # OBSERVABILITY, and it is not optional here. llama-server exposes whether a model
            # is actually speculating in exactly two places: `/slots[].speculative` (a real
            # bool from `can_speculate()`) and the `/metrics` spec-decode counters. Neither
            # endpoint exists unless these flags are passed. Without them the only readable
            # signal is `/props`'s `speculative.types`, which is a DEAD FIELD — the server
            # builds it from a fresh task_params it never populates, so it reads "none" on
            # every build whether or not speculation is running. This box spent a whole
            # investigation concluding MTP was off from that field. Never again: if a serving
            # mode can't be observed, it can't be tuned, and it will be misdiagnosed instead.
            "--slots",
            "--metrics",
            # Physical batch. NOT llama-server's default of 512: llama.cpp #27237 reports the
            # qwen35 hybrid (Gated DeltaNet) emitting GARBAGE OUTPUT at ubatch 512 on Vulkan
            # while 1024 and 4096 are clean — and this deployment serves exactly that arch on
            # exactly that backend. That is the WHOLE justification. An earlier version of this
            # comment also credited 1024 with trimming the big-vocab graph reserve (#23527) by
            # ~3 GiB, which was wrong twice over: on an unfixed build a LARGER ubatch grows that
            # buffer rather than shrinking it, and the issue is fixed anyway (PR #24086 clamps
            # the reserve to `n_outputs_max`), so ubatch does not touch it on any build we run.
            # At Qwen3.8's 248k vocab the reserve now costs ~11 MiB, not gigabytes.
            "-ub",
            "1024",
            # Per-slot context checkpoints, down from llama-server's default of 32. Each is a full
            # copy
            # of the recurrent state on a HYBRID model — ~150 MiB for Qwen3.8 (upstream #27211
            # measures
            # 149.6 MiB for this exact arch), device-resident, so 32 is ~4.7 GiB per slot.
            #
            # Checkpoints are the prefix-reuse mechanism, and on a hybrid they are the ONLY one: a
            # recurrent model's pos_min is always ~the end of the sequence, so llama.cpp takes the
            # checkpoint branch every request and, finding no match, logs `forcing full prompt
            # re-processing due to lack of cache data` and reprocesses from zero (discussion #19264,
            # closed 'It's already implemented'; PR #20288).
            #
            # MEASURED on this box 2026-08-18, and it is not what an earlier version of this comment
            # predicted: with a checkpoint hit a warm prime is 0.99 s reusing 32,485 of 32,489
            # tokens.
            # Caching WORKS here. Sweeping this value 2 -> 8 moved a cold prime by 0.2% (101.26 ->
            # 101.08 s)
            # — which measures nothing, because if no checkpoint MATCHES the count is irrelevant. Do
            # not
            # read that as evidence the flag is inert.
            #
            # Left at 2 pending a trace-level diagnosis (`-lv 4`: `created context checkpoint` /
            # `restored context checkpoint`) that shows whether checkpoints are being created and
            # matched
            # at all. Raising the count without that only spends memory. It is on the extra-args
            # allowlist
            # so the question is answerable without a release (docs/runbooks/STRIX_HALO_SETUP.md).
            "--ctx-checkpoints",
            str(local_catalog.ctx_checkpoints(cast("float | None", m.get("checkpoint_gb")))),
            # Without this llama.cpp uses 8192, which makes the count above nearly useless: the
            # checkpoints bunch at the end of the conversation and none covers an earlier
            # divergence. The two are one setting (local_catalog.CHECKPOINT_MIN_STEP).
            "--checkpoint-min-step",
            str(local_catalog.CHECKPOINT_MIN_STEP),
            "-m",
            f"/models/{model_id}/{gguf}",
            "-ngl",
            "999",
        ]
        # ALWAYS explicit, even at 1. llama-server's `-np` default is `auto`, which current
        # builds resolve to a multi-slot value — so omitting the flag does NOT mean one slot,
        # and a single-slot serving mode would be silently violated. Above 1 this is the
        # dedicated interactive slot beside the background one: llama-server routes each
        # request to the slot with the longest matching prefix, so jerv turns keep their primed
        # KV in one slot while title/background traffic uses the other — neither can evict the
        # other's cache (docs/runbooks/STRIX_HALO_SETUP.md).
        cmd += ["-np", str(n_slots)]
        # Prompt-prefix KV reuse — for an ATTENTION model only. On a recurrent/hybrid stack you
        # cannot KV-shift the state, and the partial-range `seq_rm` this path calls returns false
        # for recurrent memory, which reaches GGML_ABORT: the server dies. It has never fired
        # here only because on an identical prompt the reuse loop never executes (n_past already
        # equals the prompt length) — a latent crash, not a no-op. Prefix reuse on these models
        # is mediated entirely by context checkpoints instead (see --ctx-checkpoints).
        if not m.get("recurrent"):
            cmd += ["--cache-reuse", "256"]
        # A thinking model emits its reasoning inline as `<think>…</think>`;
        # `--reasoning-format deepseek` (paired with --jinja above) makes llama.cpp parse
        # those tags out of `content` into the `reasoning_content` channel OpenAI-compatible
        # clients read as the reasoning trace. Empty (the common case) leaves llama.cpp's `auto`.
        reasoning_format = str(m.get("reasoning_format") or "")
        if reasoning_format:
            cmd += ["--reasoning-format", reasoning_format]
        mmproj = m.get("mmproj_include")
        if mmproj:
            cmd += ["--mmproj", f"/models/{model_id}/{resolve_weight(root, model_id, str(mmproj))}"]
        # Full history on the sliding-window layers. Without it a slot restore on an SWA model
        # succeeds and is then thrown away (see LocalModel.kv_full_history).
        if m.get("kv_full_history"):
            cmd.append("--swa-full")
        # Model-specific serving flags (e.g. the MTP variant's `--spec-type draft-mtp …`
        # self-speculative-decoding config), appended verbatim after the shared flags.
        # Emitted from the field so exactly one occurrence reaches the command line, whether
        # the value came from the catalog or from an operator override.
        if floor is not None:
            cmd += ["--image-min-tokens", str(floor)]
        cmd += catalog_args
        # Operator overrides last, so they append to (never reorder) the catalog's own flags —
        # and anything they override is stripped from what came before, so each flag appears
        # exactly once whether its value came from this module, the catalog, or the operator.
        cmd = _drop_operator_overridden(cmd, operator_args)
        cmd += operator_args
        lines.append(f"  {m['served_model']}:")
        lines.append(f"    proxy: http://127.0.0.1:{port}")
        lines.append("    cmd: >")
        lines.append("      " + " ".join(cmd))

    # One non-swapping group with EVERY model as a member: `swap: false` means llama-swap
    # never evicts a member to load another, and `exclusive: false` lets an on-demand request
    # still load one. So the gateway never auto-evicts — the app (jbrain.llm.residency) is the
    # sole evictor, freeing the fewest models to hold the free-RAM floor before each load. This
    # replaced the old all-or-nothing pin (recommended pair ≈ 91 GB, no headroom) that
    # hard-locked the host; the app's budget is what keeps it safe.
    resident = [str(m["served_model"]) for m in models]
    if resident:
        lines.append("groups:")
        lines.append("  resident:")
        lines.append("    swap: false")
        lines.append("    exclusive: false")
        lines.append("    members:")
        lines += [f"      - {name}" for name in resident]

    return "\n".join(lines) + "\n"


def write(
    root: str,
    models: Sequence[Mapping[str, object]],
    *,
    windows: Mapping[str, int] | None = None,
    slots: Mapping[str, int] | None = None,
    extra_args: Mapping[str, Sequence[str]] | None = None,
    image_min_tokens: Mapping[str, int] | None = None,
) -> str:
    """Render and atomically write {root}/llama-swap.yaml (temp + rename so the
    gateway's --watch-config never sees a half-written file). Returns the path.

    NO-OPS WHEN THE RENDERED CONFIG IS UNCHANGED, and that is the whole point of the
    comparison rather than a micro-optimisation: rewriting this file KILLS EVERY RESIDENT
    MODEL.

    DIAGNOSED on the box 2026-08-20, after the owner reported — repeatedly, and was
    repeatedly told it was a display artifact — that staging a model in the PWA unloaded
    gpt-oss-120b. The chain:

      a settings PUT (context window / image floor / slots / extra args)
        -> `api.llm_settings._try_regenerate` calls this
        -> `os.replace` lands a file with a fresh mtime, even byte-identical
        -> llama-swap's `--watch-config` poller compares MTIME + SIZE, so it fires
        -> llama-swap `reload()` builds a new server and calls `old.Shutdown()`
        -> every running llama-server process dies

    Two things made it invisible for so long. The unload happens inside llama-swap, so no
    `box_events` row is written and the vitals surface stays silent — the app genuinely does
    not know it happened. And `_unload_if_loaded`, right beside the regen call, unloads only
    the model named in the PUT, which makes the code read as if a settings edit touches one
    model. It touches all of them. Confirmed in llama-swap's own log, where `reloading
    configuration` sits between every one of three consecutive manual loads of gpt-oss.

    A PUT that genuinely changes a served command still writes, still reloads, and still
    costs the resident set — correctly, since the model must relaunch to pick it up."""
    text = render(
        models,
        root,
        windows=windows,
        slots=slots,
        extra_args=extra_args,
        image_min_tokens=image_min_tokens,
    )
    path = os.path.join(root, "llama-swap.yaml")
    # Compare CONTENT, not mtime: the caller re-stamps on every settings PUT and the common
    # case is that nothing about the served commands changed. A read failure (absent file,
    # first boot) falls through to the write, which is the safe direction.
    with contextlib.suppress(OSError):
        if pathlib.Path(path).read_text() == text:
            return path
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def served_shape_from_config(root: str) -> dict[str, tuple[int, int]]:
    """The `(window, slots)` each model is ACTUALLY served at, read back out of the
    llama-swap.yaml we already wrote. Keyed by served model name.

    This exists so a caller with no database can still size a load correctly. The
    smoketest is the one that matters: it runs under `docker compose run --rm --no-deps`
    during an update, so it cannot read the operator's `-c` override out of the settings
    table, and it therefore projected the CATALOG window while llama-swap served the
    operator's. On 2026-08-19 that made a routine load look like a runaway:

        window used to project   projected   ceiling   observed   verdict
        32768  (catalog)              1.57      3.57       3.78   ABORT
        262144 (actually served)      2.45      4.45       3.78   pass

    Same model, same build, same memory — and the abort rolled llama.cpp back to its
    pinned base. The config file is the right source precisely because it is what
    llama-swap executes; it needs no DB, and it cannot disagree with the served command
    the way a re-derivation from the catalog can.

    `-c` is total KV cells across all slots (llama-server divides it evenly by `-np`), so
    the per-slot window this returns is `-c // -np`, matching what the router reports to
    the meter. Best-effort: an unreadable or unparseable config returns an empty map and
    the caller falls back to catalog defaults, which is the prior behaviour."""
    shapes: dict[str, tuple[int, int]] = {}
    path = os.path.join(root, "llama-swap.yaml")
    try:
        with open(path) as handle:
            parsed = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return shapes
    if not isinstance(parsed, dict):
        return shapes
    for name, spec in (parsed.get("models") or {}).items():
        cmd = spec.get("cmd") if isinstance(spec, dict) else None
        if isinstance(cmd, str):
            cmd = cmd.split()
        if not isinstance(cmd, list):
            continue
        flags = [str(token) for token in cmd]

        def _flag(flag: str, tokens: list[str] = flags) -> int | None:
            try:
                return int(tokens[tokens.index(flag) + 1])
            except (ValueError, IndexError):
                return None

        cells = _flag("-c")
        if cells is None or cells <= 0:
            continue
        slots = _flag("-np") or 1
        slots = max(1, slots)
        shapes[str(name)] = (max(1, cells // slots), slots)
    return shapes


def _saved_overrides() -> tuple[
    dict[str, int], dict[str, int], dict[str, list[str]], dict[str, int]
]:
    """The operator's SAVED per-model context-window, `-np` slot, extra-flag and image-floor
    overrides from the settings store (owner-scoped), so the DEPLOY re-stamp preserves them —
    the settings API caller already passes overrides, and now the deploy caller does too.

    The image floor was missing from this tuple, so every Ops → Update reverted it to the catalog
    value — the exact failure this function was written to stop, reproduced for the override kind
    added after it. Load every kind here or the next one inherits the bug again.

    Without this the deploy regenerates the config from base catalog defaults on every update, so a
    raised window (e.g. qwen3.8-27b-q4 lifted to 128k) silently drops back to its 32k catalog `-c`
    while the meter still reports the saved 128k — the model then overflows its real window at a
    displayed ~25%. `up -d` doesn't restart the api on a model-only sync, so the boot reconcile
    (the backstop) may not fire; applying the overrides here fixes the config at write time.

    Best-effort: a DB hiccup returns empty maps (catalog defaults, the prior behaviour) rather than
    failing config generation and wedging the gateway."""
    try:
        import asyncio

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from jbrain.config import get_settings
        from jbrain.queue import SYSTEM_CTX
        from jbrain.settings_store import SqlSettingsStore

        async def _load() -> tuple[
            dict[str, int], dict[str, int], dict[str, list[str]], dict[str, int]
        ]:
            engine = create_async_engine(get_settings().database_url)
            try:
                store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
                return (
                    await store.llm_local_context_windows(SYSTEM_CTX),
                    await store.llm_local_parallel_slots(SYSTEM_CTX),
                    await store.llm_local_extra_args(SYSTEM_CTX),
                    await store.llm_local_image_min_tokens(SYSTEM_CTX),
                )
            finally:
                await engine.dispose()

        return asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001 — never fail config gen on a settings-read hiccup
        print(
            f"[llama-swap] could not load saved window/slot/flag/image-floor overrides ({exc}); "
            "using catalog defaults — the boot reconcile will correct it",
            file=sys.stderr,
        )
        return {}, {}, {}, {}


def _main(argv: list[str]) -> int:
    """CLI for the deploy re-stamp (`deploy/local-models-sync.sh`) and scripts/local-llm-setup.sh:
    `... <models_dir>` reads the MANIFEST env (catalog JSON) and writes the config, applying the
    operator's saved context-window / slot overrides so an update never resets a raised `-c`.

    `--check <models_dir>` instead PRINTS the ids whose required weights are incomplete (one per
    line, empty when all resolve) and exits 0 — the provisioning scripts' download filter. It
    reads the same globs the write path will, so a model that needs a NEWLY-required file is
    re-downloaded rather than passing a `*.gguf` presence check and failing the re-stamp."""
    if argv[:1] == ["--check"]:
        if len(argv) != 2:
            print("usage: ... --check <models_dir>", file=sys.stderr)
            return 2
        for model_id in unresolved_ids(argv[1], json.loads(os.environ["MANIFEST"])):
            print(model_id)
        return 0
    if len(argv) != 1:
        print(
            "usage: python -m jbrain.llm.llama_swap_config [--check] <models_dir>", file=sys.stderr
        )
        return 2
    root = argv[0]
    models = json.loads(os.environ["MANIFEST"])
    windows, slots, extra, floors = _saved_overrides()
    path = write(
        root, models, windows=windows, slots=slots, extra_args=extra, image_min_tokens=floors
    )
    applied = sum(
        1
        for m in models
        if str(m["id"]) in windows
        or str(m["id"]) in slots
        or str(m["id"]) in extra
        or str(m["id"]) in floors
    )
    print(
        f"wrote {path}: {len(models)} model(s), {applied} with a saved override; "
        "the app evicts to make room per load"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
