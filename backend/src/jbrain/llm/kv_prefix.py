"""Disk save/restore of the agent-turn model's primed prefix — the jerv prompt cache.

The interactive model's first-token latency is owned by a ~29k-token persona+tools prefill
(~60 s measured on gpt-oss-120b). The WarmKeeper keeps that prefix hot in a slot, and a
dedicated second slot keeps background traffic from evicting it — but both protections live
in RAM: a restart pays the prefill again, and a single-slot configuration (fewer slots is
the first thing an operator cuts when memory is tight) loses the prefix to any background
task. This store is the durable base layer under both: after the keeper primes, the slot's
KV state is saved to disk once; whenever the prefix is later found missing, it is restored
in ~2 s instead of re-read in ~60 (measured 2026-08-21: a 27,787-token slot back in ~90 ms
page-cache-warm).

WHY V1 OF THIS IDEA WAS REMOVED, AND WHAT EACH LESSON PINS HERE (the old code's own
post-mortem lives at `warm_keeper.py`'s prime step):

  - It saved garbage: with one slot shared by background traffic, "save the slot" captured
    whatever held it — 400s and 2 KB files. Here a save happens ONLY when a slot's
    `n_prompt_tokens` exactly equals the prime's own `usage.input_tokens`, read in the same
    breath as the prime — an integer match no unrelated request plausibly satisfies —
    and the server's `n_saved` must equal it again or the file is deleted.
  - It restored garbage silently. Here `n_restored` is verified against the same count and
    a floor, and any shortfall falls back to the prefill the restore was replacing.
  - It was inert-by-construction on the recurrent hybrids (restore clears the context
    checkpoints that are their only prefix-reuse mechanism) — they are refused up front,
    as are external-draft speculative entries, whose draft state no slot file captures
(MTP self-drafting entries are eligible: their draft derives from the target's own
hidden states, and speculation verifies every draft — a restore can only cost brief
draft acceptance, never correctness).
  - Its files could only be pruned by a deploy. Here the store holds itself to an
    owner-set byte budget (`MAX_STORE_BYTES`), evicting least-recently-USED files — a
    restore refreshes its file's clock — so every config the operator flips between
    (slot count, window) keeps its ~2 GiB file warm and only genuinely unused ones age
    out, not a graveyard and not a re-prefill on every config flip either (the
    one-file-per-model policy this replaces charged a full prefill each time the owner
    toggled the interactive slot, observed live 2026-08-23).

WHAT "/slots" ACTUALLY REPORTS (verified against llama.cpp server source, 2026-08-23,
by the adversarial review of this module's first draft): an idle slot's `n_prompt_tokens`
is the slot's whole CACHE — prompt plus every generated token — so after a real turn it
reads prompt+output-1, never the prompt size a caller recorded; and a slot that was
restored into but never used reports no `n_prompt_tokens` at all. An exact-integer
"prefix present" test is therefore structurally wrong on the restore side (it stays right
on the SAVE side only because the keeper's prime generates exactly one token, whose stop
token the server does not append — see `save_after_prime`). The restore gate below is a
conservative threshold instead: any slot holding at least a prefix-sized cache is left
alone, whatever it holds — mistaking a large foreign prompt for the prefix costs one
un-accelerated prefill (the pre-feature behaviour), while the inverse mistake would wipe
a live conversation to re-plant a prefix it already extends. A restore that has not yet
been used reports nothing, so `_restored_unused` remembers it and stops the keeper's tick
from streaming the same 2 GiB once a minute until the owner's first message.

ACCEPTED LIMITATION: the fingerprint covers the launch line, persona and tools — not the
GGUF bytes. Re-downloaded weights under an unchanged filename would restore KV computed
from the old weights. Weights updates change the filename/quant in practice, and the
failure mode is a stale-flavoured first answer, not corruption.

The FINGERPRINT is the validity rule: sha256 over the model's rendered launch line (read
back from llama-swap.yaml — the same source `served_shape_from_config` trusts, because it
cannot disagree with what the server executes) plus the exact system text and tool schema
the prime sent. Any change that could make a saved state stale — window, slots, extra
args, a new build's flags, a persona or tool edit — moves the filename, and files nobody
uses again age out of the byte budget. Everything here is best-effort: the worst case of
any failure is the prefill that would have happened anyway.

It deliberately does NOT cover server-injected, per-render content. gpt-oss's harmony
template renders a live `Current date` that this store cannot see (llama-server injects it,
not us) — a daily-changing token that, at the prompt HEAD, re-prefilled the whole restored
prefix every day while reporting a clean restore. That is fixed at the TEMPLATE layer, not
here: a vendored `--chat-template-file` (deploy/chat-templates/gpt-oss-120b.jinja) moves the
date to the prompt TAIL, so a restored prefix stays reusable across days and only the date
re-prefills. Folding the date into this fingerprint instead would just orphan the file every
midnight — so keep it out.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from collections import deque
from collections.abc import Sequence

import structlog

from jbrain import box_events
from jbrain.llm import llama_swap_config, local_catalog
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.types import LlmTool

log = structlog.get_logger()

# The task whose prefix is worth a disk file — the interactive chat turn. Spelled here
# (not imported from warm_keeper) so the router can depend on this module alone.
AGENT_TURN_TASK = "agent.turn"

# A restore below this many tokens restored nothing worth having: the jerv prefix is
# ~29k tokens, and v1's garbage files restored a few hundred. Falling back to the
# prefill is strictly better than trusting a stub.
MIN_PREFIX_TOKENS = 4096

# The whole `.kvslots` tree's disk allowance — the trade the owner chose on 2026-08-23
# (25 GiB of hard drive for prompt caches; changing it is a release, there is no knob).
# Files are ~2.2 GiB each, so this holds every config the operator actually flips between
# with room to spare; past it, the least-recently-USED file goes first (mtime is the clock
# — restores, primes and saves all touch it — because atime is unreliable under noatime).
MAX_STORE_BYTES = 25 * 1024**3

_SLOT_FILE_SUFFIX = ".kvslot"

# The patched engine (deploy/patches/0001) writes the slot's context checkpoints to
# `<slot file>.ckpt` on save and reloads them on restore — that sidecar is what makes a
# restored SWA/hybrid/recurrent prompt REUSABLE. The store treats the pair as one unit:
# prune/invalidate both together, and count the sidecar toward the disk budget.
_SIDECAR_EXT = ".ckpt"


def _slot_int(slot: dict[str, object], key: str) -> int:
    value = slot.get(key)
    return value if isinstance(value, int) else 0


def _save_dir_from_line(launch_line: str, models_root: str) -> str | None:
    """Where THIS launch line's llama-server saves slot files, translated to this
    process's mount of the volume. Read from the line rather than re-derived from the
    config generator's conventions, so the store looks wherever the server actually
    writes — a catalog-id vs served-name mismatch, or an operator's --slot-save-path
    override, cannot silently split the two. No flag → this model has no disk layer."""
    tokens = launch_line.split()
    try:
        raw = tokens[tokens.index("--slot-save-path") + 1]
    except (ValueError, IndexError):
        return None
    prefix = "/models/"
    if not raw.startswith(prefix):
        return None  # an override pointing outside the shared volume is unreachable from here
    return os.path.join(models_root, raw[len(prefix) :].rstrip("/"))


def _fingerprint(
    launch_line: str, system: str, tools: Sequence[LlmTool], reasoning_effort: str | None
) -> str:
    tool_blob = json.dumps(
        [{"name": t.name, "description": t.description, "schema": t.input_schema} for t in tools],
        sort_keys=True,
    )
    digest = hashlib.sha256()
    # The effort is part of the RENDERED PROMPT, not just a decoding knob: gpt-oss's
    # harmony template writes a literal "Reasoning: <level>" header, and the hybrids
    # toggle whole template branches. A cache saved under one effort never matches a
    # prompt sent under another, so the effort must move the filename — or an owner
    # changing the agent task's effort would restore a permanently-stale file whose
    # save is short-circuited by its own existence (observed design gap, 2026-08-23).
    for part in (launch_line, system, tool_blob, reasoning_effort or ""):
        digest.update(part.encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def _identity_components(
    launch_line: str, system: str, tools: Sequence[LlmTool], reasoning_effort: str | None
) -> dict[str, str]:
    """Short per-component digests of the SAME inputs `_fingerprint` hashes — purely
    diagnostic, so a fingerprint miss can say WHICH input drifted (the 2026-08-24 canvas
    case: one restore bought nothing and no log said why). Never used to derive the
    filename — the fingerprint stays byte-compatible with files already on the box."""
    tool_blob = json.dumps(
        [{"name": t.name, "description": t.description, "schema": t.input_schema} for t in tools],
        sort_keys=True,
    )

    def _short(part: str) -> str:
        return hashlib.sha256(part.encode()).hexdigest()[:8]

    return {
        "launch": _short(launch_line),
        "system": _short(system),
        "tools": _short(tool_blob),
        "effort": reasoning_effort or "",
    }


# Bounded wait for a busy slot before giving up on a restore. The observed miss
# (2026-08-24): the hook fired while a ~22 s vision side-call was 0.5 s from releasing
# the only slot, gave up silently, and the turn paid a 204 s full re-prefill. Waiting a
# couple of seconds is cheap against that; a slot still busy afterwards (a long
# generation) falls back to the old behaviour.
RESTORE_BUSY_POLLS = 8
RESTORE_BUSY_INTERVAL_S = 0.25

# How many recent outcomes the store keeps for the debug read. The owner has no terminal
# (CLAUDE.md #10) and box_events' widest owner surface is fifteen minutes, so a miss that
# happened an hour ago is otherwise unreachable; this ring is what makes "what has this
# store actually been doing?" answerable at all.
OUTCOME_HISTORY = 40

# Minimum gap between box_events rows for the SAME (model, outcome). Counters below record
# every occurrence — they are the truth. This only rate-limits the owner-facing surface, so
# a pathological loop (a poisoned file re-rejected on every tick) reports once rather than
# flooding the box's narration with a fault the counters already state precisely.
BOX_EVENT_MIN_INTERVAL_S = 300.0

# The outcomes that mean THE CACHE DID NOT HELP — each one costs a full prefill somewhere.
# They are the reason this instrumentation exists: every one of them was previously an
# `info` log line on a box whose owner cannot read logs, which is how this feature shipped
# silently inert twice (the read-only mount, and the 2026-08-24 flag/eligibility split).
MISS_OUTCOMES = frozenset(
    {
        "slots_unreadable",
        "slot_unidentified",
        "save_failed",
        "save_mismatch",
        "identity_drift",
        "restore_skipped_busy",
        "restore_failed",
        "restore_rejected",
    }
)


class KvPrefixStore:
    """One per process, wired beside the gateway. `models_root` is THIS process's view of
    the models volume (`settings.local_models_dir`); llama-server's view (`/models/…`) is
    rendered into `--slot-save-path` by the config generator, and the two meet at the same
    per-model directory."""

    def __init__(
        self,
        gateway: LocalGatewayClient,
        models_root: str,
        *,
        patch_active: bool = False,
        max_store_bytes: int = MAX_STORE_BYTES,
    ) -> None:
        self._gateway = gateway
        self._models_root = models_root
        # The disk allowance. A parameter rather than the module constant it defaults to,
        # because the constant's own comment conceded the gap: "changing it is a release,
        # there is no knob" — on a box whose owner has no terminal, and whose store runs at
        # 94% of this budget. Read once at startup from the owner's setting, like patch_active.
        self._max_store_bytes = max_store_bytes
        # Whether the Fast-Qwen-loads patched llama-server is the running build (the owner's
        # `local_llm_patch_restore_checkpoint` setting, read once at startup — see main.py).
        # It is the RUNTIME gate that admits the qwen3.8 MTP-hybrid entries to the disk layer:
        # the static `kv_slot_restorable` catalog flag was reverted (the stock server re-prefills
        # a restored hybrid with no context checkpoint), and the patch is what makes the restore
        # sound. Reading it once is enough because turning the setting on triggers the gateway
        # rebuild, which recreates this api container — the setting is re-read on that restart.
        self._patch_active = patch_active
        # The prime's exact token count per FINGERPRINT — the integer that identifies the
        # primed slot among /slots entries, and the expected restore size.
        #
        # Keyed by fingerprint rather than by served model because the count belongs to an
        # IDENTITY, not to a model: a file's count is frozen at its first save (an existing
        # file short-circuits the rewrite), while a per-model key tracks whatever primed most
        # recently. When the identity flapped — a hidden-set change, an effort change — the
        # restore of a perfectly good file was rejected against the OTHER identity's count and
        # the file was DELETED, with no re-save path while the keeper's memo held. That
        # directly defeated the LRU design's stated goal of keeping every config the operator
        # flips between warm.
        self._prime_tokens: dict[str, int] = {}
        # Served models restored-but-not-yet-used: such a slot reports NO n_prompt_tokens
        # (see module docstring), so without this memo every keeper tick would re-restore
        # the same file until the first message. Cleared when a turn uses it or a fresh
        # prime supersedes it.
        self._restored_unused: set[str] = set()
        # One restore at a time: a keeper tick and an inbound turn discovering the same
        # loss must not both stream the file into different slots.
        self._lock = asyncio.Lock()
        # The identity components of the last fingerprint OBSERVED to have a file (a save,
        # or a restore/resolve that found one) — what identity_drift diffs against.
        self._last_identity: dict[str, dict[str, str]] = {}
        # ---- instrumentation (see `snapshot`) ----
        # Every outcome this store reaches, counted since process start. Cheap, unbounded in
        # value but not in keys (one per outcome name), and the only way to tell "the cache
        # is healthy and quiet" from "the cache has not worked since boot" — which read
        # IDENTICALLY on every other surface, because both produce no rows at all.
        self._counters: dict[str, int] = {}
        # The last few outcomes with timestamps, for the debug read. Bounded ring.
        self._events: deque[dict[str, object]] = deque(maxlen=OUTCOME_HISTORY)
        # Per served model, the most recent outcome — what a state read leads with.
        self._last_outcome: dict[str, dict[str, object]] = {}
        # (model, outcome) -> monotonic time of the last box_events row, for the rate limit.
        self._box_event_at: dict[tuple[str, str], float] = {}

    # ---- instrumentation ------------------------------------------------------------

    async def _note(
        self,
        outcome: str,
        served_model: str,
        *,
        warn: bool = False,
        **fields: object,
    ) -> None:
        """Record one outcome: count it, ring it, log it, and — for a miss — put it on the
        owner's own surface.

        Everything this store does is best-effort, and that used to mean every failure was an
        `info` line on a box whose owner cannot read logs. The counter is the truth (it moves
        on every occurrence); the box_events row is the attention, rate-limited so a repeating
        fault reports once rather than burying the narration it belongs in."""
        self._counters[outcome] = self._counters.get(outcome, 0) + 1
        record = {
            "at": time.time(),
            "model": served_model,
            "outcome": outcome,
            **fields,
        }
        self._events.append(record)
        self._last_outcome[served_model] = record
        if warn:
            log.warning(f"kv_prefix.{outcome}", model=served_model, **fields)
        else:
            log.info(f"kv_prefix.{outcome}", model=served_model, **fields)
        if outcome not in MISS_OUTCOMES:
            return
        key = (served_model, outcome)
        now = time.monotonic()
        last = self._box_event_at.get(key)
        if last is not None and (now - last) < BOX_EVENT_MIN_INTERVAL_S:
            return
        self._box_event_at[key] = now
        # Only short scalar fields reach the owner's row: `identity_drift` carries two whole
        # component maps, which would fill the 200-char detail with digests and push out the
        # one thing that matters (WHICH component moved). The log line keeps them all.
        detail = ", ".join(
            f"{k}={v}" for k, v in fields.items() if v is not None and len(str(v)) <= 60
        )
        await box_events.record(
            box_events.KV_PREFIX_MISSED,
            served_model,
            detail=f"{outcome}{': ' + detail if detail else ''}",
            status="failed",
        )

    async def snapshot(
        self,
        probes: Sequence[tuple[str, str, Sequence[LlmTool], str | None]] = (),
    ) -> dict[str, object]:
        """Everything this store knows, for the owner debug read.

        `probes` are (served_model, system, tools, reasoning_effort) tuples — the identity a
        caller believes a turn would send. Resolving them here is the point: it answers the
        question no other surface can, which is whether the file this store would look for is
        the file that is actually on disk. A state of `cold_no_file` beside a healthy-looking
        box is the signature of an identity drift, and the `identity` digests say which
        component moved."""
        usage = await asyncio.to_thread(self._walk_store)
        models: list[dict[str, object]] = []
        for served_model, system, tools, effort in probes:
            entry: dict[str, object] = {"model": served_model}
            eligible = self._eligible(served_model)
            entry["eligible"] = eligible is not None
            if eligible is None:
                entry["reason"] = self._ineligible_reason(served_model)
            entry["restored_unused"] = served_model in self._restored_unused
            entry["last_outcome"] = self._last_outcome.get(served_model)
            resolved = await asyncio.to_thread(self._resolve, served_model, system, tools, effort)
            if resolved is None:
                entry["state"] = "no_disk_layer" if eligible is not None else "ineligible"
                models.append(entry)
                continue
            fingerprint, _save_dir, identity = resolved
            entry["fingerprint"] = fingerprint
            entry["prime_tokens"] = self._prime_tokens.get(fingerprint)
            entry["identity"] = identity
            entry["last_known_identity"] = self._last_identity.get(served_model)
            path = os.path.join(_save_dir, f"{fingerprint}{_SLOT_FILE_SUFFIX}")
            stat = await asyncio.to_thread(self._stat_quietly, path)
            if stat is None:
                entry["state"] = "cold_no_file"
            else:
                entry["file_bytes"] = stat[0]
                entry["file_mtime"] = stat[1]
                entry["sidecar"] = (
                    await asyncio.to_thread(self._stat_quietly, path + _SIDECAR_EXT)
                ) is not None
                entry["state"] = (
                    "restored_unused" if served_model in self._restored_unused else "file_present"
                )
            models.append(entry)
        return {
            "counters": dict(sorted(self._counters.items())),
            "recent": list(self._events),
            "store": {
                "bytes": usage[0],
                "files": usage[1],
                "budget_bytes": self._max_store_bytes,
                "over_budget": usage[0] > self._max_store_bytes,
                "by_file": usage[2],
            },
            "models": models,
        }

    def _ineligible_reason(self, served_model: str) -> str:
        """Why `_eligible` said no — the difference between "this model will never use the
        disk layer" and "the owner's patch setting is off", which look the same from outside
        and have completely different remedies."""
        model = local_catalog.get_by_served(served_model)
        if model is None:
            return "not a catalog model"
        if model.recurrent and model.is_mtp_speculative and not self._patch_active:
            return "recurrent MTP hybrid, and the Fast-Qwen-loads patch setting is off"
        if model.recurrent:
            return "recurrent: a restored slot has no context checkpoints"
        if model.is_speculative:
            return "external-draft speculative: no slot file captures the draft state"
        return "ineligible"

    def _stat_quietly(self, path: str) -> tuple[int, float] | None:
        """Runs in a thread — (size, mtime) or None when the file is not there."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_size, st.st_mtime)

    def _walk_store(self) -> tuple[int, int, list[dict[str, object]]]:
        """Runs in a thread — (total bytes, file count, per-file rows) for the whole tree.

        Shares its accounting rules with `_prune_to_budget`: a sidecar is billed to its slot
        file, never counted on its own. The size the budget acts on and the size the owner
        reads must be the same number, or a prune that fires looks unexplained."""
        root = os.path.join(self._models_root, llama_swap_config.KVSLOT_DIR)
        rows: list[dict[str, object]] = []
        total = 0
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(_SLOT_FILE_SUFFIX):
                    continue
                path = os.path.join(folder, name)
                with contextlib.suppress(OSError):
                    stat = os.stat(path)
                    size = stat.st_size
                    sidecar = False
                    with contextlib.suppress(OSError):
                        size += os.stat(path + _SIDECAR_EXT).st_size
                        sidecar = True
                    total += size
                    rows.append(
                        {
                            "model": os.path.basename(folder),
                            "fingerprint": name[: -len(_SLOT_FILE_SUFFIX)],
                            "bytes": size,
                            "mtime": stat.st_mtime,
                            "sidecar": sidecar,
                        }
                    )
        rows.sort(key=lambda r: r["mtime"], reverse=True)  # type: ignore[arg-type,return-value]
        return (total, len(rows), rows)

    # ---- identity -------------------------------------------------------------------

    def _eligible(self, served_model: str) -> local_catalog.LocalModel | None:
        model = local_catalog.get_by_served(served_model)
        if model is None:
            return None
        # Blanket rule: plain attention, no speculation. `kv_slot_restorable` is the
        # per-entry override for shapes reasoned through and verified live (the qwen
        # hybrid+MTP argument lives on the catalog field). A restored hybrid slot has no
        # context checkpoints, so its first mid-prefix divergence reprocesses from zero —
        # today's cost, fail-soft; a byte-stable prefix (the load warm, #1198's anchors)
        # never diverges mid-prefix.
        if model.kv_slot_restorable:
            return model
        # The runtime gate for the qwen3.8 MTP-hybrids: with the Fast-Qwen-loads patch built
        # in, a recurrent + MTP-self-drafting entry restores soundly (the patch supplies the
        # checkpoint restore the stock server lacks; MTP self-drafting verifies every draft so
        # a restore never costs correctness). The static flag stays OFF in the catalog — the
        # owner's setting is the gate now, so no separate code change re-enables these.
        if self._patch_active and model.recurrent and model.is_mtp_speculative:
            return model
        if model.recurrent or model.is_speculative:
            return None
        return model

    def _resolve(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> tuple[str, str, dict[str, str]] | None:
        """(fingerprint, save-dir, identity components) for the CURRENT launch line, or
        None when the model is
        not served, or is served without --slot-save-path (no disk layer). One read of the
        rendered config feeds both, so they can never describe two different servers."""
        line = llama_swap_config.launch_line(self._models_root, served_model)
        if line is None:
            return None
        save_dir = _save_dir_from_line(line, self._models_root)
        if save_dir is None:
            return None
        return (
            _fingerprint(line, system, tools, reasoning_effort),
            save_dir,
            _identity_components(line, system, tools, reasoning_effort),
        )

    def note_prefix_lost(self, served_model: str) -> None:
        """The slot this store restored into is gone — an eviction, an operator unload, or a
        bare restore-load. Registered with the residency coordinator beside the keeper's hook.

        Without it `_restored_unused` is a belief nothing can correct: it is set by a restore
        and cleared only by a turn that USES that restore or by a fresh prime, so a model
        evicted in between leaves the memo set forever. `restore_if_lost` then returns False
        at its first line — before it reads `/slots` at all — and the next jerv turn pays the
        full ~125 s prefill this store exists to prevent, with a valid file sitting on disk
        unread. Residency already reported this; only the keeper was listening."""
        self._restored_unused.discard(served_model)

    def note_agent_turn(self, served_model: str, input_tokens: int) -> None:
        """A real jerv turn completed — whatever was restored has now been used, and the
        slot it grew reports a prefix-sized cache on its own from here on."""
        if input_tokens > 0:
            self._restored_unused.discard(served_model)

    # ---- save -----------------------------------------------------------------------

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        prime_tokens: int,
        *,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Persist the freshly primed slot, called by the keeper in the same breath as a
        successful prime. Returns True when a valid file exists afterwards (already
        present counts). Best-effort: every failure is a log line, never an exception."""
        model = self._eligible(served_model)
        if model is None or prime_tokens < MIN_PREFIX_TOKENS:
            return False
        # A fresh prime supersedes any restored-but-unused state.
        self._restored_unused.discard(served_model)
        resolved = await asyncio.to_thread(
            self._resolve, served_model, system, tools, reasoning_effort
        )
        if resolved is None:
            return False
        fingerprint, save_dir, identity = resolved
        self._prime_tokens[fingerprint] = prime_tokens
        self._last_identity[served_model] = identity
        path = os.path.join(save_dir, f"{fingerprint}{_SLOT_FILE_SUFFIX}")
        if await asyncio.to_thread(os.path.exists, path):
            # This exact prefix is already on disk — skip the 2 GiB write, but the prime
            # that got us here is still a USE: refresh the LRU clock, or a config that
            # stays hot in RAM for weeks reads as the store's stalest file. EXCEPT when a
            # checkpoint-gated (recurrent) model's file lacks its sidecar: a file from the
            # pre-sidecar engine restores but can never REUSE (no checkpoints ride along,
            # so the next warm re-prefills — measured live 2026-08-24), and touching it
            # would freeze it in that state forever. The slot just finished a real prime,
            # so falling through to a fresh save captures the live checkpoints and writes
            # the sidecar — a one-time upgrade per stale file, not a recurring cost.
            if not model.recurrent or await asyncio.to_thread(os.path.exists, path + _SIDECAR_EXT):
                await asyncio.to_thread(self._touch, path)
                return True
            await self._note("resaving_for_sidecar", served_model)
        try:
            slots = await self._gateway.slots(served_model)
        except LocalGatewayError as exc:
            await self._note("slots_unreadable", served_model, phase="save", error=str(exc))
            return False
        # EXACT match works here only because the prime generates exactly ONE token: the
        # server appends every sampled token to the slot's cache EXCEPT the final stop
        # token, so a max_tokens=1 prime leaves the cache at precisely its prompt size.
        # A prime that ever generated more would never match — which fails SAFE (skip +
        # log), but silently: if `slot_unidentified` becomes chronic, look here first.
        matches = [
            s
            for s in slots
            if isinstance(s, dict)
            and s.get("n_prompt_tokens") == prime_tokens
            and not s.get("is_processing")
        ]
        if len(matches) != 1:
            # Zero: something replaced the prime between the converse returning and this
            # read — the exact race v1 lost by saving anyway. More than one: ambiguous.
            await self._note(
                "slot_unidentified",
                served_model,
                expected_tokens=prime_tokens,
                candidates=len(matches),
            )
            return False
        slot_id = _slot_int(matches[0], "id")
        try:
            resp = await self._gateway.save_slot(
                served_model, slot_id, f"{fingerprint}{_SLOT_FILE_SUFFIX}"
            )
        except LocalGatewayError as exc:
            # A failed or timed-out save can leave a PARTIAL file at the trusted name —
            # and an existing file short-circuits every future save while restores keep
            # reading junk. Remove whatever landed, or this fingerprint is poisoned until
            # a config change happens to move it.
            await self._note("save_failed", served_model, error=str(exc))
            await asyncio.to_thread(self._remove_quietly, path)
            return False
        n_saved = resp.get("n_saved")
        if n_saved != prime_tokens:
            # The slot moved under the save, or the server saved something else. The file
            # on disk is NOT the prime — remove it, or a later restore trusts it. Only THIS
            # file: other configs' files were saved under their own verified counts and a
            # bad write here says nothing about them.
            await self._note(
                "save_mismatch",
                served_model,
                warn=True,
                expected=prime_tokens,
                n_saved=n_saved,
            )
            await asyncio.to_thread(self._remove_quietly, path)
            return False
        # An eviction used to be the one thing this store did that left NO trace at all —
        # not even an info line. A config the operator flips back to next month will have
        # been pruned silently, and its re-prefill then reads as a new fault.
        for gone, size in await asyncio.to_thread(self._prune_to_budget, path):
            await self._note("evicted", served_model, file=os.path.basename(gone), bytes=size)
        await box_events.record(
            box_events.KV_PREFIX_SAVED,
            served_model,
            detail=f"{prime_tokens}-token jerv prefix saved to disk",
        )
        await self._note("saved", served_model, tokens=prime_tokens, slot=slot_id)
        return True

    async def clear(self, served_model: str | None = None) -> dict[str, object]:
        """Delete this store's files — all of them, or one model's. Returns what went.

        The no-terminal twin of `rm -rf .kvslots` (CLAUDE.md #10). It exists for the same
        reason `drop-page-cache` does: the only way to reclaim this space was host shell,
        which the owner running this box remotely does not have — and the budget's own
        comment conceded it ("changing it is a release, there is no knob") while the store
        sat at 94% of it. Safe at any time: a deleted file costs at most one re-prefill,
        which is the behaviour without this store at all, and the next prime writes it back.

        In-memory state goes with the files: a `_prime_tokens` entry for a file that no
        longer exists would have the next restore verify against a count nothing can match."""
        model = local_catalog.get_by_served(served_model) if served_model else None
        only = model.id if model is not None else served_model
        removed = await asyncio.to_thread(self._clear_files, only)
        if served_model is None:
            self._prime_tokens.clear()
            self._restored_unused.clear()
            self._last_identity.clear()
        else:
            self._restored_unused.discard(served_model)
            self._last_identity.pop(served_model, None)
        for fingerprint in removed[1]:
            self._prime_tokens.pop(fingerprint, None)
        await self._note("cleared", served_model or "*", files=len(removed[1]), bytes=removed[0])
        return {"files": len(removed[1]), "bytes": removed[0], "model": served_model or "*"}

    def _clear_files(self, only_dir: str | None) -> tuple[int, list[str]]:
        """Runs in a thread — remove slot files (and their sidecars) under the tree, or under
        one model's folder. Returns (bytes freed, the fingerprints removed)."""
        root = os.path.join(self._models_root, llama_swap_config.KVSLOT_DIR)
        if only_dir is not None:
            root = os.path.join(root, only_dir)
        freed = 0
        fingerprints: list[str] = []
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(_SLOT_FILE_SUFFIX):
                    continue
                path = os.path.join(folder, name)
                with contextlib.suppress(OSError):
                    freed += os.stat(path).st_size
                with contextlib.suppress(OSError):
                    freed += os.stat(path + _SIDECAR_EXT).st_size
                self._remove_quietly(path)
                fingerprints.append(name[: -len(_SLOT_FILE_SUFFIX)])
        return (freed, fingerprints)

    def _remove_quietly(self, path: str) -> None:
        """Runs in a thread — delete a file that is now known-bad, tolerating absence.

        The patched engine writes a checkpoint sidecar beside each slot file
        (deploy/patches/0001); the pair lives and dies together — a bad slot file's
        sidecar restores checkpoints for state that no longer exists."""
        with contextlib.suppress(OSError):
            os.remove(path)
        with contextlib.suppress(OSError):
            os.remove(path + _SIDECAR_EXT)

    def _touch(self, path: str) -> None:
        """Runs in a thread — bump the file's mtime, the LRU clock a restore refreshes."""
        with contextlib.suppress(OSError):
            os.utime(path, None)

    def _prune_to_budget(self, keep_path: str) -> list[tuple[str, int]]:
        """Runs in a thread (asyncio.to_thread) — plain blocking fs on purpose.

        Hold the whole `.kvslots` tree at or under the store's byte budget by deleting
        least-recently-used files (oldest mtime first), across every model's folder. The
        just-saved file is never a candidate, whatever its mtime — deleting the thing the
        save just verified would turn a full store into a store that forgets its newest
        state. Files an operator parked outside the standard tree (a --slot-save-path
        override) are simply not this budget's to manage.

        The keep-path guard protects only THIS process's save: today that is sound
        because exactly one KvPrefixStore exists (the api's — the worker wires none), but
        a second store pruning concurrently could evict the first's fresh file. If a
        store ever grows into another process, this needs a cross-process story first."""
        root = os.path.join(self._models_root, llama_swap_config.KVSLOT_DIR)
        entries: list[tuple[float, int, str]] = []
        total = 0
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if name.endswith(_SLOT_FILE_SUFFIX + _SIDECAR_EXT):
                    # A sidecar whose slot file is gone (a crash between the paired
                    # removes) is unusable and otherwise invisible to this budget.
                    if name[: -len(_SIDECAR_EXT)] not in names:
                        with contextlib.suppress(OSError):
                            os.remove(os.path.join(folder, name))
                    continue
                if not name.endswith(_SLOT_FILE_SUFFIX):
                    continue
                path = os.path.join(folder, name)
                # Per-file, so one entry vanishing between listdir and stat skips that
                # entry rather than silently abandoning the whole prune round. The
                # checkpoint sidecar is billed to its slot file and evicted with it —
                # never on its own, or a pruned sidecar would silently turn its
                # surviving slot file's restores back into full re-prefills.
                with contextlib.suppress(OSError):
                    stat = os.stat(path)
                    size = stat.st_size
                    with contextlib.suppress(OSError):
                        size += os.stat(path + _SIDECAR_EXT).st_size
                    total += size
                    if os.path.abspath(path) != os.path.abspath(keep_path):
                        entries.append((stat.st_mtime, size, path))
        entries.sort()
        evicted: list[tuple[str, int]] = []
        for _mtime, size, path in entries:
            if total <= self._max_store_bytes:
                break
            with contextlib.suppress(OSError):
                os.remove(path)
                with contextlib.suppress(OSError):
                    os.remove(path + _SIDECAR_EXT)
                total -= size
                evicted.append((path, size))
        return evicted

    # ---- restore --------------------------------------------------------------------

    async def restore_if_lost(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        *,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Put the prefix back if nothing prefix-sized is cached anywhere and a valid file
        exists. Returns True only when a verified restore happened.

        THE GATE IS A THRESHOLD, NOT AN EQUALITY — see the module docstring for the /slots
        semantics that force this. Any slot whose cache is at least prefix-sized is left
        alone whatever it holds: a conversation that grew from the prefix, the prime
        itself, or a large foreign prompt all read the same, and the worst cost of leaving
        a foreign one alone is one un-accelerated prefill (the pre-feature behaviour). The
        inverse mistake — restoring over a conversation because its exact size stopped
        matching — is the harm this store must never cause. A slot restored into but not
        yet used reports NO size at all, so `_restored_unused` stands in for it until a
        turn or a fresh prime supersedes it."""
        if self._eligible(served_model) is None:
            return False
        # The lock opens HERE, not at the slots read. It used to sit below the memo check and
        # the file check, so two callers discovering the same loss both passed those, then
        # serialized and both restored — two multi-GB streams, and the prefix planted in BOTH
        # slots, including the one whose separation from the interactive slot is the entire
        # point of a second slot. Reproduced; the comment on `self._lock` always claimed this.
        async with self._lock:
            return await self._restore_locked(served_model, system, tools, reasoning_effort)

    async def _restore_locked(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> bool:
        """`restore_if_lost`'s body, with `self._lock` held. Split out only so the lock has
        one acquisition point and cannot be taken twice on one path."""
        if served_model in self._restored_unused:
            return False  # already restored; the slot reports nothing until a turn uses it
        resolved = await asyncio.to_thread(
            self._resolve, served_model, system, tools, reasoning_effort
        )
        if resolved is None:
            return False
        fingerprint, save_dir, identity = resolved
        path = os.path.join(save_dir, f"{fingerprint}{_SLOT_FILE_SUFFIX}")
        if not await asyncio.to_thread(os.path.exists, path):
            # Say WHY there is no file for this identity: when a file was known under a
            # different identity, name the component that moved — the 2026-08-24 canvas
            # case (a 204 s unhealed re-prefill) had no way to tell a tool-set flap from
            # a race. `effort` prints its value; the rest print short digests.
            last = self._last_identity.get(served_model)
            if last is not None and last != identity:
                await self._note(
                    "identity_drift",
                    served_model,
                    changed=sorted(k for k in identity if identity[k] != last.get(k)),
                    now=identity,
                    was=last,
                )
            return False
        self._last_identity[served_model] = identity
        try:
            slots = await self._gateway.slots(served_model)
        except LocalGatewayError as exc:
            await self._note("slots_unreadable", served_model, phase="restore", error=str(exc))
            return False
        # The smallest cache that could BE the prefix: the prime's own size. A slot
        # below it (a small task's residue) cannot contain the prefix and is fair to
        # restore over; one at or above it might, and is not. Before any prime has run
        # (a fresh process beside a long-running server) the floor stands in, so
        # anything substantial — a conversation the server faithfully kept — stays
        # untouchable until the keeper's first prime establishes the real number.
        prime = self._prime_tokens.get(fingerprint)
        threshold = prime if prime is not None else MIN_PREFIX_TOKENS
        occupied = [s for s in slots if isinstance(s, dict)]
        if any(_slot_int(s, "n_prompt_tokens") >= threshold for s in occupied):
            # Something prefix-sized is cached — never restore over it. This branch is
            # ALSO the only one a healthy hot config ever reaches (the keeper's settled
            # tick lands here every minute), so it must refresh the LRU clock: without
            # this touch the hottest config's file keeps its boot-time mtime and is the
            # FIRST out of the budget (adversarial review, 2026-08-23).
            await asyncio.to_thread(self._touch, path)
            return False
        idle = [s for s in occupied if not s.get("is_processing")]
        if not idle:
            # Every slot busy. Don't give up silently — the observed miss (2026-08-24)
            # was a side-call 0.5 s from releasing the only slot, and the turn that
            # followed paid a 204 s full re-prefill. Wait briefly for a slot to free,
            # re-checking the prefix-sized guard each poll (the request that frees the
            # slot may leave a conversation there that must not be restored over).
            for _ in range(RESTORE_BUSY_POLLS):
                await asyncio.sleep(RESTORE_BUSY_INTERVAL_S)
                try:
                    slots = await self._gateway.slots(served_model)
                except LocalGatewayError as exc:
                    await self._note(
                        "slots_unreadable", served_model, phase="busy_wait", error=str(exc)
                    )
                    return False
                occupied = [s for s in slots if isinstance(s, dict)]
                if any(_slot_int(s, "n_prompt_tokens") >= threshold for s in occupied):
                    await asyncio.to_thread(self._touch, path)
                    return False  # freed into something prefix-sized — leave it alone
                idle = [s for s in occupied if not s.get("is_processing")]
                if idle:
                    await self._note("restore_waited_for_slot", served_model)
                    break
            else:
                await self._note(
                    "restore_skipped_busy",
                    served_model,
                    slots=[_slot_int(s, "n_prompt_tokens") for s in occupied],
                )
                return False
        # Prefer an empty slot; else the one holding the smallest foreign prompt.
        target = min(idle, key=lambda s: _slot_int(s, "n_prompt_tokens"))
        slot_id = _slot_int(target, "id")
        started = time.perf_counter()
        try:
            resp = await self._gateway.restore_slot(
                served_model, slot_id, f"{fingerprint}{_SLOT_FILE_SUFFIX}"
            )
        except LocalGatewayError as exc:
            # Same reasoning as the rejected-restore branch below, which this used to
            # lack: a failed or timed-out restore can leave a PARTIAL file at the trusted
            # name, and an existing file short-circuits every future save
            # (`save_after_prime` treats existence as proof and returns True) while every
            # future restore repeats this error. Nothing else ever repairs it, so the
            # fingerprint stays poisoned until a config change happens to move it.
            await self._note("restore_failed", served_model, error=str(exc))
            await asyncio.to_thread(self._remove_quietly, path)
            return False
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        n_restored = resp.get("n_restored")
        expected = self._prime_tokens.get(fingerprint)
        if (
            not isinstance(n_restored, int)
            or n_restored < MIN_PREFIX_TOKENS
            or (expected is not None and n_restored != expected)
        ):
            # A stub or a partial read: the slot now holds junk, but the next request
            # simply misses the cache and prefills — the exact behaviour without this
            # store. Log it loudly; a repeat means the file is bad, and the next
            # successful save replaces it.
            await self._note(
                "restore_rejected",
                served_model,
                warn=True,
                n_restored=n_restored,
                expected=expected,
            )
            # The file is proven bad; leaving it means every future save is
            # short-circuited by its existence and every restore repeats this. Remove
            # it so the next prime's save can lay down a good one.
            await asyncio.to_thread(self._remove_quietly, path)
            return False
        if expected is None:
            # First restore of this process life (a boot): adopt the restored count as
            # the prime size so slot identification works before any prime has run.
            self._prime_tokens[fingerprint] = n_restored
        # A restore IS a use: refresh the file's mtime so the budget prune keeps the
        # caches that earn their disk and ages out the ones nothing restores.
        await asyncio.to_thread(self._touch, path)
        # The slot will report NO size until a request uses it — remember the restore,
        # or every keeper tick re-streams the same 2 GiB until the first message.
        self._restored_unused.add(served_model)
        await box_events.record(
            box_events.KV_PREFIX_RESTORED,
            served_model,
            detail=f"{n_restored}-token jerv prefix restored from disk in {elapsed_ms} ms",
        )
        await self._note(
            "restored",
            served_model,
            tokens=n_restored,
            slot=slot_id,
            elapsed_ms=elapsed_ms,
        )
        return True
