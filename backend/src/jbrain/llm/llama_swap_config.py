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

import glob
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import cast

# Concrete, distinct upstream ports — llama-swap's ${PORT} macro isn't substituted
# by every build, and the non-swapping group runs models concurrently so they can't
# share a port. 127.0.0.1: llama-swap and llama-server share the container.
UPSTREAM_PORT_BASE = 9100
# Where llama-server reads/writes KV-slot state files, inside the gateway container. A named
# docker volume (`llm_kv`), NOT a subdirectory of the read-only weights mount: the path must be
# writable, must survive the container being recreated by an update, and must already exist —
# llama-server refuses to start when `--slot-save-path` names a missing directory, and that
# flag sits in every model's command.
KV_SLOT_DIR = "/kv/"


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
    lines = ["# Generated by jbrain.llm.llama_swap_config — do not edit by hand.", "models:"]
    for i, m in enumerate(models):
        port = UPSTREAM_PORT_BASE + i
        model_id = str(m["id"])
        gguf = resolve_weight(root, model_id, str(m["gguf_include"]))
        window = windows.get(model_id, int(cast(int, m["context_window"])))
        catalog_args = tuple(
            str(a) for a in cast("Sequence[str]", m.get("extra_server_args") or ())
        )
        operator_args = tuple(str(a) for a in extra_args.get(model_id, ()))
        n_slots = max(1, slots.get(model_id, 1))
        # Either source can turn speculation on: the catalog's static flags, or an operator
        # trying `--spec-type` remotely via the extra-args route. Both must pin the model to
        # one slot, so the test reads the flags actually going on the command line.
        if _is_speculative(catalog_args + operator_args):
            # llama.cpp's speculative paths serve ONE sequence: MTP takes no second parallel
            # slot, and draft acceptance collapses as concurrent sequences rise (reported on
            # this exact gfx1151 SoC). A saved -np override would silently defeat the very
            # speedup the model was installed for, so the serving mode wins over the setting
            # rather than the operator having to know the interaction.
            n_slots = 1
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
            # Prompt-prefix KV reuse (docs/plans/LLM_PROMPT_CACHE_PLAN.md W2): keep the KV of
            # a matching leading prefix and salvage it via KV-shifting even after a later
            # divergence, so a stable system-prompt + history prefix isn't re-prefilled every
            # turn (the "slow first token"). 256 is the min chunk size worth reusing. Pairs
            # with W1's cache-stable message layout. A long-standing llama.cpp server flag.
            "--cache-reuse",
            "256",
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
            # exactly that backend. 1024 also trims the big-vocab graph reserve (#23527), which
            # sizes a worst-case logits buffer off `n_ubatch × n_vocab` and costs ~3 GiB at
            # Qwen3.8's 248k vocab.
            "-ub",
            "1024",
            # Per-slot context checkpoints, down from llama-server's default of 32. On a HYBRID
            # (recurrent) model each checkpoint is a full copy of the SSM state — ~150 MiB for
            # Qwen3.8 — because a recurrent state cannot be partially rewound, and they are
            # device-resident. 32 of them is ~4.7 GiB per slot spent on rollback depth nothing
            # here uses (llama.cpp #20145, #23371).
            "--ctx-checkpoints",
            "2",
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
        # KV-slot state files live on a dedicated writable volume (docker-compose `llm_kv`),
        # so the primed prefix survives the model process being unloaded and reloaded. The
        # trailing slash is load-bearing: llama-server concatenates path + filename. The
        # directory must EXIST or llama-server refuses to start — the named volume guarantees
        # that, which is why this is not a bind mount into the read-only weights dir.
        cmd += ["--slot-save-path", KV_SLOT_DIR]
        # Full history on the sliding-window layers. Without it a slot restore on an SWA model
        # succeeds and is then thrown away (see LocalModel.kv_full_history).
        if m.get("kv_full_history"):
            cmd.append("--swa-full")
        # Model-specific serving flags (e.g. the MTP variant's `--spec-type draft-mtp …`
        # self-speculative-decoding config), appended verbatim after the shared flags.
        cmd += catalog_args
        # Operator overrides last, so they append to (never reorder) the catalog's own flags.
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
) -> str:
    """Render and atomically write {root}/llama-swap.yaml (temp + rename so the
    gateway's --watch-config never sees a half-written file). Returns the path."""
    text = render(models, root, windows=windows, slots=slots, extra_args=extra_args)
    path = os.path.join(root, "llama-swap.yaml")
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def _saved_overrides() -> tuple[dict[str, int], dict[str, int], dict[str, list[str]]]:
    """The operator's SAVED per-model context-window, `-np` slot and extra-flag overrides from
    the settings store (owner-scoped), so the DEPLOY re-stamp preserves them — the settings API
    caller already passes overrides, and now the deploy caller does too.

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

        async def _load() -> tuple[dict[str, int], dict[str, int], dict[str, list[str]]]:
            engine = create_async_engine(get_settings().database_url)
            try:
                store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
                return (
                    await store.llm_local_context_windows(SYSTEM_CTX),
                    await store.llm_local_parallel_slots(SYSTEM_CTX),
                    await store.llm_local_extra_args(SYSTEM_CTX),
                )
            finally:
                await engine.dispose()

        return asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001 — never fail config gen on a settings-read hiccup
        print(
            f"[llama-swap] could not load saved window/slot/flag overrides ({exc}); "
            "using catalog defaults — the boot reconcile will correct it",
            file=sys.stderr,
        )
        return {}, {}, {}


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
    windows, slots, extra = _saved_overrides()
    path = write(root, models, windows=windows, slots=slots, extra_args=extra)
    applied = sum(
        1
        for m in models
        if str(m["id"]) in windows or str(m["id"]) in slots or str(m["id"]) in extra
    )
    print(
        f"wrote {path}: {len(models)} model(s), {applied} with a saved window/slot/flag override; "
        "the app evicts to make room per load"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
