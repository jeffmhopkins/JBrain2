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

PER ROLE AND PER CONVERSATION (FLASH_NEXT_ENGINE_PLAN F4). On a pooled model — Flash-Next's
nine role-pinned slots over one shared KV pool — a prefix belongs to a ROLE: it is saved from
that role's slot, restored into that role's slot only, never over an occupied slot, and never
when the restore would push the pool past its cells. Memos and drift diagnostics are kept per
(model, role), so a research or scheduled turn is not mistaken for jerv's identity drifting.
The interactive slot also carries CONVERSATION files (`kv_conversation`): the conversation
leaving slot 0 is saved as it goes, and one that speaks again is restored before its request
when its saved messages still open the new prompt. Both kinds share the byte budget, and every
conversation file is evicted before any role prefix. A hybrid's restore is only worth anything
with the checkpoint-sidecar patch, so such a model is admitted only while each save proves the
patch by writing its sidecar. A model with no pool keeps the single-identity behaviour above.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Sequence
from typing import Literal

import structlog

from jbrain import box_events
from jbrain.llm import engine as engines
from jbrain.llm import kv_conversation, kv_pool_guard, llama_swap_config, local_catalog
from jbrain.llm.kv_conversation import ConversationHold, ConversationMeta
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.slot_roles import KvPool, SlotRole, layout_matches
from jbrain.llm.types import LlmTool

log = structlog.get_logger()

# The task whose prefix is worth a disk file — the interactive chat turn. Spelled here
# (not imported from warm_keeper) so the router can depend on this module alone.
AGENT_TURN_TASK = "agent.turn"

# A restore below this many tokens restored nothing worth having: the jerv prefix is
# ~29k tokens, and v1's garbage files restored a few hundred. Falling back to the
# prefill is strictly better than trusting a stub.
MIN_PREFIX_TOKENS = 4096

# The whole `.kvslots` tree's disk allowance by default. 25 GiB was the owner's 2026-08-23
# trade for one standard prefix per config; 40 (owner, 2026-10-04) adds room for Flash-Next's
# role prefixes and conversation files beside them. The owner's setting overrides it, live.
# Past it, conversation files go first, then the least-recently-USED role prefix (mtime is the
# clock — restores, primes and saves all touch it — because atime is unreliable under noatime).
MAX_STORE_BYTES = 40 * 1024**3

_SLOT_FILE_SUFFIX = ".kvslot"

# The patched engine (deploy/patches/0001) writes the slot's context checkpoints to
# `<slot file>.ckpt` on save and reloads them on restore — that sidecar is what makes a
# restored SWA/hybrid/recurrent prompt REUSABLE. The store treats the pair as one unit:
# prune/invalidate both together, and count the sidecar toward the disk budget.
_SIDECAR_EXT = ".ckpt"
# A conversation file's claim (kv_conversation.ConversationMeta) lives beside it and goes with it.
_META_EXT = kv_conversation.META_EXT

# Free disk a save must leave: twice the file it is about to write, and never under this floor.
# The models volume also holds every model's weights; a prompt cache must not be what fills it.
SAVE_MIN_FREE_BYTES = 20 * 1024**3
# A save's size before it exists, per token: generous (gpt-oss measured ~72 KiB/token with its
# full-history KV; Flash-Next ~20), so the free-space check errs toward skipping.
SAVE_BYTES_PER_TOKEN_ESTIMATE = 80 * 1024
# How long the interactive slot's conversation must sit untouched before the keeper saves it
# on its own. Repurposing the slot saves at once; this catches a conversation the owner walked
# away from before a restart or an engine switch takes it, without rewriting the file after
# every turn of an active one.
CONVERSATION_IDLE_SAVE_S = 600.0

# THE RESTORE GATE (FLASH_NEXT_ENGINE_PLAN F4). On a patch-gated model nothing is restored —
# no role prefix, no conversation — until the debug slot probe has PASSED (the restored slot's
# logits within tolerance, and the save's checkpoint sidecar present) against the server that
# is running now: its launch line and its llama.cpp build (`/props` build_info). The probe
# writes its verdict beside the slot files, keyed by that fingerprint, so a new image or a new
# launch line reads as `awaiting_probe` until someone runs it again. Saves are not gated: they
# cost a disk write, and their files are what a passing probe then makes restorable.
RestoreGate = Literal["awaiting_probe", "passed", "failed"]
GATE_FILE = "restore-gate.json"
# How long a computed gate state is trusted before `/props` and the verdict are read again. A
# probe's own verdict replaces it at once (`forget_gate`); an image swap restarts the server,
# whose new build moves the fingerprint within this window.
GATE_TTL_S = 120.0
# How long an unreadable build (model not resident, `/props` failing) is remembered as unproven.
GATE_UNREADABLE_TTL_S = 15.0


def _slot_int(slot: dict[str, object], key: str) -> int:
    value = slot.get(key)
    return value if isinstance(value, int) else 0


def _prefix_sized(slots: Sequence[dict[str, object]], threshold: int) -> bool:
    """Whether ANY slot holds a cache at least prefix-sized — something that might be the
    prefix, a conversation grown from it, or a large foreign prompt. /slots cannot tell them
    apart (see the module docstring), so this is deliberately a threshold, not an identity."""
    return any(_slot_int(s, "n_prompt_tokens") >= threshold for s in slots)


def _empty_idle(slots: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Idle slots holding NOTHING. Restoring into one destroys no cached history, which is
    what makes it safe regardless of what the other slots are holding."""
    return [s for s in slots if not s.get("is_processing") and _slot_int(s, "n_prompt_tokens") == 0]


def _stable_launch_line(launch_line: str) -> str:
    """The launch line with `--port N` removed.

    The port is the model's INDEX in the installed set (`UPSTREAM_PORT_BASE + i` over
    `local_catalog.selected`), so installing or removing any model renumbers every entry after
    it — and the port changes nothing about what the server computes for a prompt. Hashing it
    meant a routine PWA install orphaned every later model's ~1.1 GB file and charged a full
    prefill to rebuild a byte-identical cache. Everything else on the line stays: `-c`, `-np`,
    the flags and the model path all change what a restored slot would mean."""
    tokens = launch_line.split()
    try:
        i = tokens.index("--port")
    except ValueError:
        return launch_line
    return " ".join(tokens[:i] + tokens[i + 2 :])


def _engine_of_folder(folder: str) -> str:
    """The engine whose budget a model folder under `.kvslots` counts against — its served
    name's catalog engine; a folder outside the catalog is the standard engine's."""
    return str(local_catalog.engine_of(os.path.basename(folder)))


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


def save_dir_for(launch_line: str, models_root: str) -> str | None:
    """Public form of `_save_dir_from_line`, for the slot probe recording its verdict."""
    return _save_dir_from_line(launch_line, models_root)


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
    for part in (_stable_launch_line(launch_line), system, tool_blob, reasoning_effort or ""):
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
        "launch": _short(_stable_launch_line(launch_line)),
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
        "patch_absent",
    }
)

# Outcomes counted as the cache HELPING / NOT HELPING in the owner's summary. Conversation
# misses are not faults (a new conversation has no file), so they are counted, never narrated.
HIT_OUTCOMES = frozenset({"restored", "conversation_restore_hit", "conversation_restore_partial"})
CONVERSATION_MISS_OUTCOMES = frozenset(
    {
        "conversation_no_file",
        "conversation_base_mismatch",
        "conversation_key_mismatch",
        "conversation_restore_miss",
        "conversation_restore_failed",
        "conversation_restore_rejected",
        "conversation_skipped_busy",
        "conversation_skipped_pool_full",
    }
)


def _pool_cells(launch_line: str, pool: KvPool) -> int:
    """The pool size this server actually runs: `-c` off its launch line (the owner can pick a
    larger pool without a release), else the catalog default."""
    tokens = launch_line.split()
    try:
        value = int(tokens[tokens.index("-c") + 1])
    except (ValueError, IndexError):
        return pool.n_ctx
    return value if value > 0 else pool.n_ctx


def gate_fingerprint(launch_line: str, build_info: str) -> str:
    """What a probe verdict is valid for: the launch line (minus `--port`, as everywhere in this
    store) and the engine build. The checkpoint-sidecar patch itself is not visible over HTTP;
    the probe's `sidecar` check proves it at probe time, and every save proves it again."""
    digest = hashlib.sha256()
    for part in (_stable_launch_line(launch_line), build_info):
        digest.update(part.encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:24]


def read_gate_verdict(save_dir: str) -> dict[str, object] | None:
    """Runs in a thread — the last probe verdict beside a model's slot files, or None."""
    try:
        with open(os.path.join(save_dir, GATE_FILE), encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_gate_verdict(save_dir: str, verdict: dict[str, object]) -> bool:
    """Runs in a thread — record a probe verdict atomically beside the model's slot files."""
    path = os.path.join(save_dir, GATE_FILE)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(verdict, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False
    return True


def _by_slot_id(slots: Sequence[dict[str, object]], slot_id: int) -> dict[str, object] | None:
    return next((s for s in slots if isinstance(s, dict) and s.get("id") == slot_id), None)


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
        engine: engines.Engine | engines.ActiveEngine = engines.STANDARD,
        conversations: bool = False,
        pool_guard: kv_pool_guard.KvPoolGuard | None = None,
    ) -> None:
        self._gateway = gateway
        # The router's pool guard, when wired: a restore's fit is judged under its lock and
        # with its pending calls, it charges what a restore put in a never-used slot, and it
        # reports the slots it erases (`note_slot_erased`). None (tests, a DB-less caller)
        # falls back to a local projection off one `/slots` read.
        self._pool_guard = pool_guard
        if pool_guard is not None:
            pool_guard.add_erase_listener(self.note_slot_erased)
        self._models_root = models_root
        # Which engine's llama-swap config launch lines are read from (`_resolve`). The
        # fingerprint is the launch line, so reading the wrong engine's file would describe a
        # server that is not running. Production passes the process's `ActiveEngine`, re-read
        # (TTL-cached) at every async entry point, so a switch made on a live api — the F2
        # debug route, the F3 switch — is picked up without a restart. A fixed engine is for
        # tests and DB-less callers.
        self._engine_source = engine if isinstance(engine, engines.ActiveEngine) else None
        self._engine: engines.Engine = (
            engines.DEFAULT_ENGINE if isinstance(engine, engines.ActiveEngine) else engine
        )
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
        # (served model, role) restored-but-not-yet-used -> (fingerprint, monotonic time): such
        # a slot reports NO n_prompt_tokens (see module docstring), so without this memo every
        # keeper tick would re-restore the same file until the first message. Cleared when a
        # turn uses it, a fresh prime supersedes it, the model reloads, or this process's pool
        # guard erases the slot. A model without a pool has one memo (the interactive key)
        # whatever role asks, as before F4: its slots are not pinned, so two roles' memos would
        # plant the same prefix in two slots. (The worker's guard can erase a pooled slot
        # unseen; the memo then holds until that role's next turn, which pays one prefill.)
        self._restored_unused: dict[tuple[str, SlotRole], tuple[str, float]] = {}
        # One restore at a time: a keeper tick and an inbound turn discovering the same
        # loss must not both stream the file into different slots.
        self._lock = asyncio.Lock()
        # The identity components of the last fingerprint OBSERVED to have a file (a save,
        # or a restore/resolve that found one) — what identity_drift diffs against. Per
        # (served model, role): a research turn's prompt is not jerv's prompt drifting.
        self._last_identity: dict[tuple[str, SlotRole], dict[str, str]] = {}
        # Served models whose running build failed the patch proof (a save wrote no checkpoint
        # sidecar). Out of the disk layer until the api restarts — which every Update does.
        self._patch_absent: set[str] = set()
        # The owner's conversation-cache toggle (settings `llm_kv_conversation_cache`), set at
        # startup and live by the settings route (`configure`).
        self._conversations = conversations
        # Per pooled served model: the conversation its interactive slot holds, as far as this
        # process knows. None/absent = unknown, which is never saved.
        self._conv_hold: dict[str, ConversationHold] = {}
        # Per patch-gated served model: (monotonic read time, gate fingerprint, state).
        self._gate_cache: dict[str, tuple[float, str, RestoreGate]] = {}
        # Every multi-GB slot write, and the prune that follows it, one at a time — and never
        # under `self._lock`, which every turn's restore check takes.
        self._save_lock = asyncio.Lock()
        # Patch-gated models whose patch a save in THIS process life proved. Until then an
        # existing file does not short-circuit a prime's save: the image may have been rebuilt.
        self._patch_seen: set[str] = set()
        # Per pooled served model: a counter bumped by every interactive request's prepare. A
        # turn claims the slot only if no other prepare ran after its own (`note_conversation_
        # turn`), so a concurrent request cannot leave a stale claim behind.
        self._prepare_seq: dict[str, int] = {}
        # Hashes of conversations in which an excluded tool ran: never saved again this process
        # life (the transcript check in the chat path covers restarts).
        self._tainted: set[str] = set()
        # Hashes of conversations whose restores missed MISS_LIMIT times in a row: their file
        # was dropped, and saving them again would only repeat the waste.
        self._unhelpful: set[str] = set()
        # Hashes of conversations forgotten this process life (a deleted or re-scoped chat): a
        # claim on one — even one made by a turn that was still streaming at the delete — is
        # never saved. Checked under `self._save_lock`, which the forget's deletion also takes,
        # so a save either sees the mark or finishes before the files are deleted.
        self._forgotten: set[str] = set()
        # Bumped by every clear of all conversations (the toggle turned off): a claim made
        # before it is never saved.
        self._conv_epoch = 0
        # Background deletions started from synchronous notes, kept so they are not collected.
        self._tasks: set[asyncio.Task[object]] = set()
        # ---- instrumentation (see `snapshot`) ----
        # Every outcome this store reaches, counted since process start. Cheap, unbounded in
        # value but not in keys (one per outcome name), and the only way to tell "the cache
        # is healthy and quiet" from "the cache has not worked since boot" — which read
        # IDENTICALLY on every other surface, because both produce no rows at all.
        self._counters: dict[str, int] = {}
        # The last few outcomes with timestamps, for the debug read. Bounded ring.
        self._events: deque[dict[str, object]] = deque(maxlen=OUTCOME_HISTORY)
        # Per served model, the most recent outcome — what a state read leads with — and per
        # (served model, role) for the pooled roles.
        self._last_outcome: dict[str, dict[str, object]] = {}
        self._last_role_outcome: dict[tuple[str, str], dict[str, object]] = {}
        # (model, outcome) -> monotonic time of the last box_events row, for the rate limit.
        self._box_event_at: dict[tuple[str, str], float] = {}

    # ---- instrumentation ------------------------------------------------------------

    def configure(
        self, *, max_store_bytes: int | None = None, conversations: bool | None = None
    ) -> None:
        """Apply the owner's settings live — the settings and debug routes call this after
        storing them, so neither waits for an api restart."""
        if max_store_bytes is not None:
            self._max_store_bytes = max_store_bytes
        if conversations is not None:
            self._conversations = conversations
            if not conversations:
                # Nothing more is saved, and what was saved goes (`clear_conversations`, which
                # the routes await after this).
                self._conv_hold.clear()
                self._conv_epoch += 1

    # ---- the restore gate ------------------------------------------------------------

    async def restore_gate(self, served_model: str) -> RestoreGate | None:
        """This model's restore gate, or None when it has none (only patch-gated models do).
        Anything unreadable — no launch line, a model not resident, `/props` failing — reads as
        `awaiting_probe`: the conservative state, which saves but never restores."""
        model = local_catalog.get_by_served(served_model)
        if model is None or not model.kv_restore_needs_patch:
            return None
        cached = self._gate_cache.get(served_model)
        if cached is not None and time.monotonic() - cached[0] < GATE_TTL_S:
            return cached[2]
        await self._refresh_engine()
        line = await asyncio.to_thread(
            llama_swap_config.launch_line, self._models_root, served_model, self._current_engine()
        )
        save_dir = None if line is None else _save_dir_from_line(line, self._models_root)
        build: str | None = None
        if line is not None:
            try:
                build = str((await self._gateway.props(served_model)).get("build_info") or "")
            except Exception:  # noqa: BLE001 — unreadable means unproven, never an error
                build = None
        if line is None or save_dir is None or build is None:
            # Unproven. Remembered briefly, so a model that is not resident is not asked for
            # its build on every keeper tick and settings read; a probe clears it at once.
            self._gate_cache[served_model] = (
                time.monotonic() - GATE_TTL_S + GATE_UNREADABLE_TTL_S,
                "",
                "awaiting_probe",
            )
            return "awaiting_probe"
        fingerprint = gate_fingerprint(line, build)
        verdict = await asyncio.to_thread(read_gate_verdict, save_dir)
        state: RestoreGate = "awaiting_probe"
        if verdict is not None and verdict.get("fingerprint") == fingerprint:
            state = "passed" if verdict.get("verdict") == "passed" else "failed"
        self._gate_cache[served_model] = (time.monotonic(), fingerprint, state)
        return state

    def gate_states(self) -> dict[str, RestoreGate]:
        """The last gate state computed per model, without any I/O — for the settings read."""
        return {served: entry[2] for served, entry in sorted(self._gate_cache.items())}

    def forget_gate(self, served_model: str | None = None) -> None:
        """Drop the cached gate state so the next restore reads the verdict afresh — the slot
        probe calls this right after recording one."""
        if served_model is None:
            self._gate_cache.clear()
        else:
            self._gate_cache.pop(served_model, None)

    async def _gate_open(self, served_model: str, role: SlotRole | None) -> bool:
        """Whether restores may run on this model now; counts the ones the gate holds back."""
        state = await self.restore_gate(served_model)
        if state is None or state == "passed":
            return True
        self._count(f"restore_gated_{state}")
        self._last_role_outcome[(served_model, str(role or SlotRole.INTERACTIVE))] = {
            "at": time.time(),
            "model": served_model,
            "outcome": f"restore_gated_{state}",
        }
        return False

    def _count(self, outcome: str) -> None:
        """Count an outcome too frequent or too ordinary for the ring and the log (a held
        conversation is every tool round of every turn)."""
        self._counters[outcome] = self._counters.get(outcome, 0) + 1

    async def _note(
        self,
        outcome: str,
        served_model: str,
        *,
        warn: bool = False,
        role: SlotRole | None = None,
        **fields: object,
    ) -> None:
        """Record one outcome: count it, ring it, log it, and — for a miss — put it on the
        owner's own surface.

        Everything this store does is best-effort, and that used to mean every failure was an
        `info` line on a box whose owner cannot read logs. The counter is the truth (it moves
        on every occurrence); the box_events row is the attention, rate-limited so a repeating
        fault reports once rather than burying the narration it belongs in."""
        self._counters[outcome] = self._counters.get(outcome, 0) + 1
        record: dict[str, object] = {
            "at": time.time(),
            "model": served_model,
            "outcome": outcome,
            **({} if role is None else {"role": str(role)}),
            **fields,
        }
        self._events.append(record)
        self._last_outcome[served_model] = record
        if role is not None:
            self._last_role_outcome[(served_model, str(role))] = record
        extra = {} if role is None else {"role": str(role)}
        if warn:
            log.warning(f"kv_prefix.{outcome}", model=served_model, **extra, **fields)
        else:
            log.info(f"kv_prefix.{outcome}", model=served_model, **extra, **fields)
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
        by_engine: dict[str, int] = {}
        for row in usage[2]:
            engine = str(row["engine"])
            by_engine[engine] = by_engine.get(engine, 0) + int(row["bytes"])  # type: ignore[call-overload]
        models: list[dict[str, object]] = []
        for served_model, system, tools, effort in probes:
            entry: dict[str, object] = {"model": served_model}
            eligible = self._eligible(served_model)
            entry["eligible"] = eligible is not None
            if eligible is None:
                entry["reason"] = self._ineligible_reason(served_model)
            interactive = (served_model, SlotRole.INTERACTIVE)
            entry["restored_unused"] = interactive in self._restored_unused
            entry["last_outcome"] = self._last_outcome.get(served_model)
            if eligible is None:
                # Decided before the launch line is read: a model can be served with a save path
                # for other reasons (Flash-Next's pool needs one for slot erase) and still never
                # save, so a resolved fingerprint here would read as a cold cache, not a refusal.
                entry["state"] = "ineligible"
                models.append(entry)
                continue
            await self._refresh_engine()
            resolved = await asyncio.to_thread(self._resolve, served_model, system, tools, effort)
            if resolved is None:
                entry["state"] = "no_disk_layer"
                models.append(entry)
                continue
            fingerprint, _save_dir, identity = resolved
            entry["fingerprint"] = fingerprint
            entry["prime_tokens"] = self._prime_tokens.get(fingerprint)
            entry["identity"] = identity
            entry["last_known_identity"] = self._last_identity.get(interactive)
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
                    "restored_unused" if interactive in self._restored_unused else "file_present"
                )
            models.append(entry)
        gates: dict[str, RestoreGate] = {}
        for served_model, *_rest in probes:
            gate = await self.restore_gate(served_model)
            if gate is not None:
                gates[served_model] = gate
        counters = dict(sorted(self._counters.items()))
        return {
            "counters": counters,
            "summary": {
                "hits": sum(counters.get(k, 0) for k in HIT_OUTCOMES),
                "misses": sum(
                    counters.get(k, 0) for k in MISS_OUTCOMES | CONVERSATION_MISS_OUTCOMES
                ),
                "role_restores": counters.get("restored", 0),
                "conversation_restores": counters.get("conversation_restored", 0),
                "conversation_misses": sum(counters.get(k, 0) for k in CONVERSATION_MISS_OUTCOMES),
                "conversation_saves": counters.get("conversation_saved", 0),
                "conversation_restore_hits": counters.get("conversation_restore_hit", 0),
                "conversation_restore_partials": counters.get("conversation_restore_partial", 0),
                "conversation_restore_misses": counters.get("conversation_restore_miss", 0),
            },
            "roles": self._role_rows(),
            "conversations": {
                "enabled": self._conversations,
                "held": self._held_rows(),
                "files": [r for r in usage[2] if r.get("kind") == "conversation"],
            },
            "patch_absent": sorted(self._patch_absent),
            "restore_gate": gates,
            "recent": list(self._events),
            "store": {
                "bytes": usage[0],
                "files": usage[1],
                # The budget is per engine: `by_engine` is what each one is held to.
                "budget_bytes": self._max_store_bytes,
                "by_engine": by_engine,
                "over_budget": any(b > self._max_store_bytes for b in by_engine.values()),
                "by_file": usage[2],
            },
            "models": models,
        }

    def _role_rows(self) -> list[dict[str, object]]:
        """Every (model, role) this store has state for: its memo, last outcome and identity."""
        keys = {(m, str(r)) for m, r in self._restored_unused} | {
            (m, str(r)) for m, r in self._last_identity
        }
        keys |= set(self._last_role_outcome)
        now = time.monotonic()
        rows: list[dict[str, object]] = []
        for served, role_name in sorted(keys):
            role = SlotRole(role_name)
            pool = local_catalog.pool_of(served)
            memo = self._restored_unused.get((served, role))
            rows.append(
                {
                    "model": served,
                    "role": role_name,
                    "slot": pool.slot(role) if pool is not None else None,
                    "restored_unused": memo is not None,
                    "restored_age_s": None if memo is None else round(now - memo[1]),
                    "last_identity": self._last_identity.get((served, role)),
                    "last_outcome": self._last_role_outcome.get((served, role_name)),
                }
            )
        return rows

    def _held_rows(self) -> list[dict[str, object]]:
        now = time.monotonic()
        return [
            {
                "model": served,
                "conversation": kv_conversation.short_key(hold.key),
                "awaiting_judgement": hold.restored_tokens is not None,
                "input_tokens": hold.input_tokens,
                "unsaved": hold.dirty,
                "idle_s": round(now - hold.at),
            }
            for served, hold in sorted(self._conv_hold.items())
        ]

    def _ineligible_reason(self, served_model: str) -> str:
        """Why `_eligible` said no — the difference between "this model will never use the
        disk layer" and "the owner's patch setting is off", which look the same from outside
        and have completely different remedies."""
        model = local_catalog.get_by_served(served_model)
        if model is None:
            return "not a catalog model"
        if model.kv_restore_needs_patch and served_model in self._patch_absent:
            return (
                "a save wrote no checkpoint sidecar: the running engine lacks the "
                "checkpoint-sidecar patch (Ops → Update rebuilds it)"
            )
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
                    with contextlib.suppress(OSError):
                        size += os.stat(path + _META_EXT).st_size
                    total += size
                    rows.append(
                        {
                            "model": os.path.basename(folder),
                            "engine": _engine_of_folder(folder),
                            "fingerprint": name[: -len(_SLOT_FILE_SUFFIX)],
                            "kind": (
                                "conversation"
                                if kv_conversation.is_conversation_file(name)
                                else "prefix"
                            ),
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
        # A patch-gated entry (Flash-Next): admitted until a save proves its engine was built
        # without the checkpoint sidecar, which restores never reach first — a restore needs
        # the sidecar a patched save wrote (`_restore_into_role_slot`).
        if model.kv_restore_needs_patch:
            return None if served_model in self._patch_absent else model
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

    @staticmethod
    def _memo_key(served_model: str, role: SlotRole | None) -> tuple[str, SlotRole]:
        """The restored-but-unused memo's key: per role on a pooled model, one per model
        otherwise (see `_restored_unused`)."""
        if role is None or local_catalog.pool_of(served_model) is None:
            return (served_model, SlotRole.INTERACTIVE)
        return (served_model, role)

    @staticmethod
    def _identity_key(served_model: str, role: SlotRole | None) -> tuple[str, SlotRole]:
        return (served_model, role or SlotRole.INTERACTIVE)

    def _memo_active(self, key: tuple[str, SlotRole]) -> bool:
        return key in self._restored_unused

    def note_slot_erased(self, served_model: str, slot: int) -> None:
        """The pool guard erased `slot`: whatever was restored there is gone. Drops that role's
        memo, and the conversation claim when it is the interactive slot."""
        pool = local_catalog.pool_of(served_model)
        if pool is None or not 0 <= slot < pool.n_slots:
            return
        role = pool.by_slot(slot).role
        self._restored_unused.pop((served_model, role), None)
        if role is SlotRole.INTERACTIVE:
            self._conv_hold.pop(served_model, None)

    def set_engine(self, engine: engines.Engine) -> None:
        """Pin launch-line resolution to `engine`'s config, for a store built without a live
        source (a source, when given, always wins)."""
        self._engine = engine

    async def _refresh_engine(self) -> None:
        """Re-read the active engine (TTL-cached) before a resolve."""
        if self._engine_source is not None:
            await self._engine_source.get()

    def _current_engine(self) -> engines.Engine:
        """The engine `_resolve` reads. With a live source, its latest value — refreshed by
        this store's async entry points and by every other reader sharing the cache (residency
        reads it on each local load), so sync `identity_of` is never more than one TTL behind."""
        if self._engine_source is not None:
            return self._engine_source.last_known()
        return self._engine

    def _resolve_line(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> tuple[str, str, dict[str, str], str] | None:
        """(fingerprint, save-dir, identity components, launch line) for the CURRENT launch
        line, or None when the model is not served, or is served without --slot-save-path
        (no disk layer). One read of the rendered config feeds all four, so they can never
        describe two different servers."""
        line = llama_swap_config.launch_line(
            self._models_root, served_model, self._current_engine()
        )
        if line is None:
            return None
        save_dir = _save_dir_from_line(line, self._models_root)
        if save_dir is None:
            return None
        return (
            _fingerprint(line, system, tools, reasoning_effort),
            save_dir,
            _identity_components(line, system, tools, reasoning_effort),
            line,
        )

    def _resolve(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> tuple[str, str, dict[str, str]] | None:
        """`_resolve_line` without the line."""
        resolved = self._resolve_line(served_model, system, tools, reasoning_effort)
        return None if resolved is None else resolved[:3]

    def note_prefix_lost(self, served_model: str) -> None:
        """The slot this store restored into is gone — an eviction, an operator unload, or a
        bare restore-load. Registered with the residency coordinator beside the keeper's hook.

        Without it `_restored_unused` is a belief nothing can correct: it is set by a restore
        and cleared only by a turn that USES that restore or by a fresh prime, so a model
        evicted in between leaves the memo set forever. `restore_if_lost` then returns False
        at its first line — before it reads `/slots` at all — and the next jerv turn pays the
        full ~125 s prefill this store exists to prevent, with a valid file sitting on disk
        unread. Residency already reported this; only the keeper was listening.

        Every role's memo goes, and so does the conversation the interactive slot held: an
        unsaved one is lost with the slot, and claiming it afterwards would save a stranger's
        cache under its name."""
        for key in [k for k in self._restored_unused if k[0] == served_model]:
            del self._restored_unused[key]
        self._conv_hold.pop(served_model, None)
        # A reload may be a new image or a resized pool: the gate is read afresh.
        self.forget_gate(served_model)
        if self._pool_guard is not None:
            self._pool_guard.forget_restored(served_model)

    def note_agent_turn(
        self,
        served_model: str,
        input_tokens: int,
        *,
        fingerprint: str | None = None,
        role: SlotRole | None = None,
    ) -> None:
        """A turn completed — if it was the turn our restore was FOR, that restore has now
        been used, and the slot it grew reports a prefix-sized cache on its own from here on.

        The identity check is the point. `agent.turn` is not an interactive lane: the daily
        briefing, deep research and every spawned sub-agent run under the same task name with
        a different system prompt and tool set. Clearing on any of them retired a restore the
        owner's turn had not consumed yet, so the next keeper tick believed a slot was primed
        that nothing had used — and `restore_if_lost` returns False on that belief before it
        reads /slots at all. A caller that cannot name the identity (`fingerprint=None`)
        clears nothing, which is the safe direction: a stale memo costs one restore, an
        early-cleared one costs a full prefill."""
        if input_tokens <= 0:
            return
        self.note_prefix_used(served_model, fingerprint, role=role)

    def note_prefix_used(
        self, served_model: str, fingerprint: str | None, *, role: SlotRole | None = None
    ) -> None:
        """The restore for `fingerprint` has been consumed by a request, so the slot now
        reports its own size and the memo has done its job.

        Separate from `note_agent_turn` because a turn is not the only way a slot gets used:
        a stream the owner STOPS mid-answer never produces a final `LlmTurn`, so every
        post-turn statement in the router is skipped — but the prompt was sent and the slot
        was grown all the same. Left set, the memo makes `restore_if_lost` decline for the
        rest of the process's life, and the next turn pays a full prefill."""
        if fingerprint is None:
            return
        key = self._memo_key(served_model, role)
        memo = self._restored_unused.get(key)
        if memo is not None and memo[0] == fingerprint:
            del self._restored_unused[key]

    def identity_of(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> str | None:
        """The fingerprint a turn with this identity would look for, or None when this model
        has no disk layer. Lets a caller name the identity it just ran without reaching into
        `_resolve` — the router needs it to close `note_agent_turn` above. None for a model
        this store never saves, whatever its launch line carries, so nothing hashes a prefix
        per turn for a cache that cannot exist."""
        if self._eligible(served_model) is None:
            return None
        resolved = self._resolve(served_model, system, tools, reasoning_effort)
        return None if resolved is None else resolved[0]

    # ---- save -----------------------------------------------------------------------

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        prime_tokens: int,
        *,
        reasoning_effort: str | None = None,
        role: SlotRole | None = None,
    ) -> bool:
        """Persist the freshly primed slot, called by the keeper in the same breath as a
        successful prime. Returns True when a valid file exists afterwards (already
        present counts). Best-effort: every failure is a log line, never an exception.

        On a pooled model only `role`'s own slot is a candidate (the interactive one when no
        role is named): the prime was pinned there, and an equal count in another role's
        slot is a coincidence, not the prime."""
        model = self._eligible(served_model)
        if model is None or prime_tokens < MIN_PREFIX_TOKENS:
            return False
        pool = local_catalog.pool_of(served_model)
        if pool is not None:
            role = role or SlotRole.INTERACTIVE
        # A fresh prime supersedes any restored-but-unused state.
        self._restored_unused.pop(self._memo_key(served_model, role), None)
        await self._refresh_engine()
        resolved = await asyncio.to_thread(
            self._resolve, served_model, system, tools, reasoning_effort
        )
        if resolved is None:
            return False
        fingerprint, save_dir, identity = resolved
        self._prime_tokens[fingerprint] = prime_tokens
        self._last_identity[self._identity_key(served_model, role)] = identity
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
            # A patch-gated model's file also needs THIS process to have proven the patch: the
            # api restarts with every image rebuild, and a stock rebuild would otherwise keep
            # trusting files a patched build wrote.
            proven = not model.kv_restore_needs_patch or served_model in self._patch_seen
            has_sidecar = await asyncio.to_thread(os.path.exists, path + _SIDECAR_EXT)
            if proven and (not model.recurrent or has_sidecar):
                await asyncio.to_thread(self._touch, path)
                return True
            await self._note("resaving_for_sidecar", served_model, role=role)
        try:
            slots = await self._gateway.slots(served_model)
        except LocalGatewayError as exc:
            await self._note(
                "slots_unreadable", served_model, role=role, phase="save", error=str(exc)
            )
            return False
        # EXACT match works here only because the prime generates exactly ONE token: the
        # server appends every sampled token to the slot's cache EXCEPT the final stop
        # token, so a max_tokens=1 prime leaves the cache at precisely its prompt size.
        # A prime that ever generated more would never match — which fails SAFE (skip +
        # log), but silently: if `slot_unidentified` becomes chronic, look here first.
        wanted = None if pool is None or role is None else pool.slot(role)
        matches = [
            s
            for s in slots
            if isinstance(s, dict)
            and s.get("n_prompt_tokens") == prime_tokens
            and not s.get("is_processing")
            and (wanted is None or s.get("id") == wanted)
        ]
        if len(matches) != 1:
            # Zero: something replaced the prime between the converse returning and this
            # read — the exact race v1 lost by saving anyway. More than one: ambiguous.
            await self._note(
                "slot_unidentified",
                served_model,
                role=role,
                expected_tokens=prime_tokens,
                candidates=len(matches),
            )
            return False
        slot_id = _slot_int(matches[0], "id")
        async with self._save_lock:
            return await self._save_prime_locked(
                model, served_model, slot_id, fingerprint, path, prime_tokens, role
            )

    async def _save_prime_locked(
        self,
        model: local_catalog.LocalModel,
        served_model: str,
        slot_id: int,
        fingerprint: str,
        path: str,
        prime_tokens: int,
        role: SlotRole | None,
    ) -> bool:
        """`save_after_prime`'s write, under `self._save_lock`."""
        if not await self._disk_room(path, prime_tokens, served_model):
            return False
        # The sidecar left by an earlier save would otherwise "prove" a build that writes none.
        await asyncio.to_thread(self._remove_sidecar, path)
        try:
            resp = await self._gateway.save_slot(
                served_model, slot_id, f"{fingerprint}{_SLOT_FILE_SUFFIX}"
            )
        except LocalGatewayError as exc:
            # A failed or timed-out save can leave a PARTIAL file at the trusted name —
            # and an existing file short-circuits every future save while restores keep
            # reading junk. Remove whatever landed, or this fingerprint is poisoned until
            # a config change happens to move it.
            await self._note("save_failed", served_model, role=role, error=str(exc))
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
                role=role,
                expected=prime_tokens,
                n_saved=n_saved,
            )
            await asyncio.to_thread(self._remove_quietly, path)
            return False
        if not await self._patch_proven(model, served_model, path, role):
            return False
        # An eviction used to be the one thing this store did that left NO trace at all —
        # not even an info line. A config the operator flips back to next month will have
        # been pruned silently, and its re-prefill then reads as a new fault.
        await self._prune_and_note(served_model, path)
        await box_events.record(
            box_events.KV_PREFIX_SAVED,
            served_model,
            detail=f"{prime_tokens}-token jerv prefix saved to disk",
        )
        await self._note("saved", served_model, role=role, tokens=prime_tokens, slot=slot_id)
        return True

    async def _patch_proven(
        self,
        model: local_catalog.LocalModel,
        served_model: str,
        path: str,
        role: SlotRole | None,
    ) -> bool:
        """For a patch-gated model, whether the save that just wrote `path` also wrote its
        checkpoint sidecar. The patched server writes one on every save, even with no
        checkpoints, so a missing sidecar means the running build is stock: the file is
        removed and the model leaves the disk layer, because every restore of it would
        re-prefill from zero while reading as a success."""
        if not model.kv_restore_needs_patch:
            return True
        if await asyncio.to_thread(os.path.exists, path + _SIDECAR_EXT):
            self._patch_seen.add(served_model)
            return True
        self._patch_absent.add(served_model)
        await asyncio.to_thread(self._remove_quietly, path)
        await self._note("patch_absent", served_model, warn=True, role=role)
        return False

    async def _disk_room(self, path: str, tokens: int, served_model: str) -> bool:
        """Whether the volume can take this save and keep `SAVE_MIN_FREE_BYTES` free."""
        estimate = max(tokens, 1) * SAVE_BYTES_PER_TOKEN_ESTIMATE
        stat = await asyncio.to_thread(self._stat_quietly, path)
        if stat is not None:
            estimate = stat[0]  # rewriting a file: its own size is the better guess
        try:
            free = await asyncio.to_thread(_free_bytes, os.path.dirname(path))
        except OSError:
            return True  # unknown is not full; the save's own failure is still caught
        if free >= max(2 * estimate, SAVE_MIN_FREE_BYTES):
            return True
        await self._note("save_skipped_low_disk", served_model, free=free, estimate=estimate)
        return False

    def _remove_sidecar(self, path: str) -> None:
        """Runs in a thread."""
        with contextlib.suppress(OSError):
            os.remove(path + _SIDECAR_EXT)

    async def _prune_and_note(self, served_model: str, keep_path: str) -> None:
        for gone, size in await asyncio.to_thread(self._prune_to_budget, keep_path):
            await self._note("evicted", served_model, file=os.path.basename(gone), bytes=size)

    async def clear(self, served_model: str | None = None) -> dict[str, object]:
        """Delete this store's files — all of them, or one model's. Returns what went.

        The no-terminal twin of `rm -rf .kvslots` (CLAUDE.md #10). It exists for the same
        reason `drop-page-cache` does: the only way to reclaim this space was host shell,
        which the owner running this box remotely does not have — and the budget's own
        comment conceded it ("changing it is a release, there is no knob") while the store
        sat at 94% of it. Safe at any time: a deleted file costs at most one re-prefill,
        which is the behaviour without this store at all, and the next prime writes it back.

        In-memory state goes with the files: a `_prime_tokens` entry for a file that no
        longer exists would have the next restore verify against a count nothing can match.
        Conversation files go too, and with them the claim on whatever the slot holds."""
        model = local_catalog.get_by_served(served_model) if served_model else None
        only = model.id if model is not None else served_model
        async with self._save_lock:
            removed = await asyncio.to_thread(self._clear_files, only)
        if served_model is None:
            self._prime_tokens.clear()
            self._restored_unused.clear()
            self._last_identity.clear()
            self._conv_hold.clear()
        else:
            for key in [k for k in self._restored_unused if k[0] == served_model]:
                del self._restored_unused[key]
            for key in [k for k in self._last_identity if k[0] == served_model]:
                del self._last_identity[key]
            self._conv_hold.pop(served_model, None)
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
                for part in (path, path + _SIDECAR_EXT, path + _META_EXT):
                    with contextlib.suppress(OSError):
                        freed += os.stat(part).st_size
                self._remove_quietly(path)
                fingerprints.append(name[: -len(_SLOT_FILE_SUFFIX)])
        return (freed, fingerprints)

    def _remove_quietly(self, path: str) -> None:
        """Runs in a thread — delete a file that is now known-bad, tolerating absence.

        The patched engine writes a checkpoint sidecar beside each slot file
        (deploy/patches/0001), and a conversation file carries its claim beside it; the set
        lives and dies together — a bad slot file's sidecar restores checkpoints for state
        that no longer exists, and an orphaned claim describes a file that is gone."""
        for part in (path, path + _SIDECAR_EXT, path + _META_EXT):
            with contextlib.suppress(OSError):
                os.remove(part)

    def _touch(self, path: str) -> None:
        """Runs in a thread — bump the file's mtime, the LRU clock a restore refreshes."""
        with contextlib.suppress(OSError):
            os.utime(path, None)

    def _prune_to_budget(self, keep_path: str) -> list[tuple[str, int]]:
        """Runs in a thread (asyncio.to_thread) — plain blocking fs on purpose.

        Hold each ENGINE's share of the `.kvslots` tree at or under the store's byte budget
        (owner, 2026-10-05: Flash-Next gets its own allowance, so the standard engine's
        parked prefixes and its never compete) by deleting least-recently-used files (oldest
        mtime first), across that engine's model folders — every CONVERSATION file before
        any role prefix, because a prefix is what each
        restart and engine switch needs back, and a conversation is one chat's convenience.
        The just-saved file is never a candidate, whatever its mtime — deleting the thing the
        save just verified would turn a full store into a store that forgets its newest
        state. Files an operator parked outside the standard tree (a --slot-save-path
        override) are simply not this budget's to manage.

        The keep-path guard protects only THIS process's save: today that is sound
        because exactly one KvPrefixStore exists (the api's — the worker wires none), but
        a second store pruning concurrently could evict the first's fresh file. If a
        store ever grows into another process, this needs a cross-process story first."""
        root = os.path.join(self._models_root, llama_swap_config.KVSLOT_DIR)
        entries: dict[str, list[tuple[bool, float, int, str]]] = {}
        totals: dict[str, int] = {}
        for folder, _dirs, names in os.walk(root):
            engine = _engine_of_folder(folder)
            for name in names:
                for ext in (_SIDECAR_EXT, _META_EXT):
                    if name.endswith(_SLOT_FILE_SUFFIX + ext):
                        # A sidecar or claim whose slot file is gone (a crash between the
                        # paired removes) is unusable and otherwise invisible to this budget.
                        if name[: -len(ext)] not in names:
                            with contextlib.suppress(OSError):
                                os.remove(os.path.join(folder, name))
                        break
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
                    for ext in (_SIDECAR_EXT, _META_EXT):
                        with contextlib.suppress(OSError):
                            size += os.stat(path + ext).st_size
                    totals[engine] = totals.get(engine, 0) + size
                    if os.path.abspath(path) != os.path.abspath(keep_path):
                        pinned = not kv_conversation.is_conversation_file(name)
                        entries.setdefault(engine, []).append((pinned, stat.st_mtime, size, path))
        evicted: list[tuple[str, int]] = []
        for engine, candidates in entries.items():
            total = totals[engine]
            for _pinned, _mtime, size, path in sorted(candidates):
                if total <= self._max_store_bytes:
                    break
                with contextlib.suppress(OSError):
                    os.remove(path)
                    for ext in (_SIDECAR_EXT, _META_EXT):
                        with contextlib.suppress(OSError):
                            os.remove(path + ext)
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
        role: SlotRole | None = None,
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
        turn or a fresh prime supersedes it.

        On a pooled model the gate is per slot: `role`'s own slot (the interactive one when
        none is named), restored into only while it holds nothing."""
        if self._eligible(served_model) is None:
            return False
        # The lock opens HERE, not at the slots read. It used to sit below the memo check and
        # the file check, so two callers discovering the same loss both passed those, then
        # serialized and both restored — two multi-GB streams, and the prefix planted in BOTH
        # slots, including the one whose separation from the interactive slot is the entire
        # point of a second slot. Reproduced; the comment on `self._lock` always claimed this.
        async with self._lock:
            return await self._restore_locked(served_model, system, tools, reasoning_effort, role)

    async def _restore_locked(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
        role: SlotRole | None = None,
    ) -> bool:
        """`restore_if_lost`'s body, with `self._lock` held. Split out only so the lock has
        one acquisition point and cannot be taken twice on one path."""
        pool = local_catalog.pool_of(served_model)
        if pool is not None:
            role = role or SlotRole.INTERACTIVE
        if self._memo_active(self._memo_key(served_model, role)):
            return False  # already restored; the slot reports nothing until a turn uses it
        await self._refresh_engine()
        resolved = await asyncio.to_thread(
            self._resolve_line, served_model, system, tools, reasoning_effort
        )
        if resolved is None:
            return False
        fingerprint, save_dir, identity, line = resolved
        path = os.path.join(save_dir, f"{fingerprint}{_SLOT_FILE_SUFFIX}")
        identity_key = self._identity_key(served_model, role)
        if not await asyncio.to_thread(os.path.exists, path):
            # Say WHY there is no file for this identity: when a file was known under a
            # different identity, name the component that moved — the 2026-08-24 canvas
            # case (a 204 s unhealed re-prefill) had no way to tell a tool-set flap from
            # a race. `effort` prints its value; the rest print short digests.
            last = self._last_identity.get(identity_key)
            if last is not None and last != identity:
                await self._note(
                    "identity_drift",
                    served_model,
                    role=role,
                    changed=sorted(k for k in identity if identity[k] != last.get(k)),
                    now=identity,
                    was=last,
                )
            return False
        self._last_identity[identity_key] = identity
        if pool is not None and role is not None:
            return await self._restore_into_role_slot(
                served_model, pool, role, fingerprint, path, line
            )
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
        # An EMPTY idle slot is safe to restore into no matter what the other slots hold:
        # writing there destroys nothing. The guard below is about not overwriting cached
        # history, and it used to be `any()` across ALL slots — a single-slot argument applied
        # to a multi-slot server, so the store declined to fill an empty slot because a
        # DIFFERENT slot was busy with something large, then touched the file so the box read
        # as healthy. On a two-slot box a long background conversation in the other slot
        # blocked every restore for as long as that cache lived.
        if _prefix_sized(occupied, threshold) and not _empty_idle(occupied):
            # Something prefix-sized is cached and there is nowhere free to land — never
            # restore over it. This branch is ALSO the only one a healthy hot config ever
            # reaches (the keeper's settled tick lands here every minute), so it must refresh
            # the LRU clock: without this touch the hottest config's file keeps its boot-time
            # mtime and is the FIRST out of the budget (adversarial review, 2026-08-23).
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
                if _prefix_sized(occupied, threshold) and not _empty_idle(occupied):
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
        return await self._restore_file(
            served_model, _slot_int(target, "id"), fingerprint, path, role=None
        )

    async def _restore_into_role_slot(
        self,
        served_model: str,
        pool: KvPool,
        role: SlotRole,
        fingerprint: str,
        path: str,
        line: str,
    ) -> bool:
        """The pooled restore: `role`'s own slot, only while it is idle and EMPTY, and only
        when the restored prefix fits the pool beside what every other slot may grow to.

        Never over an occupied slot, whatever it holds — on a pool each role's traffic is
        pinned to its slot, so anything there is that role's own live cache. Never past the
        pool either: llama-server answers a full pool by failing every busy request."""
        if not await self._gate_open(served_model, role):
            return False
        if not await asyncio.to_thread(os.path.exists, path + _SIDECAR_EXT):
            # A hybrid restore with no checkpoints re-prefills from zero (Discussion #27950);
            # the next prime's save writes the sidecar (`save_after_prime`).
            await self._note("restore_skipped_no_sidecar", served_model, role=role)
            return False
        slot_id = pool.slot(role)
        target: dict[str, object] | None = None
        slots: list[dict[str, object]] = []
        for attempt in range(RESTORE_BUSY_POLLS + 1):
            if attempt:
                await asyncio.sleep(RESTORE_BUSY_INTERVAL_S)
            try:
                slots = [s for s in await self._gateway.slots(served_model) if isinstance(s, dict)]
            except LocalGatewayError as exc:
                await self._note(
                    "slots_unreadable", served_model, role=role, phase="restore", error=str(exc)
                )
                return False
            if not layout_matches(pool, slots):
                # A server still on another layout wraps the id onto some other slot.
                await self._note(
                    "slot_layout_mismatch", served_model, role=role, live_slots=len(slots)
                )
                return False
            target = _by_slot_id(slots, slot_id)
            if target is None or not target.get("is_processing"):
                break
        else:
            await self._note("restore_skipped_busy", served_model, role=role, slot=slot_id)
            return False
        if target is None:
            return False
        if _slot_int(target, "n_prompt_tokens") > 0:
            # Occupied: the role's own cache, never overwritten. A healthy primed slot lands
            # here every keeper tick, so this is also where its file's LRU clock is kept.
            await asyncio.to_thread(self._touch, path)
            return False
        need = self._prime_tokens.get(fingerprint) or await asyncio.to_thread(
            _file_token_bound, path
        )
        reserved, ticket = (
            (False, None)
            if need is None
            else await self._reserve(served_model, pool, slot_id, need, slots, line)
        )
        if not reserved:
            await self._note("restore_skipped_pool_full", served_model, role=role, need=need)
            return False
        try:
            return await self._restore_file(served_model, slot_id, fingerprint, path, role=role)
        finally:
            self._end_reserve(served_model, slot_id, ticket)

    async def _reserve(
        self,
        served_model: str,
        pool: KvPool,
        slot_id: int,
        need: int,
        slots: Sequence[dict[str, object]],
        line: str,
    ) -> tuple[bool, int | None]:
        """Whether `need` restored cells fit in `slot_id`, and — through the pool guard, when
        wired — the cells held as pending from that same locked decision until `_end_reserve`
        (its lock, its pending calls, its charge for never-used restored slots). Without a
        guard: off the `/slots` read in hand and the pool size on the launch line."""
        if self._pool_guard is not None:
            ticket = await self._pool_guard.reserve_restore(served_model, pool, slot_id, need)
            return ticket is not None, ticket
        cells = _pool_cells(line, pool)
        fits = kv_pool_guard.projected_cells(pool, slots, exclude=slot_id) + need <= cells
        return fits, None

    def _end_reserve(self, served_model: str, slot_id: int, ticket: int | None) -> None:
        if self._pool_guard is not None and ticket is not None:
            self._pool_guard.end_restore(served_model, slot_id, ticket)

    async def _restore_file(
        self,
        served_model: str,
        slot_id: int,
        fingerprint: str,
        path: str,
        *,
        role: SlotRole | None,
    ) -> bool:
        """Restore `path` into `slot_id` and verify what came back."""
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
            await self._note("restore_failed", served_model, role=role, error=str(exc))
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
                role=role,
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
        self._restored_unused[self._memo_key(served_model, role)] = (
            fingerprint,
            time.monotonic(),
        )
        if self._pool_guard is not None and role is not None:
            self._pool_guard.note_restored(served_model, slot_id, n_restored)
        await box_events.record(
            box_events.KV_PREFIX_RESTORED,
            served_model,
            detail=f"{n_restored}-token jerv prefix restored from disk in {elapsed_ms} ms",
        )
        await self._note(
            "restored",
            served_model,
            role=role,
            tokens=n_restored,
            slot=slot_id,
            elapsed_ms=elapsed_ms,
        )
        return True

    # ---- conversations (pooled interactive slot) ----------------------------------------

    def _conversation_pool(self, served_model: str) -> KvPool | None:
        """The pool whose interactive slot carries conversation files, or None when the
        feature does not apply: off, no pool, or the model is out of the disk layer."""
        if not self._conversations:
            return None
        pool = local_catalog.pool_of(served_model)
        if pool is None or self._eligible(served_model) is None:
            return None
        return pool

    async def prepare_conversation(
        self,
        served_model: str,
        conversation_key: str | None,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> tuple[bool, int]:
        """Before an interactive request on a pooled model: save the conversation the slot
        holds if this request is about to repurpose it, then restore this request's own
        conversation if a file for it (same key, same base identity) is on disk. Returns
        (restored, sequence): on True the caller skips the persona restore, which would see the
        never-used slot as empty and overwrite it; the sequence goes back with the turn's note.

        A request with no conversation (the keeper's prime, an omnibox turn, a chat that may
        not reach disk) repurposes the slot too, so it saves the holder and restores nothing.
        The save runs outside `self._lock` (under `self._save_lock`): it streams gigabytes,
        and every other turn's restore check takes the main lock. Best-effort throughout."""
        seq = self._prepare_seq.get(served_model, 0) + 1
        self._prepare_seq[served_model] = seq
        pool = self._conversation_pool(served_model)
        if pool is None:
            return False, seq
        if conversation_key is not None and kv_conversation.key_hash(conversation_key) in (
            self._tainted | self._forgotten
        ):
            conversation_key = None
        async with self._lock:
            hold = self._conv_hold.get(served_model)
            if hold is not None and conversation_key is not None and hold.key == conversation_key:
                self._count("conversation_held")
                return False, seq
            if hold is None and conversation_key is None:
                return False, seq
            await self._refresh_engine()
            resolved = await asyncio.to_thread(
                self._resolve_line, served_model, system, tools, reasoning_effort
            )
            if resolved is None:
                return False, seq
            base, save_dir, _identity, line = resolved
            # Whatever happens next, this request replaces what the slot held.
            self._conv_hold.pop(served_model, None)
        if hold is not None and hold.dirty:
            async with self._save_lock:
                await self._save_conversation(served_model, pool, hold)
        if conversation_key is None:
            return False, seq
        async with self._lock:
            if self._prepare_seq.get(served_model) != seq:
                return False, seq  # another request is already taking the slot
            restored = await self._restore_conversation(
                served_model, pool, conversation_key, base, save_dir, line
            )
        return restored, seq

    async def _restore_conversation(
        self,
        served_model: str,
        pool: KvPool,
        conversation_key: str,
        base: str,
        save_dir: str,
        line: str,
    ) -> bool:
        name = kv_conversation.file_name(base, conversation_key)
        path = os.path.join(save_dir, name)
        meta = await asyncio.to_thread(_read_meta, path)
        decision = kv_conversation.restore_decision(meta, base, conversation_key)
        if decision != "restore" or meta is None:
            self._count(f"conversation_{decision}")
            return False
        if not await asyncio.to_thread(os.path.exists, path + _SIDECAR_EXT):
            self._count("conversation_skipped_no_sidecar")
            return False
        if not await self._gate_open(served_model, SlotRole.INTERACTIVE):
            return False
        slot_id = pool.slot(SlotRole.INTERACTIVE)
        try:
            slots = [s for s in await self._gateway.slots(served_model) if isinstance(s, dict)]
        except LocalGatewayError as exc:
            await self._note("slots_unreadable", served_model, phase="conversation", error=str(exc))
            return False
        target = _by_slot_id(slots, slot_id) if layout_matches(pool, slots) else None
        if target is None or target.get("is_processing"):
            # A busy interactive slot is another interactive request; this one queues behind
            # it and overwrites whatever it leaves, so a restore now would be wasted.
            self._count("conversation_skipped_busy")
            return False
        reserved, ticket = await self._reserve(
            served_model, pool, slot_id, meta.n_tokens, slots, line
        )
        if not reserved:
            self._count("conversation_skipped_pool_full")
            return False
        try:
            return await self._restore_conversation_file(
                served_model, conversation_key, base, path, name, meta, slot_id
            )
        finally:
            self._end_reserve(served_model, slot_id, ticket)

    async def _restore_conversation_file(
        self,
        served_model: str,
        conversation_key: str,
        base: str,
        path: str,
        name: str,
        meta: ConversationMeta,
        slot_id: int,
    ) -> bool:
        """The conversation restore itself, with its pool cells already reserved."""
        started = time.perf_counter()
        try:
            resp = await self._gateway.restore_slot(served_model, slot_id, name)
        except LocalGatewayError as exc:
            # llama-server words a full KV cache and a bad file the same way ("No available
            # space in KV cache or invalid slot save file"), so the error text cannot say which.
            # Count it as a miss: a file that keeps failing goes after MISS_LIMIT in a row, one
            # that failed on a momentarily full pool survives.
            await self._note(
                "conversation_restore_failed",
                served_model,
                role=SlotRole.INTERACTIVE,
                error=str(exc),
            )
            # The claim's read-modify-write, under the lock every other writer of it holds.
            # Taken inside `self._lock` here; nothing ever takes `self._lock` while holding the
            # save lock, so the order cannot deadlock.
            async with self._save_lock:
                await asyncio.to_thread(_bump_misses, path)
            return False
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        n_restored = resp.get("n_restored")
        if n_restored != meta.n_tokens or not isinstance(n_restored, int):
            await self._note(
                "conversation_restore_rejected",
                served_model,
                warn=True,
                role=SlotRole.INTERACTIVE,
                n_restored=n_restored,
                expected=meta.n_tokens,
            )
            await asyncio.to_thread(self._remove_quietly, path)
            return False
        await asyncio.to_thread(self._touch, path)
        # Stands in for the never-used slot's missing size, so the keeper's prefix restore
        # does not overwrite it; the turn that uses it retires it (`note_agent_turn`).
        self._restored_unused[self._memo_key(served_model, SlotRole.INTERACTIVE)] = (
            base,
            time.monotonic(),
        )
        if self._pool_guard is not None:
            self._pool_guard.note_restored(served_model, slot_id, n_restored)
        self._conv_hold[served_model] = ConversationHold(
            key=conversation_key,
            base=base,
            input_tokens=None,
            output_tokens=None,
            dirty=False,
            at=time.monotonic(),
            restored_tokens=n_restored,
            epoch=self._conv_epoch,
        )
        await box_events.record(
            box_events.KV_PREFIX_RESTORED,
            served_model,
            detail=f"{n_restored}-token conversation restored from disk in {elapsed_ms} ms",
        )
        await self._note(
            "conversation_restored",
            served_model,
            role=SlotRole.INTERACTIVE,
            tokens=n_restored,
            slot=slot_id,
            elapsed_ms=elapsed_ms,
        )
        return True

    async def _save_conversation(
        self, served_model: str, pool: KvPool, hold: ConversationHold
    ) -> Literal["saved", "busy", "failed"]:
        """Save the interactive slot as `hold`'s conversation — only when `/slots`, read just
        before, still shows that conversation's cache (see `ConversationHold.still_in_slot`)
        and the server saves exactly that many tokens. Under `self._save_lock`, never the main
        lock. `busy` means try again later; `failed` means the claim is no longer good."""
        digest = kv_conversation.key_hash(hold.key)
        if not self._hold_current(hold):
            return "failed"
        await self._refresh_engine()
        line = await asyncio.to_thread(
            llama_swap_config.launch_line, self._models_root, served_model, self._current_engine()
        )
        save_dir = None if line is None else _save_dir_from_line(line, self._models_root)
        if save_dir is None:
            return "failed"
        slot_id = pool.slot(SlotRole.INTERACTIVE)
        try:
            slots = [s for s in await self._gateway.slots(served_model) if isinstance(s, dict)]
        except LocalGatewayError as exc:
            await self._note("slots_unreadable", served_model, phase="conversation", error=str(exc))
            return "busy"
        target = _by_slot_id(slots, slot_id) if layout_matches(pool, slots) else None
        if target is None or target.get("is_processing"):
            self._count("conversation_save_skipped_busy")
            return "busy"
        n_slot = _slot_int(target, "n_prompt_tokens")
        if not hold.still_in_slot(n_slot):
            await self._note(
                "conversation_slot_moved",
                served_model,
                role=SlotRole.INTERACTIVE,
                slot_tokens=n_slot,
                last_input=hold.input_tokens,
            )
            return "failed"
        name = kv_conversation.file_name(hold.base, hold.key)
        path = os.path.join(save_dir, name)
        if not await self._disk_room(path, n_slot, served_model):
            return "failed"
        previous = await asyncio.to_thread(_read_meta, path)
        await asyncio.to_thread(self._remove_sidecar, path)
        try:
            resp = await self._gateway.save_slot(served_model, slot_id, name)
        except LocalGatewayError as exc:
            await self._note(
                "conversation_save_failed", served_model, role=SlotRole.INTERACTIVE, error=str(exc)
            )
            await asyncio.to_thread(self._remove_quietly, path)
            return "failed"
        n_saved = resp.get("n_saved")
        if n_saved != n_slot or not isinstance(n_saved, int):
            await self._note(
                "conversation_save_mismatch",
                served_model,
                role=SlotRole.INTERACTIVE,
                expected=n_slot,
                n_saved=n_saved,
            )
            await asyncio.to_thread(self._remove_quietly, path)
            return "failed"
        model = local_catalog.get_by_served(served_model)
        if model is not None and not await self._patch_proven(
            model, served_model, path, SlotRole.INTERACTIVE
        ):
            return "failed"
        # A re-save carries the miss streak: it judges the conversation, not one file.
        meta = ConversationMeta(
            base=hold.base,
            key=digest,
            n_tokens=n_saved,
            saved_at=time.time(),
            misses=previous.misses if previous is not None else 0,
        )
        if not await asyncio.to_thread(_write_meta, path, meta):
            await asyncio.to_thread(self._remove_quietly, path)
            return "failed"
        hold.dirty = False
        await self._prune_and_note(served_model, path)
        await self._note(
            "conversation_saved", served_model, role=SlotRole.INTERACTIVE, tokens=n_saved
        )
        return "saved"

    def note_conversation_turn(
        self,
        served_model: str,
        conversation_key: str | None,
        *,
        fingerprint: str | None,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        seq: int | None = None,
        tool_names: Sequence[str] = (),
    ) -> None:
        """An interactive turn completed in the pooled interactive slot: the slot now holds
        this request's prompt plus its answer, unsaved.

        Judges a restore by the first request it served (`cached_tokens` against what was
        restored). Claims the slot only if no other request prepared after this one (`seq`).
        A turn that ran an excluded tool taints the conversation: its files go, and it is never
        saved again. No conversation, no base identity, or no real usage leaves the slot
        unclaimed, which is never saved."""
        if self._conversation_pool(served_model) is None:
            return
        if conversation_key is not None and kv_conversation.key_hash(conversation_key) in (
            self._forgotten
        ):
            # The chat was deleted or re-scoped while this turn streamed: claim nothing.
            self._conv_hold.pop(served_model, None)
            return
        if conversation_key is not None and kv_conversation.any_excluded(tool_names):
            self._tainted.add(kv_conversation.key_hash(conversation_key))
            self._conv_hold.pop(served_model, None)
            self._spawn(self.forget_conversation(conversation_key))
            self._count("conversation_tainted")
            return
        if seq is not None and self._prepare_seq.get(served_model) != seq:
            self._count("conversation_claim_superseded")
            return
        previous = self._conv_hold.get(served_model)
        if (
            previous is not None
            and conversation_key is not None
            and previous.key == conversation_key
            and previous.restored_tokens is not None
        ):
            verdict = kv_conversation.judge(cached_tokens, previous.restored_tokens)
            self._count(f"conversation_restore_{verdict}")
            self._spawn(self._judged(served_model, previous.base, conversation_key, verdict))
        if conversation_key is None or fingerprint is None or input_tokens <= 0:
            self._conv_hold.pop(served_model, None)
            return
        self._conv_hold[served_model] = ConversationHold(
            key=conversation_key,
            base=fingerprint,
            input_tokens=input_tokens,
            output_tokens=max(0, output_tokens),
            dirty=True,
            at=time.monotonic(),
            epoch=self._conv_epoch,
        )

    def _hold_current(self, hold: ConversationHold) -> bool:
        """Whether a claim may still be saved: the cache is on, no clear happened since the
        claim, and its conversation was not forgotten, tainted or retired as unhelpful."""
        digest = kv_conversation.key_hash(hold.key)
        return (
            self._conversations
            and hold.epoch == self._conv_epoch
            and digest not in self._forgotten
            and digest not in self._tainted
            and digest not in self._unhelpful
        )

    async def _judged(
        self,
        served_model: str,
        base: str,
        conversation_key: str,
        verdict: kv_conversation.Judgement,
    ) -> None:
        """Record a judgement on the file's claim — a read-modify-write of the `.meta`, so under
        the save lock that every other writer of it holds. A conversation whose restores keep
        missing is not saved again this process life: each save would be a wasted write."""
        path = await asyncio.to_thread(
            self._conversation_path, served_model, base, conversation_key
        )
        if path is None:
            return
        async with self._save_lock:
            dropped = await asyncio.to_thread(_record_judgement, path, verdict)
        if dropped:
            self._unhelpful.add(kv_conversation.key_hash(conversation_key))
            self._count("conversation_dropped_unhelpful")

    def _conversation_path(self, served_model: str, base: str, key: str) -> str | None:
        line = llama_swap_config.launch_line(
            self._models_root, served_model, self._current_engine()
        )
        save_dir = None if line is None else _save_dir_from_line(line, self._models_root)
        return (
            None
            if save_dir is None
            else os.path.join(save_dir, kv_conversation.file_name(base, key))
        )

    def _spawn(self, work: Awaitable[object]) -> None:
        """Run a best-effort file task from a synchronous note, holding a reference to it."""

        async def _run() -> object:
            try:
                return await work
            except Exception:  # noqa: BLE001 — a cache's housekeeping never fails a turn
                log.warning("kv_prefix.background_task_failed", exc_info=True)
                return None

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def note_conversation_abandoned(self, served_model: str) -> None:
        """A request reached the interactive slot without a final turn (a stopped stream): what
        the slot holds is unknown, so nothing may be saved under any conversation's name."""
        self._conv_hold.pop(served_model, None)

    async def forget_conversation(self, conversation_key: str) -> int:
        """Delete every file this conversation has, under any base identity and model, and
        drop any claim on a slot holding it. For a deleted or re-scoped session, and a
        conversation an excluded tool ran in. Returns how many files went."""
        digest = kv_conversation.key_hash(conversation_key)
        # Marked BEFORE waiting for the save lock: a save already streaming finishes and its file
        # is then deleted below; one that starts later sees the mark and writes nothing.
        self._forgotten.add(digest)
        for served, hold in list(self._conv_hold.items()):
            if hold.key == conversation_key:
                del self._conv_hold[served]
        async with self._save_lock:
            removed = await asyncio.to_thread(self._remove_conversation_files, digest)
        if removed:
            await self._note("conversation_forgotten", "*", files=removed)
        return removed

    async def clear_conversations(self) -> int:
        """Delete every conversation file (the toggle turned off). Role prefixes stay. Claims
        made before it are void (`_conv_epoch`), whichever await they are parked at."""
        self._conv_hold.clear()
        self._conv_epoch += 1
        async with self._save_lock:
            removed = await asyncio.to_thread(self._remove_conversation_files, None)
        if removed:
            await self._note("conversation_cleared", "*", files=removed)
        return removed

    def _remove_conversation_files(self, digest: str | None) -> int:
        """Runs in a thread — remove conversation files whose claim names `digest` (all of them
        when None), with their sidecars and claims. A file with no readable claim goes too:
        nothing can say whose it is."""
        root = os.path.join(self._models_root, llama_swap_config.KVSLOT_DIR)
        removed = 0
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if not kv_conversation.is_conversation_file(name):
                    continue
                path = os.path.join(folder, name)
                if digest is not None:
                    meta = _read_meta(path)
                    if meta is not None and meta.key != digest:
                        continue
                self._remove_quietly(path)
                removed += 1
        return removed

    async def save_idle_conversation(
        self, served_model: str, *, idle_s: float = CONVERSATION_IDLE_SAVE_S
    ) -> bool:
        """The keeper's tick: save the interactive slot's conversation once it has sat
        unchanged for `idle_s`, so a restart or an engine switch does not take it."""
        pool = self._conversation_pool(served_model)
        hold = self._conv_hold.get(served_model)
        if pool is None or hold is None or not hold.dirty:
            return False
        if time.monotonic() - hold.at < idle_s:
            return False
        async with self._save_lock:
            if self._conv_hold.get(served_model) is not hold or not hold.dirty:
                return False
            outcome = await self._save_conversation(served_model, pool, hold)
        if outcome == "failed" and self._conv_hold.get(served_model) is hold:
            # The slot moved on or the save failed: the claim is no longer good.
            self._conv_hold.pop(served_model, None)
        return outcome == "saved"


def _record_judgement(path: str, verdict: kv_conversation.Judgement) -> bool:
    """Runs in a thread — a hit or partial resets the file's miss count; a miss bumps it, and
    the file goes at `MISS_LIMIT` in a row. True when the file was dropped."""
    meta = _read_meta(path)
    if meta is None:
        return False
    misses = meta.misses + 1 if verdict == "miss" else 0
    if misses == meta.misses:
        return False
    if misses >= kv_conversation.MISS_LIMIT:
        for part in (path, path + _SIDECAR_EXT, path + _META_EXT):
            with contextlib.suppress(OSError):
                os.remove(part)
        return True
    _write_meta(path, dataclasses.replace(meta, misses=misses))
    return False


def _bump_misses(path: str) -> None:
    """Runs in a thread, under the store's save lock — a failed restore counts as a miss."""
    _record_judgement(path, "miss")  # a drop here is seen at the next restore: no file


def _read_meta(path: str) -> ConversationMeta | None:
    """Runs in a thread — a conversation file's claim, or None when the file or its claim is
    missing or unreadable (a slot file with no claim is never restored)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path + _META_EXT, encoding="utf-8") as fh:
            return ConversationMeta.from_json(fh.read())
    except OSError:
        return None


def _write_meta(path: str, meta: ConversationMeta) -> bool:
    """Runs in a thread — write the claim beside its slot file, atomically, so a crash never
    leaves a torn claim that reads as valid."""
    tmp = f"{path}{_META_EXT}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(meta.to_json())
        os.replace(tmp, path + _META_EXT)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False
    return True


def _free_bytes(folder: str) -> int:
    """Runs in a thread — free bytes on the volume holding `folder`."""
    return shutil.disk_usage(folder).free


def _file_token_bound(path: str) -> int | None:
    """Runs in a thread — the token count a slot file's header declares (llama.cpp's
    `state_seq_save_file`: magic, version, then the packed prompt's u32 count), an upper bound
    on the cells a restore will take. None when unreadable."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(12)
    except OSError:
        return None
    if len(header) < 12:
        return None
    count = int.from_bytes(header[8:12], "little")
    return count if count > 0 else None
