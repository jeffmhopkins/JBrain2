"""The local-model catalog and how it drives the opt-in settings choices."""

import dataclasses
import json
from typing import Any

import pytest

from jbrain.config import Settings
from jbrain.llm import local_catalog
from jbrain.llm.providers import active_local_override, provider_choices
from jbrain.llm.router import PROVIDERS, _split_spec


def test_catalog_entries_are_well_formed() -> None:
    ids = [m.id for m in local_catalog.CATALOG]
    assert len(ids) == len(set(ids)), "catalog ids must be unique"
    for m in local_catalog.CATALOG:
        # Every spec parses and names the local provider the router knows.
        provider, model = _split_spec(m.id, m.spec)
        assert provider == "local" and provider in PROVIDERS
        assert model == m.served_model
        assert m.tiers, "a model must serve at least one tier"
        # A vision-tier model must actually be vision-capable (and ship a projector).
        if "vision" in m.tiers:
            assert m.supports_vision and m.mmproj_include is not None


def test_reasoning_served_models_are_exactly_the_reasoning_capable_ones() -> None:
    # The router's gating set is derived from the catalog flag; gpt-oss and GLM-Air
    # are the reasoning models, the Qwen Instruct/VL and Llama variants are not.
    expected = {m.served_model for m in local_catalog.CATALOG if m.supports_reasoning}
    assert expected == local_catalog.REASONING_SERVED_MODELS
    assert "gpt-oss-120b" in local_catalog.REASONING_SERVED_MODELS
    assert "glm-4.5-air" in local_catalog.REASONING_SERVED_MODELS
    assert "qwen3-30b-a3b" not in local_catalog.REASONING_SERVED_MODELS


def test_recommended_set_is_the_two_resident_models() -> None:
    assert local_catalog.recommended_ids() == ("qwen3-vl-30b", "gpt-oss-120b")


def test_retired_ids_are_gone_from_the_catalog_but_flagged_for_uninstall() -> None:
    # The decommissioned Qwen3.6 pair: removed from CATALOG (unselectable, unresolvable) but
    # kept in RETIRED_IDS so the sync force-uninstalls + prunes them from any box still holding
    # them (deploy/local-models-sync.sh reads retired_ids()). Superseded by the qwen3.8 twins.
    assert local_catalog.retired_ids() == (
        "qwen3.6-27b",
        "qwen3.6-27b-q4",
        # Dropped from CATALOG when MTP became a serving mode rather than a model, but never
        # listed here — so a box that installed it kept ~16.8 GB of weights nothing serves and
        # the owner cannot delete without a shell. Removing an id from the catalog hides it;
        # only this list reclaims the disk.
        "qwen3.8-27b-mtp",
    )
    catalog_ids = {m.id for m in local_catalog.CATALOG}
    for rid in local_catalog.retired_ids():
        assert rid not in catalog_ids  # dropped from the catalog…
        assert local_catalog.get(rid) is None  # …so nothing can route to it
    # A retired id is never also a live one (the tombstone can't shadow a served model).
    assert not (set(local_catalog.retired_ids()) & catalog_ids)


def test_context_window_reads_the_catalog_then_falls_back() -> None:
    # Every catalog model serves its own window (the same value the setup script
    # stamps into the llama-swap config and the meter divides by).
    for m in local_catalog.CATALOG:
        assert local_catalog.context_window(m.served_model) == m.context_window
    # gpt-oss-120b runs its full native window; the rest use the gateway default.
    gpt_oss = local_catalog.get("gpt-oss-120b")
    assert gpt_oss is not None and gpt_oss.context_window == 131072
    # An unknown served name (a model outside the catalog) gets the safe default.
    assert (
        local_catalog.context_window("mystery-model")
        == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
        == 32768
    )


def test_max_context_window_is_native_when_known_else_the_served_default() -> None:
    # The override ceiling is the model's native window; never below its served
    # default (the picker must always be able to keep the default selected).
    for m in local_catalog.CATALOG:
        assert m.max_context_window >= m.context_window
        if m.native_context_window:
            assert m.max_context_window == m.native_context_window
        else:
            assert m.max_context_window == m.context_window
    # The coder serves its FULL native 256k window — code mode wants the whole context,
    # so its served default and native ceiling coincide.
    coder = local_catalog.get("qwen3-coder-next")
    assert coder is not None
    assert coder.context_window == 262144 and coder.max_context_window == 262144
    # gpt-oss already serves its full native window, so default and ceiling coincide.
    gpt_oss = local_catalog.get("gpt-oss-120b")
    assert gpt_oss is not None and gpt_oss.max_context_window == 131072


def test_selected_keeps_catalog_order_and_drops_unknown() -> None:
    got = local_catalog.selected(["gpt-oss-120b", "nope", "qwen3-vl-30b"])
    assert [m.id for m in got] == ["qwen3-vl-30b", "gpt-oss-120b"]


def test_manifest_is_json_with_provisioning_fields() -> None:
    manifest = json.loads(local_catalog._manifest(["qwen3-vl-30b"]))
    (entry,) = manifest
    assert entry["hf_repo"] == "Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF"
    assert entry["gguf_include"] and entry["mmproj_include"]
    assert entry["served_model"] == "qwen3-vl-30b-a3b"


def test_qwen3_vl_q4_is_a_memory_saver_vision_alt() -> None:
    # The Q4_K_M twin of the recommended Q8 Qwen3-VL: same model + repo, half the weights,
    # so it co-resides beside gpt-oss-120b instead of evicting it. Opt-in, lower OCR fidelity.
    m = local_catalog.get("qwen3-vl-30b-q4")
    assert m is not None
    assert m.tiers == ("vision", "low")
    # Vision-capable, and the projector stays F16 (fine text degrades first at low quant). The
    # include is a GLOB matching this repo's `mmproj-Qwen3VL-...-F16.gguf` (not the bare
    # `mmproj-F16.gguf` name) so the projector actually downloads — and it excludes the Q8_0 one.
    assert m.supports_vision and m.mmproj_include == "mmproj*F16.gguf"
    assert "Q8_0" not in m.mmproj_include  # the F16-only glob must not pull the Q8 projector
    assert m.supports_tools
    # Non-thinking, like the Q8 sibling — not in the reasoning gating set.
    assert not m.supports_reasoning
    assert not m.reasoning_format
    assert m.served_model not in local_catalog.REASONING_SERVED_MODELS
    # The Q4_K_M quant, pulled from the SAME official Qwen repo as the Q8 default.
    assert m.quant == "Q4_K_M"
    assert "Q4_K_M" in m.gguf_include
    assert m.hf_repo == "Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF"
    # A distinct served name from the Q8 entry (both can be provisioned side by side).
    assert m.spec == "local:qwen3-vl-30b-a3b-q4"
    assert m.served_model != local_catalog.get("qwen3-vl-30b").served_model  # type: ignore[union-attr]
    # Materially lighter than the ~32 GiB Q8 entry — the whole point is co-residence headroom.
    assert m.size_gb < local_catalog.get("qwen3-vl-30b").size_gb  # type: ignore[union-attr]
    # Serves the conservative gateway default with the native 256k as the ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    # Opt-in: the Q8 entry stays the recommended default, this is never auto-provisioned.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen3_vl_q4_includes_match_the_real_repo_filenames() -> None:
    # Regression: the bare string "mmproj-F16.gguf" matched NOTHING in the Qwen GGUF repo
    # (its projector is "mmproj-Qwen3VL-30B-A3B-Instruct-F16.gguf"), so `hf download --include`
    # and resolve_weight's glob pulled/resolved no projector — the install stuck at ~95% and the
    # gateway config would fail "download incomplete". The include feeds both, so it must fnmatch
    # the real F16 projector and the Q4_K_M weights, while excluding the Q8_0 projector/weights.
    from fnmatch import fnmatch

    m = local_catalog.get("qwen3-vl-30b-q4")
    assert m is not None and m.mmproj_include is not None
    f16 = "mmproj-Qwen3VL-30B-A3B-Instruct-F16.gguf"
    q8_proj = "mmproj-Qwen3VL-30B-A3B-Instruct-Q8_0.gguf"
    assert fnmatch(f16, m.mmproj_include), "F16 projector must match — else it never downloads"
    assert not fnmatch(q8_proj, m.mmproj_include), "must not pull the redundant Q8_0 projector"
    assert fnmatch("Qwen3VL-30B-A3B-Instruct-Q4_K_M.gguf", m.gguf_include)
    assert not fnmatch("Qwen3VL-30B-A3B-Instruct-Q8_0.gguf", m.gguf_include)


def test_llama_4_scout_is_a_vision_alt_at_int4() -> None:
    # Meta's Scout — a 109B/17B multimodal MoE at Unsloth's int4 dynamic quant, an
    # opt-in vision alternate to qwen3-vl-30b. Non-thinking (no reasoning channel).
    m = local_catalog.get("llama-4-scout-int4")
    assert m is not None
    assert m.tiers == ("vision", "low")
    # A vision-tier entry must be vision-capable and ship a projector (the well-formed
    # check enforces this too; assert the concrete facts here).
    assert m.supports_vision and m.mmproj_include == "mmproj-F16.gguf"
    assert m.supports_tools
    # Non-thinking — not in the reasoning gating set, no reasoning_format wired.
    assert not m.supports_reasoning
    assert not m.reasoning_format
    assert m.served_model not in local_catalog.REASONING_SERVED_MODELS
    # The int4 dynamic quant the manifest pulls, from Unsloth's Scout GGUF repo.
    assert m.quant == "UD-Q4_K_XL"
    assert "UD-Q4_K_XL" in m.gguf_include
    assert m.hf_repo == "unsloth/Llama-4-Scout-17B-16E-Instruct-GGUF"
    assert m.spec == "local:llama-4-scout-int4"
    # Serves the conservative gateway default; the native 10M window is capped to the
    # largest window an operator can realistically serve here (1M — ~105 GB with weights
    # + KV fits a 128 GB box), which the picker exposes as its ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 1_000_000 and m.max_context_window == 1_000_000
    # Opt-in, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen38_27b_is_a_dense_vision_hybrid_reasoner_high_tier() -> None:
    # Qwen3.8-27B: a DENSE 27B multimodal (text + vision, image & video) hybrid reasoner at
    # 8-bit — the compact vision + high-tier entry. Thinking is the enable_thinking
    # chat-template toggle (hybrid), and it emits <think> so it pins --reasoning-format deepseek.
    from fnmatch import fnmatch

    m = local_catalog.get("qwen3.8-27b")
    assert m is not None
    assert m.tiers == ("vision", "high")
    assert m.supports_vision and m.supports_tools
    # The projector include is the EXACT F16 name, not a glob: this repo also has a
    # `mmproj-BF16.gguf` that a `mmproj*F16.gguf` glob would wrongly match.
    assert m.mmproj_include == "mmproj-F16.gguf"
    assert fnmatch("mmproj-F16.gguf", m.mmproj_include)
    assert not fnmatch("mmproj-BF16.gguf", m.mmproj_include)
    # Hybrid reasoner: in the gating set, <think> split via deepseek, on/off via enable_thinking.
    assert m.supports_reasoning and m.reasoning_format == "deepseek" and m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # 8-bit (near-lossless) from Unsloth's Qwen3.8 GGUF repo; the Q4 twin shares the repo.
    assert m.quant == "Q8_0" and "Q8_0" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3.8-27B-GGUF"
    assert m.spec == "local:qwen3.8-27b"
    assert m.size_gb == 27.9
    # Its recommended sampling splits thinking vs non-thinking, like the other Qwen hybrids.
    assert m.sampling.presence_penalty == 1.5 and m.sampling.temperature == 0.7
    assert m.sampling_thinking is not None and m.sampling_thinking.temperature == 1.0
    # Serves the conservative gateway default with its native 262k window as the ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144 and m.max_context_window == 262144
    # Opt-in: the recommended default set stays the two resident models.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen38_27b_q4_is_the_interactive_twin() -> None:
    # The Q4_K_M twin of the Q8 entry: same dense 27B + repo + projector, ~17 GiB — the
    # interactive daily driver while the Q8 twin is the quality-first option.
    m = local_catalog.get("qwen3.8-27b-q4")
    q8 = local_catalog.get("qwen3.8-27b")
    assert m is not None and q8 is not None
    assert m.tiers == ("vision", "high")
    assert m.supports_vision and m.supports_tools
    # The projector stays F16 even at Q4 weights (fine text degrades first under quantization).
    assert m.mmproj_include == "mmproj-F16.gguf"
    # Same hybrid-reasoner profile as the Q8 twin.
    assert m.supports_reasoning and m.reasoning_format == "deepseek" and m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # Q4_K_M from the SAME repo as the Q8 default, but a DISTINCT served name (both can be
    # provisioned side by side) and materially lighter weights.
    assert m.quant == "Q4_K_M" and "Q4_K_M" in m.gguf_include
    assert m.hf_repo == q8.hf_repo == "unsloth/Qwen3.8-27B-GGUF"
    assert m.spec == "local:qwen3.8-27b-q4"
    assert m.served_model != q8.served_model
    assert m.size_gb == 16.8 and m.size_gb < q8.size_gb
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    assert m.id not in local_catalog.recommended_ids()


def test_qwen38_27b_q4_serves_mtp_as_a_mode_not_a_separate_entry() -> None:
    """MTP is a SERVING MODE of these weights, not a different model. It used to have its own
    catalog entry, which meant two identical 16.8 GB Q4_K_M entries differing only in flags —
    indistinguishable in the settings drawer.

    Measured at 128k before merging: 26.39 GiB resident against 26.02 for the same weights
    served without MTP, 2.40 tokens per forward pass, 22.41 t/s against ~11-12 unspeculated.
    Vision is unaffected — projector and MTP head coexist (19.07 GiB at 32k, +0.11 GiB for a
    full-resolution image encode, correct captions and OCR)."""
    m = local_catalog.get("qwen3.8-27b-q4")
    q8 = local_catalog.get("qwen3.8-27b")
    assert m is not None and q8 is not None
    assert local_catalog.get("qwen3.8-27b-mtp") is None  # the duplicate entry is gone

    # The speedup rides on flags, not a separate repo or draft file: the MTP head is already
    # in unsloth's GGUF (`blk.*.nextn.*`) and llama.cpp ignores it without --spec-type.
    assert m.is_speculative
    assert "--spec-type" in m.extra_server_args
    assert "draft-mtp" in m.extra_server_args
    assert m.hf_repo == q8.hf_repo == "unsloth/Qwen3.8-27B-GGUF"
    assert m.quant == "Q4_K_M" and "Q4_K_M" in m.gguf_include

    # Vision survives the merge, and the image floor is kept rather than dropped — now as a
    # first-class field (like context_window) rather than a raw flag, so an operator override
    # replaces it instead of being appended after it.
    assert m.supports_vision and m.mmproj_include
    # 2048, not llama.cpp's lower floor: measured three reads per floor on a label with fine
    # print, 1024 was usable one time in three (the rest invented company names) while 2048
    # read the core label every time. Pinned so a change to it is a deliberate one.
    assert m.image_min_tokens == 2048
    assert "--image-min-tokens" not in m.extra_server_args
    assert m.tiers == ("vision", "high")
    assert m.supports_tools

    # Both quants serve the same way, so the lineup differs by QUANT alone — which is the
    # whole point of collapsing the entries.
    assert q8.is_speculative and q8.quant == "Q8_0"


def test_qwen38_27b_abliterated_is_an_opt_in_red_team_probe() -> None:
    """The abliterated Qwen3.8-27B: a deliberately unaligned checkpoint for exercising the
    sandbox's own controls. It is a PROBE, so the thing worth pinning is that it can never
    arrive by default — and that it is a distinct install from the aligned twins rather than
    something that could shadow them."""
    m = local_catalog.get("qwen3.8-27b-abliterated")
    q4 = local_catalog.get("qwen3.8-27b-q4")
    assert m is not None and q4 is not None
    # Opt-in twice over: never in the recommended set the install prompt offers, and nothing
    # in the catalog routes on its own (the router's defaults are all cloud).
    assert not m.recommended
    assert m.id not in local_catalog.recommended_ids()
    # A DISTINCT repo, served name and weights dir from the aligned twins — selecting the
    # probe can never quietly replace what an aligned entry serves.
    assert m.hf_repo == "Blackfrost-AI/Qwen3.8-27B-ABLITERATED-GGUF"
    assert m.hf_repo != q4.hf_repo
    assert m.spec == "local:qwen3.8-27b-abliterated"
    assert m.served_model not in {x.served_model for x in local_catalog.CATALOG if x is not m}


def test_qwen38_27b_abliterated_includes_match_the_real_repo_filenames() -> None:
    # The globs feed BOTH `hf download --include` and resolve_weight, so a mismatch pulls
    # nothing and the install sticks partway ("download incomplete") rather than failing loudly.
    from fnmatch import fnmatch

    m = local_catalog.get("qwen3.8-27b-abliterated")
    assert m is not None and m.mmproj_include is not None
    assert fnmatch("Qwen3.8-27B-ABLITERATED-Q4_K_M.gguf", m.gguf_include)
    # The repo ships the whole K-quant ladder; the glob must select exactly one rung.
    for other in ("Q2_K", "Q3_K_M", "Q4_K_S", "Q5_K_M", "Q6_K", "Q8_0"):
        assert not fnmatch(f"Qwen3.8-27B-ABLITERATED-{other}.gguf", m.gguf_include)
    # Projector: the EXACT F16 name, because a `mmproj*` glob would also match the Q8_0
    # projector beside it and the vision tower stays full precision even at Q4 weights.
    assert m.mmproj_include == "mmproj-Qwen3.8-27B-ABLITERATED-F16.gguf"
    assert fnmatch("mmproj-Qwen3.8-27B-ABLITERATED-F16.gguf", m.mmproj_include)
    assert not fnmatch("mmproj-Qwen3.8-27B-ABLITERATED-Q8_0.gguf", m.mmproj_include)


def test_qwen38_27b_abliterated_serves_exactly_like_its_aligned_twin() -> None:
    """Same base model, so the serving shape is not re-derived: architecture, window, KV
    estimate, MTP flags and the vision floor all have to match the aligned Q4 twin. Verified
    against the published GGUF header (arch `qwen35`, block_count 65, full_attention_interval
    4, context_length 262144, MTP head at `blk.64.nextn.*`) before the entry landed."""
    m = local_catalog.get("qwen3.8-27b-abliterated")
    q4 = local_catalog.get("qwen3.8-27b-q4")
    assert m is not None and q4 is not None
    assert m.tiers == q4.tiers == ("vision", "high")
    assert m.supports_vision and m.supports_tools
    assert m.quant == q4.quant == "Q4_K_M"
    assert m.extra_server_args == q4.extra_server_args
    assert m.is_speculative
    # MTP serves one sequence, so speculation and a second slot are mutually exclusive — and an
    # explicit slot request now WINS, dropping speculation rather than being ignored.
    assert m.serves_speculative(1) and not m.serves_speculative(2)
    assert m.effective_slots(4) == 4
    # The grounding floor is a FIELD, not a raw flag, so an operator override replaces it
    # rather than appending a second --image-min-tokens. Same shipped 2048 as the aligned
    # twins: abliteration edits refusal directions, it does not touch the vision tower.
    assert m.image_min_tokens == q4.image_min_tokens == 2048
    assert "--image-min-tokens" not in m.extra_server_args
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == q4.native_context_window == 262144
    assert m.kv_gb_per_128k == q4.kv_gb_per_128k == 8.0
    # Weights + F16 projector, from the repo's real blob sizes (15.66 + 0.86 GiB).
    assert m.size_gb == 16.5


def test_qwen38_27b_abliterated_keeps_the_qwen38_reasoning_wiring() -> None:
    """The abliteration edits refusal directions, not the reasoning plumbing: the shipped
    template still reads `enable_thinking` + `reasoning_effort` and still emits <think>.

    The level map matters MORE here than on the aligned twins. This template RAISES on a
    level outside (xhigh, medium, low) instead of ignoring it, so an unmapped level is a hard
    template error rather than a silent default — and with no map at all every thinking call
    would run at the template's own `xhigh`."""
    m = local_catalog.get("qwen3.8-27b-abliterated")
    assert m is not None
    assert m.supports_reasoning and m.reasoning_format == "deepseek" and m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    assert m.thinking_effort_map == {"low": "low", "medium": "medium", "high": "xhigh"}
    # Every level we can send has to be one this template accepts, or the call 500s.
    assert set(m.thinking_effort_map.values()) <= {"xhigh", "medium", "low"}
    assert "none" not in m.thinking_effort_map  # thinking off is the toggle, not a level
    # Same card, same base weights, so the same hybrid sampling split as the aligned twins.
    q4 = local_catalog.get("qwen3.8-27b-q4")
    assert q4 is not None
    assert m.sampling == q4.sampling and m.sampling_thinking == q4.sampling_thinking


def test_qwen38_27b_abliterated_note_warns_about_the_embedded_system_prompt() -> None:
    """Its GGUF chat template hard-codes a 'never refuse, no pushback' system prompt that is
    emitted ABOVE the caller's own system message on every turn, with no API switch to
    disable it. That displaces JBrain's prompts wherever the model is selected, and it is
    itself part of what the vendor's refusal score measures — so the settings drawer, which
    renders `note` verbatim, has to say so before anyone picks it."""
    m = local_catalog.get("qwen3.8-27b-abliterated")
    assert m is not None
    note = m.note.lower()
    assert "red-team" in note  # what it is FOR, in the first line the drawer shows
    assert "chat template" in note and "system message" in note
    assert "experimental" in note


def test_reasoning_format_is_wired_only_for_the_think_emitters() -> None:
    # --reasoning-format deepseek is pinned ONLY for entries that emit <think> inline: the three
    # Qwen3.8 hybrids, the two small Qwen3.5 hybrids, and the Nemotron hybrids. The harmony/GLM
    # reasoners keep llama.cpp's auto (empty reasoning_format), so the field is NOT just a
    # synonym for supports_reasoning — renaming or mis-wiring it would fail here.
    assert {x.id for x in local_catalog.CATALOG if x.reasoning_format} == {
        "qwen3.8-27b",
        "qwen3.8-27b-q4",
        "qwen3.8-27b-abliterated",
        "nemotron-3-super-120b",
        "nemotron-3.5-lightning-30b",
        "qwen3.5-0.8b",
        "qwen3.5-4b",
    }


def test_nemotron_35_lightning_is_a_fast_hybrid_reasoner_alt_at_q8() -> None:
    # NVIDIA's Nemotron 3.5 Lightning: a 30B MoE / 3B active hybrid Mamba-2 + MoE + attention
    # reasoner at 8-bit, from the llama.cpp team's own (ggml-org) GGUF conversion. A hybrid
    # thinker (enable_thinking toggle) that emits <think>, so it pins --reasoning-format
    # deepseek like the other Nemotron/Qwen hybrids.
    m = local_catalog.get("nemotron-3.5-lightning-30b")
    assert m is not None
    assert m.tiers == ("high",)
    # Text-only — no vision tower, no projector.
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    assert m.supports_reasoning and m.reasoning_format == "deepseek" and m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # 8-bit (near-lossless) from ggml-org's GGUF (the same org as the gpt-oss GGUF).
    assert m.quant == "Q8_0" and "Q8_0" in m.gguf_include
    assert m.hf_repo == "ggml-org/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF"
    assert m.spec == "local:nemotron-3.5-lightning-30b"
    # Serves the conservative gateway default with its native 1M window as the ceiling; the
    # Mamba-2 hybrid's constant state keeps the KV term small (a big -c is cheap here).
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 1048576 and m.max_context_window == 1048576
    assert m.kv_gb_per_128k == 3.0
    # Alternate, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()


def test_nemotron_3_super_is_a_hybrid_reasoner_alt_high_tier_at_q4() -> None:
    # NVIDIA's US-made 120B/12B agentic reasoner: an alternate high-tier MoE at
    # Unsloth's UD-Q4_K_XL. A HYBRID reasoner (enable_thinking chat-template toggle)
    # that emits <think>, so it pins --reasoning-format deepseek like the Qwen hybrids.
    m = local_catalog.get("nemotron-3-super-120b")
    assert m is not None
    assert m.tiers == ("high",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    assert m.supports_reasoning
    assert m.reasoning_format == "deepseek"
    assert m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # The 4-bit dynamic quant the manifest pulls, from NVIDIA's Unsloth GGUF repo.
    assert m.quant == "UD-Q4_K_XL"
    assert "UD-Q4_K_XL" in m.gguf_include
    assert m.hf_repo == "unsloth/NVIDIA-Nemotron-3-Super-120B-A12B-GGUF"
    assert m.spec == "local:nemotron-3-super-120b"
    # Size is on-disk GiB (the catalog unit), summed from the real shards (78.0 GiB),
    # NOT HuggingFace's 83.8 decimal-GB listing — an overshoot there caps the install
    # progress bar near 93% and reads as a stall.
    assert m.size_gb == 78.0
    # Serves the conservative gateway default with its native 1M window as the ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 1048576
    # Alternate, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen35_0_8b_is_a_tiny_hybrid_reasoner_low_tier() -> None:
    # The smallest catalog entry: a fast, cheap Q8 worker for undemanding side
    # projects. A Qwen HYBRID reasoner — thinking is a chat-template toggle, so its
    # level is set per task (incl. "none" to run it as a snappy Instruct model).
    m = local_catalog.get("qwen3.5-0.8b")
    assert m is not None
    assert m.tiers == ("low",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    # Reasoning-capable and hybrid: in the gating set, <think> split via deepseek,
    # and the adapter drives its on/off through enable_thinking.
    assert m.supports_reasoning
    assert m.reasoning_format == "deepseek"
    assert m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # Q8_0 (near-lossless at this size), not the Q4 the big MoE entries use.
    assert m.quant == "Q8_0"
    assert "Q8_0" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3.5-0.8B-GGUF"
    assert m.spec == "local:qwen3.5-0.8b"
    # Serves the conservative gateway default with the full native 256k as the ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    # Opt-in, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen35_4b_is_a_small_hybrid_reasoner_low_tier() -> None:
    # The step up from the 0.8b tiny model: a small dense Q8 low-tier daily driver.
    # Also a Qwen hybrid reasoner (thinking is a chat-template toggle).
    m = local_catalog.get("qwen3.5-4b")
    assert m is not None
    assert m.tiers == ("low",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    assert m.supports_reasoning
    assert m.reasoning_format == "deepseek"
    assert m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    assert m.quant == "Q8_0"
    assert "Q8_0" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3.5-4B-GGUF"
    assert m.spec == "local:qwen3.5-4b"
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    assert m.id not in local_catalog.recommended_ids()


def test_footprint_gb_is_weights_plus_kv_scaled_by_window() -> None:
    gpt = local_catalog.get("gpt-oss-120b")
    vl = local_catalog.get("qwen3-vl-30b")
    assert gpt is not None and vl is not None
    # gpt-oss at its native 128k window: weights 59 + KV 4.5 (the 128k reference), DOUBLED to
    # 9.0 because it serves with `--swa-full` — full history on its sliding-window layers, the
    # precondition for KV-slot restore — plus the 0.55 runtime term every entry carries, plus
    # the 8.0 in-RAM prompt cache llama.cpp keeps by default (CACHE_RAM_GB): host memory, paid
    # by every resident model whether or not we write the flag, and invisible to the evictor
    # until it was added here.
    assert local_catalog.footprint_gb(gpt, 131072) == 76.55
    # The LOAD reservation deliberately excludes the prompt cache: it is a cache limit that
    # fills as prompts are served, not an allocation the load makes. Same split as the vision
    # peak and the context checkpoints.
    assert local_catalog.load_footprint_gb(gpt, 131072) == 68.55
    # KV still scales linearly with the window. vl also carries a projector, so its resident
    # figure includes the CLIP attention workspace (see vision_attn_buffer_gb): 32 + 6*(32768/
    # 131072) + 0.55 + 0.47 + 8.0 prompt cache = 42.52. The 0.47 is MEASURED with flash
    # attention on; the quadratic no-flash-attention branch would put it at 16.0 instead.
    assert local_catalog.footprint_gb(vl, 32768) == 42.52
    # Half the window → half the KV term; the vision and runtime terms do not scale with it.
    assert local_catalog.footprint_gb(vl, 16384) == 41.77
    # A measured on-disk size overrides the nominal weights estimate — and only that term;
    # KV, runtime, vision and the prompt cache are unaffected (31.9 + 1.5 + 0.55 + 0.47 + 8.0).
    assert local_catalog.footprint_gb(vl, 32768, disk_gb=31.9) == 42.42
    # A second slot doubles ONLY the KV term (weights are shared), and it compounds with
    # full-history: gpt-oss at 128k with 2 slots = 59 + 2*(2*4.5) + 0.55 runtime + 8.0 prompt
    # cache = 85.55. The number the owner trades against — halving the window takes 9 off it.
    assert local_catalog.footprint_gb(gpt, 131072, slots=2) == 85.55
    assert local_catalog.footprint_gb(gpt, 65536, slots=2) == 76.55
    assert local_catalog.footprint_gb(gpt, 131072, slots=1) == 76.55


def test_footprint_budgets_the_checkpoints_a_hybrid_actually_pins() -> None:
    """Context checkpoints are device-resident and, on a hybrid, a full copy of the recurrent
    state each. `footprint_gb` modelled none of them, so the residency evictor would co-load a
    second model against RAM already spoken for — which matters now that `--ctx-checkpoints` is
    an operator-settable flag.

    Budgeted at the SERVED count. An operator override is not threaded in here, which is exactly
    why that flag is bounded rather than left open."""
    m = local_catalog.get("qwen3.8-27b-q4")
    assert m is not None and m.checkpoint_gb == 0.15
    plain = dataclasses.replace(m, checkpoint_gb=0.0)
    delta = local_catalog.footprint_gb(m, 32768) - local_catalog.footprint_gb(plain, 32768)
    assert delta == pytest.approx(0.15 * local_catalog.CTX_CHECKPOINTS, abs=0.01)


def test_the_checkpoint_term_is_zero_where_nobody_measured_it() -> None:
    """An attention model's checkpoints are cheap KV bookkeeping, not a state copy — zero is
    correct there. The Nemotron Mamba-2 hybrids ARE recurrent, but their SSM state has a
    different shape and nobody has measured it on this box: a guessed number in a budget that
    governs host stability is worse than a documented zero. Pinned so the omission stays
    deliberate rather than becoming a silent gap again."""
    for model_id in ("gpt-oss-120b", "qwen3-vl-30b", "llama-3.3-70b"):
        model = local_catalog.get(model_id)
        assert model is not None and model.checkpoint_gb == 0.0
    for model_id in ("nemotron-3-super-120b", "nemotron-3.5-lightning-30b"):
        model = local_catalog.get(model_id)
        assert model is not None and model.checkpoint_gb == 0.0


def test_the_served_checkpoint_count_has_one_source() -> None:
    """The gateway command and the memory model must not disagree about how many checkpoints
    are pinned — the number was only in the command, which is how the footprint came to ignore
    them entirely."""
    from jbrain.llm import llama_swap_config

    assert local_catalog.CTX_CHECKPOINTS == 2
    # An import-identity check would pin nothing — reverting `render` to a literal "2" would
    # still pass it. Move the constant and the rendered command has to move with it.
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    (root / "gpt-oss-120b").mkdir()
    (root / "gpt-oss-120b" / "w-mxfp4.gguf").write_bytes(b"\0")
    manifest = [
        {
            "id": "gpt-oss-120b",
            "served_model": "gpt-oss-120b",
            "gguf_include": "*mxfp4*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
        }
    ]
    monkey = dataclasses.replace  # noqa: F841 — readability: this test mutates a module const
    original = local_catalog.CTX_CHECKPOINTS
    try:
        local_catalog.CTX_CHECKPOINTS = 7
        text = llama_swap_config.render(manifest, str(root))
        assert "--ctx-checkpoints 7" in text
    finally:
        local_catalog.CTX_CHECKPOINTS = original
    assert "--ctx-checkpoints 2" in llama_swap_config.render(manifest, str(root))


def test_get_by_served_maps_served_name_to_catalog_entry() -> None:
    m = local_catalog.get_by_served("qwen3-vl-30b-a3b")
    assert m is not None and m.id == "qwen3-vl-30b"
    assert local_catalog.get_by_served("not-a-model") is None


def _settings(**kw: Any) -> Settings:
    # Both cloud keys present — provider_choices hides a keyless cloud provider, so
    # tests that expect grok/claude to be offered must supply the keys.
    kw.setdefault("xai_api_key", "test-xai")
    kw.setdefault("anthropic_api_key", "test-anthropic")
    return Settings(database_url="postgresql+asyncpg://nobody@localhost:1/none", **kw)


def test_active_local_override_gates_reasoning_effort() -> None:
    # The agent.turn override the update's `local-activate` writes: a reasoning model carries
    # an effort (mirrors the settings PUT's REASONING_DEFAULT), a non-thinking one spec only.
    gpt = local_catalog.get("gpt-oss-120b")
    vl = local_catalog.get("qwen3-vl-30b")
    assert gpt is not None and vl is not None
    assert active_local_override(gpt) == {"spec": "local:gpt-oss-120b", "reasoning_effort": "low"}
    assert active_local_override(vl) == {"spec": "local:qwen3-vl-30b-a3b"}


def test_choices_are_cloud_only_when_local_hosting_off() -> None:
    ids = [c.id for c in provider_choices(_settings())]
    assert ids == ["grok", "claude"]


def test_choices_add_selected_local_models_when_enabled() -> None:
    choices = provider_choices(
        _settings(local_llm_enabled=True, local_models=["qwen3-vl-30b", "gpt-oss-120b"])
    )
    by_id = {c.id: c for c in choices}
    assert set(by_id) == {"grok", "claude", "qwen3-vl-30b", "gpt-oss-120b"}
    assert by_id["qwen3-vl-30b"].spec == "local:qwen3-vl-30b-a3b"
    assert by_id["qwen3-vl-30b"].supports_vision is True
    assert by_id["gpt-oss-120b"].supports_vision is False


def test_enabled_but_empty_selection_falls_back_to_generic_local() -> None:
    choices = provider_choices(_settings(local_llm_enabled=True, local_llm_model="my-model"))
    by_id = {c.id: c for c in choices}
    assert by_id["local"].spec == "local:my-model"


def test_qwen38_hybrids_publish_an_effort_level_map_and_older_hybrids_do_not() -> None:
    # Qwen3.8's chat template reads a `reasoning_effort` level ON TOP OF the enable_thinking
    # toggle its predecessors had, and applies the card's own default (`xhigh`) when given
    # none — which spends thousands of reasoning tokens on a trivial prompt. So the Qwen3.8
    # entries carry a level map; the 3.5/3.6-era hybrids, whose templates genuinely ignore the
    # field, must NOT (sending one there would be noise on the wire).
    mapped = {m.id for m in local_catalog.CATALOG if m.thinking_effort_map}
    assert mapped == {"qwen3.8-27b", "qwen3.8-27b-q4", "qwen3.8-27b-abliterated"}
    for model_id in mapped:
        model = local_catalog.get(model_id)
        assert model is not None
        # Our four settings levels onto the card's three: "none" is absent because it is the
        # toggle (thinking off), and "high" means the card's top level, which it spells xhigh.
        assert model.thinking_effort_map == {"low": "low", "medium": "medium", "high": "xhigh"}
        assert "none" not in model.thinking_effort_map
    # A level map is only meaningful for a hybrid — every model carrying one must gate on it.
    assert all(local_catalog.get(i).hybrid_thinking for i in mapped)  # type: ignore[union-attr]


def test_an_explicit_slot_request_wins_and_costs_speculation() -> None:
    """Speculation and parallel slots are mutually exclusive — llama.cpp's speculative paths
    serve ONE sequence. This used to resolve by pinning `-np` to 1 and IGNORING the operator,
    which was the worst of both: they asked for a second slot, did not get it, and got no signal.

    It now resolves the other way. A second slot is the only thing that keeps a background task
    (the auto-titler follows the interactive model) out of the slot holding a 32k primed prefix,
    and losing that prefix costs a ~100 s cold prefill — measured on the box. Trading MTP's
    decode gain (~22 vs ~11-12 t/s) for that is a real choice and it is the operator's."""
    spec = local_catalog.get("qwen3.8-27b-q4")  # MTP is a serving mode of this entry
    plain = local_catalog.get("gpt-oss-120b")  # a genuinely non-speculative contrast
    assert spec is not None and plain is not None
    assert spec.is_speculative and not plain.is_speculative
    # The request is honoured...
    assert spec.effective_slots(4) == 4
    # ...and speculation is what gives way, at exactly the point a second slot appears.
    assert spec.serves_speculative(1) is True
    assert spec.serves_speculative(2) is False
    # A non-speculative model is unaffected either way.
    assert plain.effective_slots(2) == 2
    assert plain.effective_slots(0) == 1  # ...floored at one
    assert plain.serves_speculative(1) is False


def test_footprint_follows_a_slot_override_on_a_speculative_model() -> None:
    """The residency budget must reserve for the slots the engine will ACTUALLY allocate.

    Inverted with the clamp: a second slot is now really served (speculation drops instead), so
    the budget has to carry its KV. Under the old clamp this asserted the opposite — that a saved
    2 cost one slot's KV — which was correct only while the request was being ignored."""
    mtp = local_catalog.get("qwen3.8-27b-q4")
    assert mtp is not None
    one = local_catalog.footprint_gb(mtp, 131072, slots=1)
    two = local_catalog.footprint_gb(mtp, 131072, slots=2)
    assert two > one
    # Exactly one extra slot's worth of KV and checkpoints — weights are shared.
    extra = mtp.kv_gb_per_128k + mtp.checkpoint_gb * local_catalog.CTX_CHECKPOINTS
    assert two - one == pytest.approx(extra, abs=0.01)


def test_qwen38_kv_estimate_reflects_its_hybrid_attention() -> None:
    # Qwen3.8-27B declares full_attention_interval 4: only 16 of its 64 layers are full
    # attention, the rest are Gated DeltaNet carrying a constant state. So its KV grows far
    # slower than the parameter count suggests, and the old dense-shaped 6.0 over-reserved
    # memory on a box that hard-freezes when it runs out. Pinned so a regression is loud.
    for model_id in ("qwen3.8-27b", "qwen3.8-27b-q4"):
        model = local_catalog.get(model_id)
        assert model is not None
        # DERIVED from the model's own shape, not estimated: 2 (K+V) x 4 kv_heads x 256
        # head_dim x 2 bytes (f16, llama.cpp's default — the gateway passes no --cache-type-*)
        # x 16 attention layers = 64 KiB/token, which is 8.0 GiB per 128k.
        assert model.kv_gb_per_128k == 8.0
        # This read 2.0 for a while, which is the q4_0 figure (18 KiB/token) for a cache type
        # this box does not serve — so the budget under-counted every Qwen3.8 entry by 1.5 GiB
        # at 32k and would have been 6 GiB light at a full window. If the serving flags ever
        # gain a KV quant, this must move with them (q8_0 = 4.25, q4_0 = 2.25).
        assert model.kv_gb_per_128k != 2.0


def test_full_history_doubles_the_kv_term_so_the_budget_stays_honest() -> None:
    """`--swa-full` gives the windowed layers a full-size cache. If `footprint_gb` did not
    count that, the settings meter and the eviction budget would both under-report the model
    by several GB — on a box that has hard-locked under memory pressure."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None and model.kv_full_history is True
    plain = dataclasses.replace(model, kv_full_history=False)
    delta = local_catalog.footprint_gb(model, 131072) - local_catalog.footprint_gb(plain, 131072)
    assert delta == pytest.approx(4.5, abs=0.01)  # exactly one extra kv_gb_per_128k


def test_halving_the_window_pays_for_full_history() -> None:
    """The knob that makes the flag affordable: KV scales linearly with `-c`, so serving at
    half the window costs the same RAM it did before the flag."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    plain = dataclasses.replace(model, kv_full_history=False)
    assert local_catalog.footprint_gb(model, 65536) == pytest.approx(
        local_catalog.footprint_gb(plain, 131072), abs=0.01
    )


def test_the_residency_default_matches_the_settings_default() -> None:
    """Two defaults for one number is how a planner and a guard end up disagreeing.

    `ResidencyCoordinator` defaulted to 0.25 against `Settings.local_llm_free_ram_fraction`
    = 0.15, so any construction path that did not pass the fraction explicitly reserved
    30 GiB instead of 18.2 on this box — planning against a ceiling 12 GiB tighter than the
    one the guard and the settings meter use, and refusing loads the meter said would fit.
    """
    from jbrain.config import Settings
    from jbrain.llm.residency import DEFAULT_FREE_RAM_FRACTION

    settings_default = Settings.model_fields["local_llm_free_ram_fraction"].default
    assert settings_default == DEFAULT_FREE_RAM_FRACTION


def test_every_saved_override_names_a_model_the_cost_model_knows() -> None:
    """An override for an id outside the catalog is invisible, not inert.

    `residency._footprint` returns 0.0 for a served name it cannot resolve, so a model
    running under an unknown id costs the eviction budget NOTHING and can never be chosen
    as a victim. This asserts the catalog can still resolve everything it ships; the live
    box additionally carried saved overrides for `qwen3-235b-a22b` and
    `qwen3.5-122b-a10b-mtp`, which are in neither CATALOG nor RETIRED_IDS — those are
    settings rows to clean up, and this test is what stops the catalog side regressing.
    """
    catalog = local_catalog.CATALOG
    ids = {m.id for m in catalog}
    served = {m.served_model for m in catalog}
    assert len(ids) == len(catalog), "duplicate catalog ids"
    for model in catalog:
        assert local_catalog.get_by_served(model.served_model) is not None, model.id
    assert served, "catalog is empty"
