"""Curated catalog of self-hostable local models.

The single source of truth for the OPT-IN local-hosting feature: it names the
models we support running on-box (through the `local-llm` compose profile's
llama-swap + llama.cpp Vulkan gateway), maps each to a `local:<model>` router
spec, and records the provisioning facts the setup script needs to download the
right GGUF weights. Tuned for an AMD Strix Halo class box (large unified memory,
~256 GB/s bandwidth) where MoE / small-dense models with a small active-param
set are the only ones that run at interactive speed.

Two consumers read this:
  - the app (jbrain.llm.providers) surfaces enabled models as settings choices;
  - scripts/local-llm-setup.sh reads `python -m jbrain.llm.local_catalog <ids>`
    for the JSON download manifest.

Nothing here changes default routing — every default stays on the cloud
providers (jbrain.llm.router.TASK_DEFAULTS). A model is reachable only after an
operator enables local hosting and selects it.
"""

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from jbrain.llm.types import Sampling

# The router spec for a local model is always "local:<served_model>": the local
# provider client posts <served_model> as the OpenAI `model`, and llama-swap
# routes/loads on that name. Keep served names matching the llama-swap config the
# setup script generates.
LOCAL_PROVIDER = "local"

# Qwen3.8's chat template accepts a `reasoning_effort` level (low / medium / xhigh) ON TOP OF
# the `enable_thinking` toggle its predecessors had, and applies `xhigh` when given none. Our
# four settings levels map onto its three: "none" is the toggle (thinking off, so it never
# appears here) and the rest step up. Shared by the Qwen3.8 twins so they can't drift apart.
QWEN38_EFFORT_LEVELS: dict[str, str] = {"low": "low", "medium": "medium", "high": "xhigh"}

# Qwen3.8-27B is NOT a plain dense transformer: its config declares `full_attention_interval:
# 4`, so of 64 layers only 16 are full attention and the other 48 are Gated DeltaNet (linear
# attention), which carries a constant state rather than a growing KV cache. Only that quarter
# of the layers grows with context.
#
# DERIVED, not estimated. Per token, per attention layer, KV is
# `2 (K and V) x n_kv_heads x head_dim x bytes_per_element`; for this model that is
# 2 x 4 x 256 x 2 (f16) = 4 KiB, and x16 attention layers = 64 KiB/token -> 8.0 GiB per 128k.
#
# Now q8_0, and this number is MEASURED rather than derived — the two disagree, and the
# measurement wins because it is the one the box has to survive.
#
# The derivation (MODEL_PROMPTING.md) gives the cache alone: 2 (K+V) x 4 kv-heads x 256
# head-dim x 1.0625 bytes x 16 attention layers = 34 KiB/token = 4.25 GiB per 128k at q8_0,
# against 8.0 at the f16 default we used to inherit. That much halves.
#
# But a sweep of this model at four served windows (2026-08-21, 8k/32k/64k/128k, one model
# resident, device delta read per load) fits a slope of 9.53 GiB per 128k at f16 — 1.53 above
# the 8.0 the cache accounts for. So something ELSE grows with the window by that much, and it
# will not halve with the cache. The MTP draft context is the likely home: `MTP_OVERHEAD_GB`
# is booked flat at 1.0 while its own comment says it "scales with context and with n-max".
# That has not been isolated, so the residual rides here instead of hiding: under-reserving is
# the direction that takes the host down, and a term that is measured but unattributed is
# still better budgeted than dropped.
#
#   4.25 (q8_0 cache, derived) + 1.53 (window-scaling residual, measured) = 5.78
#
# MOVES WITH THE SERVING FLAG. The `-ctk`/`-ctv q8_0` on all three Qwen3.8 27B entries and
# this number are one decision in two places; changing either alone mis-budgets every load.
_QWEN38_KV_GB_PER_128K = 5.78

# Everything a load pins that is neither weights nor KV, for this model family:
#   ~0.14 GiB  Gated DeltaNet recurrent state across the 48 linear-attention layers (f32,
#              constant — it does not grow with context)
#   ~0.40 GiB  compute + output buffers at the served `-ub`
#   ~0.96 GiB  the MTP draft context, on speculative entries only (see MTP_OVERHEAD_GB)
# The first two apply to every entry and were simply missing: weights + KV alone predicted
# 16.4 GiB for the MTP entry against ~19.5 GiB measured on-box, and this is half of that gap.
RUNTIME_OVERHEAD_GB = 0.55

# `--spec-type draft-mtp` builds a SECOND llama_context against the same model — no duplicated
# weights. For `qwen35` that context takes the plain-KV branch with a filter selecting only the
# single nextn block, so it allocates ONE attention layer's KV (~128 MiB at f16/32k) and zero
# recurrent state, plus its own compute/output buffers and sampler chains. The hybrid shape
# makes MTP cheaper here, not dearer.
#
# ~1 GiB at our configuration (32k context, --spec-draft-n-max 3). It scales with context and
# with n-max, so a much larger window or draft depth needs this revisited rather than reused.
MTP_OVERHEAD_GB = 1.0

# llama.cpp's in-RAM prompt cache (`--cache-ram` / `-cram`), which defaults to 8192 MiB and
# is therefore paid by EVERY resident model whether or not we ever write the flag.
#
# It is HOST memory, not GTT, and that distinction is why it was missed: the note beside the
# flag on `EXTRA_ARG_FLAGS` reasons that it "does not touch the GTT budget" — true, and
# irrelevant to `jbrain.llm.residency`, which is a HOST-RAM budget. So up to 8 GiB per
# resident model has been invisible to the evictor all along; on a box holding three models
# that is 24 GiB the plan could not see, on the machine whose failure mode is running out of
# exactly this.
#
# It belongs in the RESIDENT figure and NOT in the load reservation, for the same reason as
# context checkpoints and the vision peak: it is a cache LIMIT that fills lazily as prompts
# are served, not an allocation the load makes. A model that has just loaded has not paid it
# yet; a model that has been answering for an hour has.
#
# Budgeted at the default rather than the served value: an operator override rides
# `extra_server_args`, which the cost model does not parse. Under-counting an override is the
# same class of gap as the checkpoint count, and is why that flag is bounded (0..32 GiB) in
# the settings API rather than left open.
# ZERO because the gateway now serves `-cram 0` (llama_swap_config): the in-RAM prompt cache
# is switched off, so no resident model pays for it. It was 8.0 — llama.cpp's own default,
# which we inherited without writing the flag, and which cost 8 GiB of the same unified pool
# the weights come from, per resident model.
#
# MOVES WITH THE SERVING FLAG. This number and the `-cram` on the command line are one decision
# in two places: budgeting 8 GiB while serving 0 over-reserves 16 GiB across a co-resident pair
# and evicts models that fit; budgeting 0 while serving the default under-reserves on the path
# this box has hard-locked on.
CACHE_RAM_GB = 0.0

_KV_REFERENCE_TOKENS = 131072

# Per-slot context checkpoints the gateway serves (`--ctx-checkpoints`). Lives HERE, not in
# llama_swap_config, because two things need the same number: the command that sets it and the
# footprint that must budget for it. It was only in the command, so the memory model did not
# know checkpoints existed at all.
#
# 16, up from 2, MEASURED on the box against a real 33k-token conversation. On a HYBRID model
# `seq_pos_min` is the tail position (llama-memory-hybrid.cpp), so `pos_min >= pos_min_thold`
# always holds and llama.cpp must find a checkpoint at or before the divergence point or
# reprocess the WHOLE prompt from token 0. At 2 — with `--checkpoint-min-step` left at its
# 8192 default — both checkpoints sat bunched at the end of the conversation, none covered the
# divergence, and every turn paid a full re-prefill: 33,648 prompt tokens, 232 s, on repeat.
# With 16 + a 1024 step the same conversation processes only its delta: 814 tokens in 8.4 s,
# 1,084 in 15.9 s, and `erasing old context checkpoint` went from 12 occurrences to 0.
#
# Still half of llama.cpp's own default of 32.
CTX_CHECKPOINTS = 16

# The count for a model whose per-checkpoint cost has NOT been measured (`checkpoint_gb == 0.0`).
#
# The raise only pays where checkpoints are actually CREATED — a hybrid, or an SWA model served
# without `--swa-full` (llama.cpp zeroes `n_swa` when that flag is set, so gpt-oss creates none at
# all). Both Nemotron hybrids qualify and are unmeasured, and `nemotron-3.5-lightning-30b` runs TWO
# slots, so a flat raise would put 32 checkpoints of unknown size on the box against a budget of 0.
#
# So the raise is gated on the measurement rather than applied flat: `ctx_checkpoints()` hands the
# higher count only to models whose cost is known. That makes the setting self-limiting — the
# count cannot outrun the budget accounting for it — and it is the same principle as the
# deliberate zero on `checkpoint_gb`: a measurement earns the memory, a guess does not.
CTX_CHECKPOINTS_UNMEASURED = 2


def ctx_checkpoints(checkpoint_gb: float | None) -> int:
    """Per-slot context checkpoints to serve a model with (`--ctx-checkpoints`).

    Takes the model's measured per-checkpoint cost rather than the model, because the gateway
    config renders from manifest dicts while the cost model holds `LocalModel`s. 0/None means
    unmeasured — see `CTX_CHECKPOINTS_UNMEASURED`."""
    return CTX_CHECKPOINTS if (checkpoint_gb or 0) > 0 else CTX_CHECKPOINTS_UNMEASURED


# Minimum token spacing between checkpoints (`--checkpoint-min-step`). llama.cpp defaults to
# 8192, which we never set — and the count above is worthless without it: 16 checkpoints forced
# 8192 apart still cannot cover a conversation densely enough to catch a divergence near the
# end. The two are one setting and are raised together. llama.cpp rejects anything below 64.
CHECKPOINT_MIN_STEP = 1024

# The CLIP/mtmd vision encoder's attention buffer — the real cost behind llama.cpp #27146, and
# NOT what this code previously claimed it was.
#
# What it actually is: with flash attention off in the CLIP graph, `clip_graph::build_attn`
# materialises the full attention matrix as F32 `[n_patches, n_patches, n_head]`. So it grows
# with the SQUARE of the image token count, and has nothing to do with the projector file's
# size. A fixed-resolution projector (Gemma) costs a few hundred MiB; only a dynamic-resolution
# one at a high token ceiling reaches double-digit GB.
#
# Two corrections to what this module used to assert, both of which matter:
#   - It is NOT a load-time cost. Load warms up at a capped 46x46 = 2116 tokens; the large
#     allocation lands on the first FULL-RESOLUTION image encode, which can be much later.
#   - It is NOT transient. `ggml_gallocr_reserve_n_impl` only ever grows the buffer — there is
#     no shrink path — and it is freed at model unload. A smaller subsequent image releases
#     nothing. So it is a RESIDENT high-water mark, and belongs in footprint_gb too.
#
# The old flat 33.0 came from back-solving one freeze against a bug report that is (a) a
# different GPU (gfx1150 / 32 GB, not this box), and (b) a `total-vm` figure — virtual address
# space, with `anon-rss: 4 kB` — not resident memory. It was not a measurement of anything.
_CLIP_ATTN_HEADS = 16
_CLIP_MERGE = 2
# llama.cpp's default ceiling for the Qwen3-VL projector family (`set_limit_image_tokens(8,
# 4096)`). We pass `--image-min-tokens` (the `image_min_tokens` field, 2048 by default), which
# is the FLOOR; nothing caps the ceiling, so
# this is the worst case we are actually exposed to. Pinning `--image-max-tokens` would cut
# this quadratically (1024 -> ~1 GiB) at some cost to grounding accuracy on small text.
_VISION_MAX_IMAGE_TOKENS = 4096
# What llama.cpp actually warms the projector at during LOAD: `set_warmup_n_tokens(46 * 46)`.
# The gap between this and the ceiling above is the whole reason the balloon shows up long
# after a load reported success.
_VISION_WARMUP_IMAGE_TOKENS = 46 * 46


# Bytes per patch of CLIP attention workspace WITH flash attention on, anchored to a measured
# figure: 248.10 MiB at the 2116-token warmup (8464 patches). Flash attention never
# materialises the [n_patches, n_patches] matrix — it tiles — so the cost is linear in patches
# rather than quadratic, which is the whole difference.
_CLIP_FA_BYTES_PER_PATCH = 248.10 * 1024**2 / (2116 * _CLIP_MERGE**2)


def vision_attn_buffer_gb(
    max_image_tokens: int = _VISION_MAX_IMAGE_TOKENS, *, flash_attention: bool = True
) -> float:
    """Resident GB the CLIP attention workspace reaches at `max_image_tokens`.

    MEASURED ON THIS BOX, no longer assumed. Loading `qwen3.8-27b-q4` beside a resident
    gpt-oss moved GTT 67.71 -> 93.73 GiB (+26.02, against 25.60 predicted for flash attention
    ON and 29.62 for OFF), and a subsequent full-resolution 2.1 MB image encode moved it only
    93.73 -> 93.84 (+0.11 GiB). With flash attention off that image would have allocated up to
    16 GiB. It is on — `-fa 1` reaches the CLIP graph — so the default is the linear branch.
    The previous default was the quadratic one, which over-reserved by ~145x.

    `flash_attention=False` keeps the quadratic worst case for a build or backend where it
    does not apply: `n_patches**2 * n_head * 4` bytes (F32, softmax in-place so 1x not 2x).
    Anything that drops `-fa` from the served flags has to revisit this."""
    n_patches = max_image_tokens * _CLIP_MERGE**2
    if flash_attention:
        return round(n_patches * _CLIP_FA_BYTES_PER_PATCH / 1024**3, 2)
    return round(n_patches**2 * _CLIP_ATTN_HEADS * 4 / 1024**3, 2)


@dataclass(frozen=True)
class LocalModel:
    """One self-hostable model and how to provision it."""

    id: str  # stable settings-choice id (also the UI provider id)
    label: str  # human label for the settings screen
    served_model: str  # name the local gateway serves it under
    tiers: tuple[str, ...]  # capability tiers it can credibly serve
    supports_vision: bool
    supports_tools: bool
    # In the recommended default-enabled set the install prompt offers first.
    recommended: bool
    # Provisioning hints for scripts/local-llm-setup.sh.
    hf_repo: str
    gguf_include: str  # huggingface-cli --include glob for the weights
    mmproj_include: str | None  # vision projector glob, or None for text-only
    quant: str
    size_gb: float
    note: str = ""
    # Emits a `reasoning_content` channel and honors a reasoning setting (gpt-oss
    # harmony effort / GLM thinking / a Qwen hybrid think toggle). Drives the settings
    # effort control and lets the router send a level to this model; default False
    # (plain Instruct variants and Llama here are non-thinking).
    supports_reasoning: bool = False
    # llama-server `--reasoning-format` for a model that emits its thinking inline as
    # `<think>…</think>` (DeepSeek-R1 / Qwen3-Thinking / a Qwen hybrid with thinking on):
    # "deepseek" makes llama.cpp parse those tags OUT of `content` into a separate
    # `reasoning_content` channel, which OpenAI-compatible clients (grok build) read as
    # the reasoning trace. Empty = leave llama.cpp's default (`auto`) — correct for
    # harmony/GLM reasoners, whose template `auto` handles.
    reasoning_format: str = ""
    # A Qwen-style HYBRID reasoner: thinking is a chat-template toggle
    # (`enable_thinking`), not a `reasoning_effort` level. The adapter maps the routed
    # level onto that toggle instead of sending `reasoning_effort` (which the Qwen
    # template ignores): "none" → `enable_thinking=false` (a real "reasoning off"),
    # any other level → thinking on. False for harmony/grok/GLM (they take the effort
    # verbatim) and for always-on `<think>` checkpoints (which have no off switch).
    hybrid_thinking: bool = False
    # For a hybrid whose chat template ALSO understands a `reasoning_effort` level (Qwen3.8
    # onward): the map from OUR routed level to the level its template accepts. The toggle
    # alone is not enough for these — with thinking on and no level, the template applies the
    # card's own default, which for Qwen3.8 is `xhigh`, and a trivial prompt then burns
    # thousands of reasoning tokens (measured on-box: the same prompt took 37.9s / 439 output
    # tokens with no level against 13.8s / 161 with thinking off, and the long run's answer
    # was the SHORTER of the two). Empty (the default) preserves the toggle-only behaviour
    # that is still correct for the Qwen3.5/3.6-era hybrids, whose templates ignore the field.
    # Keys are our levels; "none" never appears — it turns thinking off via the toggle.
    thinking_effort_map: dict[str, str] = field(default_factory=dict)
    # Extra `llama-server` flags appended verbatim to the gateway command
    # (jbrain.llm.llama_swap_config). Carries the MTP self-speculative-decoding flags
    # (`--spec-type draft-mtp …`) for the MTP variant; empty for every model whose
    # serving is fully described by the fields above.
    extra_server_args: tuple[str, ...] = ()
    # `--image-min-tokens`: the FLOOR an image is encoded to, and the knob that decides
    # whether small text in a photo survives to the model. A FIELD rather than a raw flag in
    # extra_server_args, for the same reason context_window is: the operator can override it
    # per model (Settings → LLM), and an override has to REPLACE the default rather than be
    # appended after it — two of the same flag on one command line is unreadable as a record
    # of what is actually served. None on a text-only entry, where llama.cpp never reads it.
    #
    # Measured on a bottle label with fine print, three reads per floor: at 1024 (llama.cpp's
    # own default is lower still) one read in three was usable and the rest invented company
    # names; at 2048 all three read the core label; at 4096 all three read the core label AND
    # promotional small print the lower floors could not resolve at all.
    #
    # The shipped default is 2048 — the point where the core label became RELIABLE rather than
    # lucky. 4096 read strictly more on that image, but it is 4x llama.cpp's own floor and the
    # evidence is one photo, so the extra prefill is left as an opt-in per model rather than
    # spent on every image turn. Costs prefill and KV, never weights, so no floor can change
    # whether a model fits.
    image_min_tokens: int | None = None
    # The context window the gateway serves this model with (llama-server's `-c`)
    # ABSENT an operator override. The single source of truth: scripts/local-llm-setup.sh
    # stamps this into the llama-swap config, and the router reports it to the PWA's
    # context-usage meter. Kept conservative on this memory-bound box — the operator
    # raises it per-model up to `native_context_window` when the KV cache fits.
    context_window: int = 32768
    # The model's native (architectural) maximum context — the CEILING the operator
    # may raise the served window to from the settings drawer. 0 means "no headroom
    # above context_window" (the served default is already the max we expose). The
    # served default stays small for memory; this opens the door to the full window
    # the weights support, with the drawer's KV-cache estimate as the guardrail.
    native_context_window: int = 0
    # A HYBRID/RECURRENT attention stack (qwen35's Gated DeltaNet, Nemotron's Mamba-2) rather
    # than plain attention. Not derivable from `checkpoint_gb`, which is a memory figure we only
    # have for some of them — this is an architecture fact and drives SERVING decisions:
    # `--cache-reuse` is dropped for these (its partial-range `seq_rm` returns false for
    # recurrent memory and reaches GGML_ABORT, i.e. the server dies), and prefix reuse here is
    # mediated entirely by context checkpoints.
    recurrent: bool = False
    # KV-slot save/restore opt-in for entries the blanket rules would exclude. The default
    # rule (llama_swap_config + kv_prefix) admits plain attention models only; a HYBRID
    # (recurrent) model was excluded because a restore leaves the slot with no context
    # checkpoints — but llama.cpp's hybrid memory serializes BOTH halves of the state
    # (llama_memory_hybrid::state_write covers mem_attn and mem_recr), so a byte-stable
    # prefix (the load warm) restores soundly: the recurrent state lands exactly at the
    # prefix end, and the first divergence merely reprocesses from zero (today's cost,
    # fail-soft). MTP self-drafting is likewise safe: the draft derives from the target's
    # hidden states and is always verified — a restore costs brief draft acceptance, never
    # correctness. Set per entry once reasoned through; verified live per model.
    kv_slot_restorable: bool = False
    # GiB per context checkpoint, per slot. Non-zero only for a HYBRID (recurrent) model,
    # where a checkpoint is a full copy of the recurrent state and is device-resident —
    # ~150 MiB for Qwen3.8 (llama.cpp #20145, #23371). Zero on an attention model, whose
    # checkpoints are cheap KV bookkeeping rather than a state copy.
    #
    # This term was missing entirely, which mattered once `--ctx-checkpoints` became an
    # operator-settable flag: the residency evictor would co-load a second model against RAM
    # already spoken for. Deliberately NOT set for the Nemotron Mamba-2 hybrids — their SSM
    # state has a different shape and nobody has measured it here, and a guessed number in a
    # budget that governs host stability is worse than a documented zero.
    #
    # That zero got 8x more expensive when CTX_CHECKPOINTS went 2 -> 16, so measuring it is now
    # worth doing: load a Nemotron, hold a multi-turn conversation, and read the `size = N MiB`
    # off its `create_check` lines via GET /api/debug/llm/upstream-logs. That is exactly how the
    # 0.28 above was obtained (275-284 MiB observed, against a catalog 0.15 taken from upstream
    # #27211's figure for a different quant).
    checkpoint_gb: float = 0.0
    # Measured override for the flat RUNTIME_OVERHEAD_GB, for a model whose resident
    # non-weight cost the constant misrepresents. The tiny Qwen3.5 hybrids are why this
    # exists: their compute/state buffers dwarf their weights, so the flat 0.55 declared a
    # 0.9 GiB model at 1.57 while it held 3.83 on the box — and the ledger, the load
    # pre-flight and the runaway ceiling all inherited the lie. None = use the constant.
    runtime_overhead_gb: float | None = None
    # Rough KV-cache size (GB) at the model's full 131072-token window — an ESTIMATE
    # (not a measurement) the settings drawer's memory bar uses to size the context
    # portion of each model's segment, scaled linearly by the configured window.
    # gpt-oss is low (alternating sliding-window attention); dense models are higher.
    kv_gb_per_128k: float = 0.0
    # Serve this model with `--swa-full` — full history on its sliding-window layers.
    #
    # Only meaningful for an interleaved-SWA model (gpt-oss). It is the PRECONDITION for
    # KV-slot restore doing anything: measured on the box 2026-08-17, a restore into a
    # windowed cache returns 200 with every token accounted for and llama-server then
    # re-prefills anyway (69,373 ms), while the same restore with this flag skips the
    # prefill entirely (194 ms). The failure without it is silent — same token count, same
    # bytes, same latency on the restore call — so this is not a tuning knob.
    #
    # It roughly DOUBLES the model's KV, which `footprint_gb` accounts for: every layer now
    # keeps full history instead of a 128-token window. Leave False for a dense model, where
    # it buys nothing and costs nothing (llama.cpp warns and ignores it).
    kv_full_history: bool = False
    # The vendor's RECOMMENDED sampling for this model (docs/reference/MODEL_PROMPTING.md).
    # The router applies it to every call so the model runs at its card's values instead of
    # llama.cpp's engine defaults (temp 0.8 / top_p 0.95 / top_k 40 / min_p 0.1 — which match
    # no card and, for gpt-oss, actively prune tokens the model wants). Empty for a model
    # whose vendor publishes none. A per-task `.prompt` `sampling:` override merges on top.
    sampling: Sampling = field(default_factory=Sampling)
    # A hybrid reasoner whose card gives DIFFERENT sampling for thinking vs non-thinking mode
    # (the Qwen hybrids) sets this to the thinking-mode values; the router picks it when the
    # model is generating a thinking trace, else `sampling`. None when the card is unified
    # (Nemotron/GLM) or the model has no thinking mode — `sampling` then applies always.
    sampling_thinking: Sampling | None = None

    @property
    def spec(self) -> str:
        return f"{LOCAL_PROVIDER}:{self.served_model}"

    @property
    def is_speculative(self) -> bool:
        """Served with speculative decoding (`--spec-type <mode>` in its serving flags).
        Read off the flags rather than a separate boolean so the fact and its cause can't
        drift apart; `llama_swap_config._is_speculative` is the same test on the manifest
        dicts the config generator sees."""
        return "--spec-type" in self.extra_server_args

    @property
    def is_mtp_speculative(self) -> bool:
        """Speculative via the model's own MTP head (`--spec-type draft-mtp`). Unlike an
        external draft model, the MTP draft derives from the TARGET model's hidden states
        — there is no separate draft KV a slot file would miss — and speculative decoding
        verifies every draft against the target, so a KV restore can cost draft
        acceptance for a few tokens but never correctness. This is what lets these
        entries into the kv_prefix disk store while other speculative shapes stay out."""
        args = list(self.extra_server_args)
        try:
            return args[args.index("--spec-type") + 1] == "draft-mtp"
        except (ValueError, IndexError):
            return False

    def effective_slots(self, requested: int) -> int:
        """The `-np` this model will ACTUALLY serve with, given a requested slot count.
        Everything that reasons about slots (the gateway command, the residency budget, the
        settings drawer) goes through here, so none of them can disagree with the engine.

        An explicit request for more than one slot is now HONOURED on a speculative model, at
        the cost of speculation (`serves_speculative`). It used to be silently clamped to 1,
        which was the worst of both: the operator asked for a second slot, did not get it, and
        got no signal saying so. A second slot is the only thing that stops a background task
        that FOLLOWS the interactive model (`research.title`, and anything a future feature
        routes the same way) from landing in the one slot and evicting a 32k primed prefix,
        which costs a ~100 s cold prefill. The chat auto-titler was the worst offender and is
        gone — jerv now names its chat in-turn via `name_session` — but the class of task
        remains. Trading MTP's decode gain (~22 vs ~11-12 t/s measured) for that is a real
        choice, and it is the operator's to make."""
        return max(1, requested)

    def serves_speculative(self, requested_slots: int) -> bool:
        """Whether speculation is actually served at `requested_slots`.

        llama.cpp's speculative paths run ONE sequence — MTP takes no second parallel slot and
        draft acceptance collapses as concurrent sequences rise (reported on this exact gfx1151
        SoC). So the two features are mutually exclusive, and asking for slots turns speculation
        OFF rather than being ignored."""
        return self.is_speculative and max(1, requested_slots) == 1

    @property
    def max_context_window(self) -> int:
        """The largest `-c` the operator may select for this model: its native
        window when recorded, else the served default (no headroom above it)."""
        return self.native_context_window or self.context_window


# Order is the order the settings screen and install prompt present them.
CATALOG: tuple[LocalModel, ...] = (
    LocalModel(
        id="qwen3-vl-30b",
        label="Qwen3-VL 30B · vision",
        served_model="qwen3-vl-30b-a3b",
        # Qwen VL Instruct card values; presence_penalty 1.5 is Qwen's headline knob for
        # the repetition/endless-loop failure VL models hit on dense images and long OCR.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        tiers=("vision", "low"),
        supports_vision=True,
        supports_tools=True,
        recommended=True,
        hf_repo="Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include="mmproj*.gguf",
        quant="Q8_0",
        size_gb=32.0,
        note="Vision + a capable cheap text model; Q8 preserves OCR fidelity.",
        # Native 256k (expandable to 1M upstream); serves the gateway default.
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="qwen3-vl-30b-q4",
        label="Qwen3-VL 30B · vision (Q4, memory-saver)",
        served_model="qwen3-vl-30b-a3b-q4",
        # Same model as the Q8 twin — same VL card sampling.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        tiers=("vision", "low"),
        supports_vision=True,
        supports_tools=True,
        # Opt-in memory-saver twin of the recommended Q8 entry — same model and repo, half
        # the weights, so it co-resides beside gpt-oss-120b with real headroom under the
        # free-RAM floor instead of evicting it. A plain local-hosting enable never pulls it
        # (Q8 is the recommended default); the operator installs it when co-residence matters
        # more than the last bit of OCR fidelity.
        recommended=False,
        hf_repo="Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        # Keep the F16 projector so the vision tower stays full precision even at Q4 weights
        # (fine text degrades first under quantization, docs/reference/MODEL_PROMPTING.md). This
        # repo names it `mmproj-Qwen3VL-30B-A3B-Instruct-F16.gguf`, so a glob (not the bare
        # `mmproj-F16.gguf` name Unsloth repos use) is required — it matches the F16 projector
        # while skipping the redundant `...-Q8_0.gguf` one. The include feeds BOTH `hf download
        # --include` and resolve_weight's glob, so a mismatch pulls no projector and the install
        # sticks partway (bar caps < 100%, resolve fails "download incomplete").
        mmproj_include="mmproj*F16.gguf",
        quant="Q4_K_M",
        # GiB on disk (the catalog's unit): HF lists Q4_K_M at ~18.6 decimal GB (~17.3 GiB)
        # plus the ~1.0 GiB F16 projector = ~18.3 GiB — an ESTIMATE until measured on-box,
        # kept at the GiB (not decimal-GB) sum so the install bar doesn't cap early (the
        # Nemotron note). ~14 GiB lighter than the Q8 entry: that gap is the co-residence win.
        size_gb=18.3,
        note="Vision + a capable cheap text model at Q4_K_M — the memory-saver twin of the "
        "recommended Q8 entry. ~18 GiB vs ~32, so it co-resides beside gpt-oss-120b with room "
        "to spare under the free-RAM floor rather than evicting it. The projector stays F16, "
        "but the Q4 weights trade some OCR fidelity on dense/small text — prefer the Q8 entry "
        "when transcription accuracy matters, this one when co-residence headroom does.",
        # Same architecture as the Q8 entry: native 256k, served at the gateway default, and
        # the same KV estimate (quantizing the weights doesn't shrink the KV cache).
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="llama-4-scout-int4",
        label="Llama 4 Scout · vision (int4)",
        served_model="llama-4-scout-int4",
        # Meta ships temp 0.6 / top_p 0.9 (generation_config.json); min_p 0.01 is Unsloth's
        # community default for Scout. Meta gives no top_k, so disable it (0) and sample from
        # pure temperature/top_p — the way the card was validated — not llama.cpp's top_k 40.
        sampling=Sampling(temperature=0.6, top_p=0.9, top_k=0, min_p=0.01),
        tiers=("vision", "low"),
        supports_vision=True,
        supports_tools=True,
        # Opt-in alternate to qwen3-vl-30b — a plain local-hosting enable never pulls
        # its ~59 GB, and adding it leaves the already-selected models untouched.
        recommended=False,
        hf_repo="unsloth/Llama-4-Scout-17B-16E-Instruct-GGUF",
        # Unsloth nests the int4 dynamic quant's shards in a UD-Q4_K_XL/ subdir (two
        # shards); the recursive glob matches each shard path and resolve_weight follows
        # the -00001-of-00002 head to the rest.
        gguf_include="*UD-Q4_K_XL*.gguf",
        # The F16 vision projector lives at the repo root. Name it exactly (not mmproj*)
        # so the pull doesn't also grab the redundant BF16 projector beside it.
        mmproj_include="mmproj-F16.gguf",
        quant="UD-Q4_K_XL",
        # GiB on disk (the catalog's unit): the two UD-Q4_K_XL shards (~57.8 GiB from
        # HF's 49.6 + 12.4 decimal-GB listing) plus the ~1.6 GiB F16 projector. An
        # ESTIMATE until measured on-box; kept at the GiB (not the decimal-GB) sum so the
        # install bar doesn't cap early and read as a stall (see the Nemotron note).
        size_gb=59.4,
        note="109B MoE, 17B active over 16 experts — Meta's multimodal (text + vision) "
        "Scout at Unsloth's int4 dynamic quant. A fast vision alternate to qwen3-vl-30b "
        "(more total params, similar active cost); co-resides beside a small model on a "
        "128 GB box. Non-thinking (no reasoning channel). Vision needs a recent llama.cpp "
        "build with Llama 4 mmproj support (in the multimodal set upstream).",
        # Scout's native window is architecturally huge (10M via iRoPE), far beyond what
        # this box can hold. Expose a 1M ceiling (picker steps at 500k and 1M): ~59 GB of
        # weights plus this model's KV estimate keeps 1M inside a 128 GB box (~105 GB),
        # while the steps above that would exceed it — so 1M is the largest window an
        # operator can realistically serve here. The drawer's KV bar is the guardrail;
        # serves the conservative gateway default until raised.
        native_context_window=1_000_000,
        # Interleaved local/global attention keeps the KV cache moderate for a model this
        # size; matches the other vision MoE's conservative guardrail estimate.
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="gpt-oss-120b",
        label="GPT-OSS 120B · reasoning",
        served_model="gpt-oss-120b",
        # OpenAI's guidance: sample from the model's own distribution — temp 1.0, top_p 1.0,
        # top_k 0 (off). min_p 0.0 is CRITICAL here: llama.cpp's default min_p 0.1 would prune
        # low-probability tokens gpt-oss wants, fighting the model. Penalties stay off.
        sampling=Sampling(temperature=1.0, top_p=1.0, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=True,
        hf_repo="ggml-org/gpt-oss-120b-GGUF",
        gguf_include="*mxfp4*.gguf",
        mmproj_include=None,
        quant="MXFP4",
        size_gb=59.0,
        note="Strongest open reasoning that still runs fast here (~31 t/s).",
        supports_reasoning=True,
        # The model's full native window. Its alternating sliding-window attention
        # keeps the f16 KV cache modest (~half the layers grow with context), so
        # 128k fits the box's unified memory beside the MXFP4 weights.
        context_window=131072,
        # MEASURED ON THE BOX 2026-08-23, at four windows (16k/32k/64k/128k) with the device
        # probe, each load cold and each baseline taken only after free memory had SETTLED —
        # the first attempt at this measured 69.41 GB at a 64k window against 69.26 at 128k,
        # which is impossible, because its baseline was sampled one second after a 69 GB model
        # was unloaded and was still falling.
        #
        # Against the old 4.5 the drift ran -0.20, -0.04, +0.21, +0.71 — an error PROPORTIONAL
        # to the KV term (slope +11.4%, r-squared effectively 1), so the coefficient was light
        # rather than the model carrying a missing constant. At 5.01 the same four points give
        # -0.33, -0.30, -0.30, -0.32: a flat, conservative offset, which is the same shape the
        # 27B family already has (-0.41 across a 16x window range) and is what a correct
        # coefficient looks like here.
        #
        # WHICH NUMBER IS ACTUALLY WRONG IS NOT SETTLED. gpt-oss is the only `kv_full_history`
        # entry in the catalog, so "the base KV is 11% bigger" and "the --swa-full doubling is
        # really ~2.23x" fit these measurements identically. It is recorded on the model rather
        # than on the multiplier because a per-model figure is what was measured; the next
        # `--swa-full` model must be measured too rather than inheriting either guess.
        kv_gb_per_128k=5.01,
        # The interactive persona lives here, so it is the one model whose cold prefill the
        # owner actually waits on — and the only one where a KV-slot restore pays for itself.
        kv_full_history=True,
    ),
    LocalModel(
        id="nemotron-3.5-lightning-30b",
        label="Nemotron 3.5 Lightning 30B · reasoning (alt)",
        served_model="nemotron-3.5-lightning-30b",
        # Same NVIDIA guidance as the Super 120B: temp 1.0 / top_p 0.95, unified across modes.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="ggml-org/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # GiB on disk (the catalog's unit): the single Q8_0 weight, ~32.6 GiB from HF's 35
        # decimal-GB listing. ESTIMATE until measured on-box; kept at the GiB (not the
        # decimal-GB) figure so the install bar doesn't cap early.
        size_gb=32.6,
        note="30B MoE, 3B active — NVIDIA's Nemotron 3.5 Lightning at 8-bit (near-lossless), a "
        "fast high-tier alt. Hybrid Mamba-2 + MoE + attention arch: the constant Mamba state "
        "keeps the KV cache tiny, so it holds long context far better than a dense model and "
        "stays fast even at Q8 (only ~3B active is read per token, so quant barely moves speed "
        "here — Q8 for the quality). Co-resides beside gpt-oss-120b. A HYBRID reasoner: "
        "thinking is the enable_thinking chat-template toggle, set per task in LLM Settings "
        "('none' runs it as a snappy Instruct model). Emits <think> traces, so it needs a "
        "recent llama.cpp build that serves the hybrid Mamba arch and supports "
        "--reasoning-format.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 1M context; serves the conservative gateway default. The Mamba-2 hybrid's
        # constant state makes the KV term small, so raising the window is cheap here — the
        # drawer's linear KV estimate overcounts the non-growing Mamba layers (a conservative
        # guardrail, not a true measure).
        native_context_window=1048576,
        kv_gb_per_128k=3.0,
        recurrent=True,  # Mamba-2 hybrid
    ),
    LocalModel(
        id="qwen3.8-27b",
        label="Qwen3.8 27B · vision + reasoning (Q8)",
        served_model="qwen3.8-27b",
        # The Qwen hybrid sampling split: non-thinking (Instruct) temp 0.7 / top_p 0.8 /
        # presence_penalty 1.5; thinking temp 1.0 / top_p 0.95, no presence penalty. The router
        # picks by whether thinking is on for the call.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0),
        tiers=("vision", "high"),
        supports_vision=True,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.8-27B-GGUF",
        gguf_include="*Q8_0*.gguf",
        # Keep the F16 projector (fine text degrades first under quantization). Name it EXACTLY:
        # a `mmproj*F16.gguf` glob would also match this repo's `mmproj-BF16.gguf` (it ends in
        # `F16.gguf` too), so the exact name pulls only the F16 one and skips the BF16 beside it.
        mmproj_include="mmproj-F16.gguf",
        # Grounding (box-a-thing-in-my-photo) is unreliable when the projector is fed
        # too few visual tokens: llama.cpp sizes the image budget from the model
        # metadata and a modest photo can land well under what Qwen-VL needs to
        # localize. Floor it. Without this the model still answers confidently — it
        # just answers with the wrong box (AGENT_CANVAS_PLAN §5.4).
        # Same MTP serving mode as the Q4 sibling — the head ships in every quant of unsloth's
        # GGUF, so the flags apply identically. UNMEASURED at this quant: the 26.39 GiB / 22.41
        # t/s figures behind the Q4 entry were taken on Q4_K_M. The load guard is the backstop
        # if Q8 behaves differently, and nothing routes here by default, so the first load is
        # the measurement. Pins to one slot, as any speculative model does.
        extra_server_args=(
            "--spec-type",
            "draft-mtp",
            "--spec-draft-n-max",
            "3",
            # q8_0 KV. `-fa` is served unconditionally, which llama.cpp requires for a
            # quantised cache. MEASURED 2026-08-21: this family's served KV is the single
            # largest window-scaling cost on the box — 8.0 GiB per 128k at f16, more than
            # half the weights again at the full window — and q8_0 halves the cache proper
            # (MODEL_PROMPTING.md derives 4.25 from 2 x 4 x 256 x 1.0625 x 16 layers).
            #
            # `_QWEN38_KV_GB_PER_128K` MOVES WITH THIS and the two may never be changed
            # apart: serving a quantised cache while budgeting f16 over-reserves every one
            # of these models by ~4 GiB, which evicts models that fit — and the inverse,
            # budgeting q8 while serving f16, under-reserves on the box's freeze path.
            "-ctk",
            "q8_0",
            "-ctv",
            "q8_0",
        ),
        image_min_tokens=2048,
        quant="Q8_0",
        # GiB on disk (the catalog's unit): the single Q8_0 weight (~27.0 GiB from HF's 29
        # decimal-GB listing) plus the ~0.86 GiB F16 projector. ESTIMATE until measured on-box;
        # kept at the GiB (not decimal-GB) sum so the install bar doesn't cap early.
        size_gb=27.9,
        note="Dense 27B (text + vision, image & video) — Qwen3.8's hybrid reasoner at 8-bit "
        "(near-lossless), the compact vision + high-tier entry. "
        "A DENSE 27B is memory-bandwidth-bound on this box, so Q8 runs ~7 t/s "
        "(quality-first / batch) — prefer the Q4 twin for interactive use. Thinking is the "
        "enable_thinking chat-template toggle, set per task in LLM Settings ('none' runs it as a "
        "snappy Instruct model). ~28 GiB, co-resides beside gpt-oss-120b. Needs a llama.cpp "
        "build with --reasoning-format and Qwen3.8 mmproj support.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        thinking_effort_map=dict(QWEN38_EFFORT_LEVELS),
        # Native 262k (YaRN-extensible to ~1M upstream); serves the conservative gateway
        # default with the native window as the picker's ceiling.
        native_context_window=262144,
        kv_gb_per_128k=_QWEN38_KV_GB_PER_128K,
        recurrent=True,
        kv_slot_restorable=True,  # hybrid+MTP, reasoned + verified live (2026-08-23)
        checkpoint_gb=0.28,
    ),
    LocalModel(
        id="qwen3.8-27b-q4",
        label="Qwen3.8 27B · vision + reasoning (Q4, interactive)",
        served_model="qwen3.8-27b-q4",
        # Same model as the Q8 twin — same hybrid thinking/non-thinking sampling split.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0),
        tiers=("vision", "high"),
        supports_vision=True,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.8-27B-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        # Same F16 projector as the Q8 twin (kept full precision even at Q4 weights). Exact
        # name, not `mmproj*F16.gguf`, so it doesn't also pull the `mmproj-BF16.gguf` beside it.
        mmproj_include="mmproj-F16.gguf",
        # Same grounding floor as the Q8 twin — see the note there.
        # MTP (multi-token prediction / self-speculative decoding) is a SERVING MODE of these
        # same weights, not a different model — unsloth's GGUF already carries the MTP head
        # (`blk.*.nextn.*`), which llama.cpp ignores without the flag. It used to be a separate
        # catalog entry, which meant two identical 16.8 GB entries differing only in flags.
        #
        # Measured on this box at 128k: 26.39 GiB resident against 26.02 for the same weights
        # served without it, 2.40 tokens per forward pass, 22.41 t/s against ~11-12 unspecul-
        # ated. Vision is unaffected — the projector and the MTP head coexist (19.07 GiB at
        # 32k, +0.11 GiB for a full-resolution image encode, correct captions and OCR).
        #
        # NOTE: this pins the model to ONE slot (see llama_swap_config) — llama.cpp's
        # speculative path serves a single sequence and acceptance collapses as concurrent
        # sequences rise, so the serving mode overrides the interactive-slot setting.
        extra_server_args=(
            "--spec-type",
            "draft-mtp",
            "--spec-draft-n-max",
            "3",
            # q8_0 KV. `-fa` is served unconditionally, which llama.cpp requires for a
            # quantised cache. MEASURED 2026-08-21: this family's served KV is the single
            # largest window-scaling cost on the box — 8.0 GiB per 128k at f16, more than
            # half the weights again at the full window — and q8_0 halves the cache proper
            # (MODEL_PROMPTING.md derives 4.25 from 2 x 4 x 256 x 1.0625 x 16 layers).
            #
            # `_QWEN38_KV_GB_PER_128K` MOVES WITH THIS and the two may never be changed
            # apart: serving a quantised cache while budgeting f16 over-reserves every one
            # of these models by ~4 GiB, which evicts models that fit — and the inverse,
            # budgeting q8 while serving f16, under-reserves on the box's freeze path.
            "-ctk",
            "q8_0",
            "-ctv",
            "q8_0",
        ),
        image_min_tokens=2048,
        quant="Q4_K_M",
        # GiB on disk: the Q4_K_M weight (~15.9 GiB from HF's 17.1 decimal-GB listing) plus the
        # ~0.86 GiB F16 projector. ESTIMATE until measured on-box.
        size_gb=16.8,
        note="Dense 27B (text + vision) hybrid reasoner at Q4_K_M — the INTERACTIVE twin of the "
        "Q8 entry, same model + repo. A dense 27B is bandwidth-bound here, so Q4 roughly doubles "
        "Q8's throughput: the better daily driver, at some quality cost the Q8 twin keeps. ~17 "
        "GiB, so it co-resides beside gpt-oss-120b with wide headroom. Projector stays F16 "
        "(fine-text OCR degrades first under weight quantization). Thinking is the "
        "enable_thinking toggle, set per task in LLM Settings.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        thinking_effort_map=dict(QWEN38_EFFORT_LEVELS),
        native_context_window=262144,
        kv_gb_per_128k=_QWEN38_KV_GB_PER_128K,
        recurrent=True,
        kv_slot_restorable=True,  # hybrid+MTP, reasoned + verified live (2026-08-23)
        checkpoint_gb=0.28,
    ),
    LocalModel(
        id="qwen3.8-27b-abliterated",
        label="Qwen3.8 27B · abliterated (red-team probe)",
        served_model="qwen3.8-27b-abliterated",
        # Same base weights as the qwen3.8-27b twins, so the same Qwen3.8 card sampling: the
        # abliteration edits refusal directions out of the residual stream, it does not change
        # what the card recommends sampling at.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0),
        tiers=("vision", "high"),
        supports_vision=True,
        supports_tools=True,
        # NEVER recommended, and nothing routes here by default. This is a PROBE, not a worker:
        # it exists so the owner can red-team the air-gapped sandbox with a model that will not
        # refuse the prompt under test. Putting it on a real task would put the embedded prompt
        # below in front of every JBrain system prompt (see the note).
        recommended=False,
        hf_repo="Blackfrost-AI/Qwen3.8-27B-ABLITERATED-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        # EXACT name, not `mmproj*F16.gguf`: this repo ships a `mmproj-Qwen3.8-27B-ABLITERATED-
        # Q8_0.gguf` beside the F16 one, and the projector stays full precision even at Q4
        # weights (fine text degrades first under quantization, as on the other Qwen entries).
        mmproj_include="mmproj-Qwen3.8-27B-ABLITERATED-F16.gguf",
        # Identical serving shape to the qwen3.8-27b-q4 twin, and that is VERIFIED rather than
        # assumed: the published GGUF header parses as arch `qwen35`, `block_count` 65,
        # `full_attention_interval` 4, `context_length` 262144, and carries the MTP head
        # (`blk.64.nextn.*`) these flags need. The vendor rebuilt every quant with the head
        # embedded (2026-08-16) precisely so no separate draft file is required. Pins to one
        # slot, as any speculative entry does. The image floor is the field below, not a flag
        # here, so an operator override replaces it rather than landing twice on the command line.
        extra_server_args=(
            "--spec-type",
            "draft-mtp",
            "--spec-draft-n-max",
            "3",
            # q8_0 KV. `-fa` is served unconditionally, which llama.cpp requires for a
            # quantised cache. MEASURED 2026-08-21: this family's served KV is the single
            # largest window-scaling cost on the box — 8.0 GiB per 128k at f16, more than
            # half the weights again at the full window — and q8_0 halves the cache proper
            # (MODEL_PROMPTING.md derives 4.25 from 2 x 4 x 256 x 1.0625 x 16 layers).
            #
            # `_QWEN38_KV_GB_PER_128K` MOVES WITH THIS and the two may never be changed
            # apart: serving a quantised cache while budgeting f16 over-reserves every one
            # of these models by ~4 GiB, which evicts models that fit — and the inverse,
            # budgeting q8 while serving f16, under-reserves on the box's freeze path.
            "-ctk",
            "q8_0",
            "-ctv",
            "q8_0",
        ),
        # Same shipped floor as the aligned twins — the abliteration edits refusal directions,
        # it does not touch the vision tower, so the measurement behind 2048 carries over.
        image_min_tokens=2048,
        quant="Q4_K_M",
        # GiB on disk (the catalog's unit), from the repo's real blob sizes rather than a
        # decimal-GB listing: 15.66 weight + 0.86 F16 projector.
        size_gb=16.5,
        note="ABLITERATED Qwen3.8-27B — a deliberately unaligned research checkpoint, here as a "
        "RED-TEAM PROBE for exercising the sandbox's own controls, not as a worker model. The "
        "vendor measures 11 residual refusals out of 450 on R1-HARMFUL-BENCH-450 (2.4%). "
        "Two things to know before selecting it anywhere: (1) its GGUF chat template hard-codes "
        "a 'task-execution machine / never refuse, no pushback' system prompt that is emitted "
        "ABOVE the caller's own system message on every turn, with no way to switch it off from "
        "the API — so it displaces JBrain's prompts and is itself part of what the refusal score "
        "measures; (2) it is vendor-labelled EXPERIMENTAL. Same base as the qwen3.8-27b twins "
        "(dense 27B, text + vision, hybrid thinking, ~16.5 GiB at Q4_K_M), so it co-resides "
        "beside gpt-oss-120b and behaves identically on everything except refusal.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # The abliteration kept Qwen3.8's reasoning plumbing intact: the shipped template still
        # reads `enable_thinking` and `reasoning_effort`, still defaults the level to `xhigh`,
        # and — unlike the upstream template — RAISES on a level outside (xhigh, medium, low).
        # Our three mapped values are exactly that set, so the map is load-bearing twice over
        # here: without it every thinking call runs at xhigh, and a wrong level is a hard error
        # rather than a silent ignore.
        thinking_effort_map=dict(QWEN38_EFFORT_LEVELS),
        native_context_window=262144,
        kv_gb_per_128k=_QWEN38_KV_GB_PER_128K,
        recurrent=True,
        kv_slot_restorable=True,  # hybrid+MTP, reasoned + verified live (2026-08-23)
        checkpoint_gb=0.28,
    ),
    LocalModel(
        id="qwen3-coder-next",
        label="Qwen3-Coder-Next 80B · coding agent (Q4)",
        served_model="qwen3-coder-next",
        # Qwen3-Coder-Next card: temp 1.0 / top_p 0.95 / top_k 40 (the Coder line uses 40, not
        # the 20 the rest of the Qwen family uses). Agentic coder, no separate thinking mode.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=40, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        # Opt-in: code mode (jcode) provisions this via scripts/jcode-setup.sh; it is
        # NOT recommended, so a plain local-hosting enable never pulls its ~50 GB.
        recommended=False,
        hf_repo="unsloth/Qwen3-Coder-Next-GGUF",
        gguf_include="*UD-Q4_K_XL*.gguf",
        mmproj_include=None,
        quant="UD-Q4_K_XL",
        size_gb=49.6,
        note="80B MoE, 3B active — agentic coder (~70% SWE-Bench Verified); the model "
        "behind code mode (jcode). Co-resides beside another large model. Uses the "
        "Qwen3-Next hybrid-attention arch — confirm the gateway's llama.cpp "
        "build supports it (a recent build fixed a Qwen looping bug). Served at its full "
        "native 256k window: jcode's terminal `claude` wants the whole context, and the "
        "light hybrid-attention KV (~10 GB at 256k) fits beside the weights here.",
        # Code mode wants the whole window — serve the full native 256k (not the small
        # memory-bound default) so the coder gets full context.
        context_window=262144,
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="qwen3-coder-next-q8",
        label="Qwen3-Coder-Next 80B · coding agent (Q8)",
        served_model="qwen3-coder-next-q8",
        # Same model as the Q4 twin — same Coder card sampling.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=40, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        # Opt-in, standalone high-fidelity coder for a box that PINS one jcode model.
        # Not recommended (a plain local-hosting enable never pulls its ~85 GB).
        recommended=False,
        hf_repo="unsloth/Qwen3-Coder-Next-GGUF",
        # Sharded into a Q8_0/ subdir; the glob matches each shard's path (same shape
        # as the 235B's UD-Q3_K_XL/ subdir). The config generator resolves the shards.
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # ~85 GB (8-bit of 80B) — an ESTIMATE until measured on disk; the install bar
        # tolerates it. Runs STANDALONE on a 128 GB box: it will NOT co-reside with
        # gpt-oss-120b, so expect a cold load on every switch and a tight context
        # budget beside the weights. If the gateway's llama.cpp build won't load Q8 on
        # gfx1151, fall back to the Q4 entry above.
        size_gb=85.0,
        note="80B MoE, 3B active — agentic coder at 8-bit (near-lossless) for jcode "
        "pinned to one model. Standalone only on a 128 GB box; cold-loads on switch. "
        "Same hybrid-attention arch — confirm the llama.cpp build serves Q8 on gfx1151. "
        "Served at full native 256k (standalone, so the window has the most room).",
        # Code mode wants the whole window — serve the full native 256k.
        context_window=262144,
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="glm-4.5-air",
        label="GLM-4.5 Air · reasoning (alt)",
        served_model="glm-4.5-air",
        # Z.ai's GLM-4.5-series API default: temp 0.6 / top_p 0.95 (the 4.5 series defaults to
        # 0.6, distinct from 4.6+). No official top_k/min_p, so disable them for pure temp/top_p.
        sampling=Sampling(temperature=0.6, top_p=0.95, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/GLM-4.5-Air-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        mmproj_include=None,
        quant="Q4_K_M",
        size_gb=70.0,
        note="70B-class quality, MoE-fast; alternate high tier.",
        supports_reasoning=True,
        # Native 128k; serves the gateway default.
        native_context_window=131072,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="qwen3-30b-a3b",
        label="Qwen3 30B · lightweight",
        served_model="qwen3-30b-a3b",
        # The Instruct-2507 (non-thinking) checkpoint — Qwen's non-thinking values:
        # temp 0.7 / top_p 0.8 / top_k 20. presence_penalty is optional here, left off.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0),
        tiers=("low",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="Qwen/Qwen3-30B-A3B-Instruct-2507-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        mmproj_include=None,
        quant="Q4_K_M",
        size_gb=18.0,
        note="Snappy text-only one-shots; swap-in for the low tier.",
        # Native 256k (Instruct-2507); serves the gateway default.
        native_context_window=262144,
        kv_gb_per_128k=3.2,
    ),
    LocalModel(
        id="qwen3.5-0.8b",
        label="Qwen3.5 0.8B · tiny",
        served_model="qwen3.5-0.8b",
        # Hybrid, and the card warns this tiny one is especially loop-prone — hence the high
        # presence_penalty (2.0 non-thinking, 1.5 thinking) as the mitigation.
        sampling=Sampling(temperature=1.0, top_p=1.0, top_k=20, min_p=0.0, presence_penalty=2.0),
        sampling_thinking=Sampling(
            temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5
        ),
        tiers=("low",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.5-0.8B-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # 8-bit of a 0.8B dense model — near-lossless at trivial cost. On this
        # memory-bandwidth-bound box the Q4 savings (~0.3 GB) buy nothing, so the
        # tiny model keeps its quality rather than shaving already-thin headroom.
        size_gb=0.9,
        # Measured 2026-08-23 on the box: 3.83 GiB resident after a real prefill at 32768,
        # against 1.57 declared — the flat overhead constant was 2.3 GiB light on a model
        # this small. 3.3 anchors the declaration at 4.33, +0.5 over the measurement.
        runtime_overhead_gb=3.3,
        note="Tiniest catalog model — a fast, cheap worker for side projects that "
        "don't need to be smart (classification, extraction, short one-shots). "
        "Newer generation than qwen3-30b: a hybrid reasoner whose thinking is a "
        "chat-template toggle. Its level is set per task in LLM Settings (pick "
        "'none' to run it as a snappy Instruct model, or a thinking level for the "
        "extra depth). Loads instantly and co-resides beside anything.",
        # A hybrid Qwen: emits <think> when thinking is on, so parse it onto the
        # reasoning channel (deepseek) and drive the on/off via the hybrid toggle.
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 256k; serves the conservative gateway default like the other low-tier
        # entries. Its KV cache is negligible at this size, so a big -c is cheap here.
        native_context_window=262144,
        kv_gb_per_128k=0.5,
    ),
    LocalModel(
        id="qwen3.5-4b",
        label="Qwen3.5 4B · small",
        served_model="qwen3.5-4b",
        # Hybrid: non-thinking temp 0.7 / top_p 0.8, thinking temp 1.0 / top_p 0.95; both keep
        # presence_penalty 1.5 (Qwen3.5's anti-loop default), top_k 20.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(
            temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5
        ),
        tiers=("low",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.5-4B-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # 8-bit of a 4B dense model (~4.3 GB) — the step up from 0.8b when the tiny
        # model is too weak but you still want an instant, low-footprint local worker.
        size_gb=4.3,
        # FLOOR-ANCHORED, not fully measured: on 2026-08-23 the runaway watchdog aborted this
        # model's load at 12.8 GiB GTT, still climbing, against 5.15 declared — the same flat-
        # overhead defect as the 0.8b, scaled up. 9.5 puts the declaration at 14.1, above the
        # abort floor with margin; verify against a completed load once this ships (the old
        # under-prediction also set the watchdog ceiling too low to let one finish).
        runtime_overhead_gb=9.5,
        note="Small dense model — noticeably smarter than qwen3.5-0.8b while still "
        "loading instantly and co-residing beside anything. A solid low-tier daily "
        "driver for local one-shots. A hybrid reasoner: set its thinking level per "
        "task in LLM Settings ('none' runs it as a snappy Instruct model); tools on.",
        # A hybrid Qwen: emits <think> when thinking is on, so parse it onto the
        # reasoning channel (deepseek) and drive the on/off via the hybrid toggle.
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 256k; serves the conservative gateway default like the other low-tier
        # entries. A dense 4B KV stays cheap here, so a big -c is affordable.
        native_context_window=262144,
        kv_gb_per_128k=1.2,
    ),
    LocalModel(
        id="llama-3.3-70b",
        label="Llama 3.3 70B · batch (slow)",
        served_model="llama-3.3-70b",
        # Meta's shipped default (generation_config.json): temp 0.6 / top_p 0.9. No Meta
        # top_k/min_p, so disable them for pure temp/top_p sampling.
        sampling=Sampling(temperature=0.6, top_p=0.9, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="bartowski/Llama-3.3-70B-Instruct-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        mmproj_include=None,
        quant="Q4_K_M",
        size_gb=40.0,
        note="Dense 70B — high quality but only ~5 t/s here; batch use only.",
        # Native 128k; serves the gateway default. Dense KV — a big -c costs the most here.
        native_context_window=131072,
        kv_gb_per_128k=8.0,
    ),
)

_BY_ID = {m.id: m for m in CATALOG}

# DECOMMISSIONED model ids — entries removed from the catalog above that must be
# actively UNINSTALLED from any box still holding them, not merely dropped from the
# picker. deploy/local-models-sync.sh reads this (via `retired_ids()`) and, for any
# retired id actually present on a box, force-subtracts it from the served roster and
# prunes its weights on the next update — the no-shell path to retiring a model the
# operator can't `rm` themselves (CLAUDE.md #10). A box that never installed one is
# unaffected (it stays off the fast-path only while a retired weight lingers).
# The 3.6 pair is superseded by qwen3.8-27b / -q4 (its newer-generation twins), so nothing
# routes there. `qwen3.8-27b-mtp` is a different retirement: MTP turned out to be a serving MODE
# of the Q4 entry rather than a model, so the duplicate entry was dropped from CATALOG — but it
# was never listed here, which is not the same thing. Dropping an id from the catalog only hides
# it from the picker; a box that installed it keeps ~16.8 GB of weights under
# /models/qwen3.8-27b-mtp/ that nothing will ever serve and the owner cannot `rm` (CLAUDE.md
# #10). Listing it is what actually reclaims the disk on the next update.
RETIRED_IDS: tuple[str, ...] = ("qwen3.6-27b", "qwen3.6-27b-q4", "qwen3.8-27b-mtp")

# Served-model names that emit reasoning + honor `reasoning_effort`. The router
# consults this to decide whether a `local:<served_model>` call may carry an effort
# (and the loop/UI surface the thinking trace only for these).
REASONING_SERVED_MODELS: frozenset[str] = frozenset(
    m.served_model for m in CATALOG if m.supports_reasoning
)


_BY_SERVED = {m.served_model: m for m in CATALOG}

# Fallback window for a `local:<served_model>` spec we don't recognize (an operator
# serving a model outside the catalog): the gateway's default `-c` for the set.
DEFAULT_LOCAL_CONTEXT_WINDOW = 32768


def get(model_id: str) -> LocalModel | None:
    return _BY_ID.get(model_id)


def context_window(served_model: str) -> int:
    """The context window a `local:<served_model>` runs with — the catalog value
    when known, else the gateway's default. Drives the PWA's context-usage meter."""
    model = _BY_SERVED.get(served_model)
    return model.context_window if model else DEFAULT_LOCAL_CONTEXT_WINDOW


def supports_vision(served_model: str) -> bool:
    """Whether a `local:<served_model>` can accept image content. False for a served
    name outside the catalog — the safe default that drops image bytes rather than
    sending them to a model with no vision projector (which errors at the gateway)."""
    model = _BY_SERVED.get(served_model)
    return model.supports_vision if model else False


def id_for_served(served_model: str) -> str | None:
    """Catalog id for a served-model name (the gateway loads/reports served names,
    but per-model settings — overrides, staging — key off the catalog id), or None
    for a served name outside the catalog."""
    model = _BY_SERVED.get(served_model)
    return model.id if model else None


def get_by_served(served_model: str) -> LocalModel | None:
    """The catalog entry a gateway `served_model` name maps to, or None for a served
    name outside the catalog (an operator serving something unlisted)."""
    return _BY_SERVED.get(served_model)


# The context length KV estimates are normalized to: kv_gb_per_128k is the KV cache at
# 131072 tokens, and KV scales linearly with the served window. (Defined near the top, beside
# the runtime-overhead and vision terms, because load_footprint_gb needs them.)


def _kv_gb(model: LocalModel, window: int, slots: int) -> float:
    """KV cache (GiB) for `model` held at `window` tokens across `slots` parallel slots.

    Linear off the 128k reference. `--swa-full` (`kv_full_history`) gives the sliding-window
    layers a full-size cache instead of a ring, which roughly doubles the term — counted here
    so the load reservation, the eviction budget and the settings meter cannot drift apart."""
    kv = model.kv_gb_per_128k * window / _KV_REFERENCE_TOKENS * model.effective_slots(slots)
    return kv * 2 if model.kv_full_history else kv


def footprint_gb(
    model: LocalModel, window: int, *, disk_gb: float | None = None, slots: int = 1
) -> float:
    """Total unified-memory footprint (GiB) of `model` held resident at `window`
    tokens: weights + KV cache. Weights = the measured on-disk size when known
    (`disk_gb`), else the catalog's nominal `size_gb`; KV scales linearly off the 128k
    reference (`kv_gb_per_128k * window / 131072`) — the same figures the settings
    memory meter shows. `slots` (llama-server `-np`) multiplies the KV: each parallel
    slot holds its own `window`-sized cache, so a second slot (the interactive
    keep-warm slot) doubles the KV cost while the weights are shared. On a Strix Halo
    box the iGPU draws from unified system RAM, so this one number is the whole cost of
    keeping the model loaded. The residency budget compares it against live free RAM.

    `slots` goes through `effective_slots`, so a speculative model costs one slot's KV even
    with a larger override saved — matching what the gateway will really serve rather than
    reserving for slots the engine won't allocate."""
    weights = disk_gb if disk_gb is not None else model.size_gb
    # `--swa-full` doubling lives in `_kv_gb`: omitting it under-reported the model by several
    # GB in both the meter and the eviction budget — on a box that has hard-locked under
    # memory pressure.
    kv = _kv_gb(model, window, slots)
    # Context checkpoints: per slot, and on a hybrid a full copy of the recurrent state each.
    #
    # HOST RAM, not device memory, despite what this comment said for as long as it existed:
    # `common_prompt_checkpoint` holds `std::vector<uint8_t>` buffers (llama.cpp common/common.h).
    # CONFIRMED on the box — raising the count from 2 to 16 left GTT at 26.21 GiB, unchanged to
    # the centibyte. It still belongs in this total because on Strix Halo host and device draw
    # on one physical pool, so the budget counts it either way; what changes is that checkpoints
    # do NOT add to the GTT cap pressure that is this box's documented hang mode.
    #
    # Budgeted at the SERVED count — an operator override through `--ctx-checkpoints` is not
    # threaded in here, which is why that flag is bounded in the settings API rather than left
    # open.
    checkpoints = (
        model.checkpoint_gb * ctx_checkpoints(model.checkpoint_gb) * model.effective_slots(slots)
    )
    return round(
        weights
        + kv
        + checkpoints
        + CACHE_RAM_GB
        + _runtime_overhead_gb(model)
        + _vision_resident_gb(model),
        2,
    )


def _runtime_overhead_gb(model: LocalModel) -> float:
    """Everything a resident model pins that is neither weights nor KV: the recurrent state of
    any linear-attention layers, compute and output buffers, and the MTP draft context on a
    speculative entry. Small individually, but omitting all of them is what made the MTP
    entry's estimate 3 GiB light against the measurement."""
    base = (
        model.runtime_overhead_gb if model.runtime_overhead_gb is not None else RUNTIME_OVERHEAD_GB
    )
    return base + (MTP_OVERHEAD_GB if model.is_speculative else 0.0)


def _vision_resident_gb(model: LocalModel) -> float:
    """The CLIP attention buffer a vision model reaches once it has encoded a full-resolution
    image, and then holds until unload (there is no shrink path — see vision_attn_buffer_gb).

    In `footprint_gb` rather than only in the load estimate because it is PERSISTENT: the
    earlier code had this backwards, reserving it for the load and hiding it from the eviction
    budget, which is the arrangement that lets a box drift into trouble after the load
    succeeded. Nothing for a text-only entry."""
    return vision_attn_buffer_gb() if model.mmproj_include else 0.0


def load_footprint_gb(model: LocalModel, window: int | None = None, *, slots: int = 1) -> float:
    """Device memory to have free before loading `model`, in GiB.

    Weights + KV at the window it will actually be SERVED at + runtime overhead, and for a
    vision model the CLIP attention buffer at its LOAD-TIME warmup size rather than its
    eventual peak. llama.cpp warms the projector at a capped 46x46 = 2116 image tokens
    (`set_warmup_n_tokens`), and the full-resolution buffer only materialises on the first real
    image — which may be much later, or never. Reserving the peak here would refuse loads that
    are genuinely safe; the peak is the eviction budget's problem (`footprint_gb`), which is
    where it now lives.

    `window` and `slots` are the OPERATOR-RESOLVED values (Settings → LLM per-model overrides),
    the same pair the gateway config generator writes as `-c` / `-np` and the same pair the
    eviction budget sizes with. They default to the catalog's own window and one slot, which is
    what an unconfigured box serves.

    Passing them is not optional in practice. This function took no window at all until a
    measurement caught it: the abliterated 27B is served at `-c 262144` against a catalog
    default of 32768, so the pre-flight reserved 20.29 GB for a load that measured 36.92 GB —
    1.8x light, on the one code path whose stated job is refusing a load rather than freezing
    the host. KV is linear in the window, so any override at all moves this number.

    Kept distinct from `footprint_gb` because the two answer different questions, but note the
    difference is now much smaller than the old code assumed — and pointed the other way."""
    served_window = model.context_window if window is None else window
    total = model.size_gb + _kv_gb(model, served_window, slots) + _runtime_overhead_gb(model)
    if model.mmproj_include:
        total += vision_attn_buffer_gb(_VISION_WARMUP_IMAGE_TOKENS)
    return round(total, 2)


def declared_gb(
    model: LocalModel, window: int, *, disk_gb: float | None = None, slots: int = 1
) -> tuple[float, float]:
    """The reservation ledger's two columns for one instance: (host_gb, device_gb).

    THE ONE PLACE A DECLARATION IS COMPUTED. The ledger charges a row at intent and holds that
    charge, unchanged, for the instance's whole life — so this number is not an estimate the
    system revisits, it is the promise the box is then protected by. Derived here rather than at
    the call sites because a second derivation is a second answer, and eight uncoordinated
    memory budgets is the state this work exists to end.

    THE SPLIT IS REAL EVEN THOUGH THE POOL IS ONE. On Strix Halo the iGPU draws GTT from system
    RAM, so every device byte is also a host byte — device is a SUBSET of host, never a second
    pool to add on. What distinguishes them is what is host-ONLY: today that is the context
    checkpoints (`common_prompt_checkpoint` holds `std::vector<uint8_t>`, CONFIRMED on the box —
    raising the count from 2 to 16 left GTT unchanged to the centibyte), plus `--cache-ram`,
    which is carried in the sum but currently contributes NOTHING because `CACHE_RAM_GB` is 0.0
    — it is here so that flipping the serving flag moves this number with it, not because it is
    doing work today. Those cost host RAM and add nothing to the GTT cap pressure that is this
    box's documented hang mode, so charging them to the device column would refuse loads that
    are safe, and charging them to neither is how the host ran out while GTT looked
    comfortable.

    The vision attention buffer is counted at its RESIDENT size, not the load-time warmup size
    `load_footprint_gb` uses. That difference is deliberate and points the other way from the
    usual: a pre-flight asks "what will be allocated in the next three minutes", where the
    warmup cap is right; a reservation asks "what will this instance be holding for its whole
    life", and `ggml_gallocr_reserve_n_impl` only ever grows the allocation — a smaller later
    image releases nothing. Declaring the warmup figure would silently under-reserve every
    vision model from its first real image onward."""
    device = (
        (disk_gb if disk_gb is not None else model.size_gb)
        + _kv_gb(model, window, slots)
        + _runtime_overhead_gb(model)
        + _vision_resident_gb(model)
    )
    host_only = (
        model.checkpoint_gb * ctx_checkpoints(model.checkpoint_gb) * model.effective_slots(slots)
    ) + CACHE_RAM_GB
    return round(device + host_only, 2), round(device, 2)


def recommended_ids() -> tuple[str, ...]:
    """The default-enabled set the install prompt offers first."""
    return tuple(m.id for m in CATALOG if m.recommended)


def retired_ids() -> tuple[str, ...]:
    """Decommissioned model ids the sync must force-uninstall + prune on the next update
    (RETIRED_IDS). These are no longer in CATALOG, so they can never be re-selected — the
    list only drives the removal of weights left on a box from before they were retired."""
    return RETIRED_IDS


def selected(ids: Sequence[str]) -> tuple[LocalModel, ...]:
    """Catalog entries for the given ids, in catalog order; unknown ids dropped."""
    wanted = set(ids)
    return tuple(m for m in CATALOG if m.id in wanted)


def jcode_models(local_llm_enabled: bool, local_models: Sequence[str]) -> tuple[LocalModel, ...]:
    """Installed, tool-capable local models — the set code mode (jcode) can run, in catalog
    order. The single source of truth for three consumers that must agree: the jcode model
    dropdown (llm_settings), the sandbox's grok `/model` list, and the residency-aware jcode
    proxy's allow-list (api.jcode_llm). jcode is a tool-using agent, so non-tool models are
    excluded; empty when local hosting is off (nothing installed to serve)."""
    installed = set(local_models)
    return tuple(m for m in CATALOG if local_llm_enabled and m.id in installed and m.supports_tools)


def _manifest(ids: Sequence[str]) -> str:
    """JSON download manifest for the setup script (one object per model)."""
    models = selected(ids) if ids else CATALOG
    return json.dumps([asdict(m) for m in models], indent=2)


if __name__ == "__main__":  # scripts/local-llm-setup.sh reads this
    print(_manifest(sys.argv[1:]))
