"""The local-model catalog and how it drives the opt-in settings choices."""

import json
from typing import Any

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


def test_qwen36_27b_is_a_dense_vision_hybrid_reasoner_high_tier() -> None:
    # Qwen3.6-27B: a DENSE 27B multimodal (text + vision) hybrid reasoner at 8-bit. The compact
    # vision + high-tier entry that replaced the removed 122B/235B/Next flagships. Thinking is
    # the enable_thinking chat-template toggle (hybrid), and it emits <think> so it pins
    # --reasoning-format deepseek like the other Qwen hybrids.
    m = local_catalog.get("qwen3.6-27b")
    assert m is not None
    assert m.tiers == ("vision", "high")
    # Vision-capable and ships a projector (the well-formed check enforces this too).
    assert m.supports_vision and m.supports_tools
    # The projector include is the EXACT F16 name, not a glob: this repo also has a
    # `mmproj-BF16.gguf` that a `mmproj*F16.gguf` glob would wrongly match.
    from fnmatch import fnmatch

    assert m.mmproj_include == "mmproj-F16.gguf"
    assert fnmatch("mmproj-F16.gguf", m.mmproj_include)
    assert not fnmatch("mmproj-BF16.gguf", m.mmproj_include)
    assert not fnmatch("mmproj-F32.gguf", m.mmproj_include)
    # Hybrid reasoner: in the gating set, <think> split via deepseek, on/off via enable_thinking.
    assert m.supports_reasoning and m.reasoning_format == "deepseek" and m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # 8-bit (near-lossless) from Unsloth's Qwen3.6 GGUF repo; the Q4 twin shares the repo.
    assert m.quant == "Q8_0" and "Q8_0" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3.6-27B-GGUF"
    assert m.spec == "local:qwen3.6-27b"
    assert m.size_gb == 27.5
    # Serves the conservative gateway default with its native 262k window as the ceiling
    # (YaRN-extensible to ~1M upstream, but the arch window is what the picker exposes).
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144 and m.max_context_window == 262144
    # Opt-in: the recommended default set stays the two resident models.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen36_27b_q4_is_the_interactive_twin() -> None:
    # The Q4_K_M twin of the Q8 entry: same dense 27B + repo + projector, ~16 GiB. On this
    # bandwidth-bound box a dense 27B runs materially faster at Q4, so this is the interactive
    # daily driver while the Q8 twin is the quality-first option.
    m = local_catalog.get("qwen3.6-27b-q4")
    q8 = local_catalog.get("qwen3.6-27b")
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
    assert m.hf_repo == q8.hf_repo == "unsloth/Qwen3.6-27B-GGUF"
    assert m.spec == "local:qwen3.6-27b-q4"
    assert m.served_model != q8.served_model
    assert m.size_gb == 16.5 and m.size_gb < q8.size_gb
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    assert m.id not in local_catalog.recommended_ids()


def test_qwen38_27b_is_a_dense_vision_hybrid_reasoner_high_tier() -> None:
    # Qwen3.8-27B: the newer-generation successor to qwen3.6-27b — a DENSE 27B multimodal
    # (text + vision, image & video) hybrid reasoner at 8-bit. Thinking is the enable_thinking
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


def test_reasoning_format_is_wired_only_for_the_think_emitters() -> None:
    # --reasoning-format deepseek is pinned ONLY for entries that emit <think> inline: the two
    # Qwen3.6 hybrids, the two small Qwen3.5 hybrids, and the Nemotron hybrid. The harmony/GLM
    # reasoners keep llama.cpp's auto (empty reasoning_format), so the field is NOT just a
    # synonym for supports_reasoning — renaming or mis-wiring it would fail here.
    assert {x.id for x in local_catalog.CATALOG if x.reasoning_format} == {
        "qwen3.6-27b",
        "qwen3.6-27b-q4",
        "qwen3.8-27b",
        "qwen3.8-27b-q4",
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
    # gpt-oss at its native 128k window: weights 59 + KV 4.5 (the 128k reference) = 63.5.
    assert local_catalog.footprint_gb(gpt, 131072) == 63.5
    # KV scales linearly with the window: vl at 32k = 32 + 6*(32768/131072) = 33.5.
    assert local_catalog.footprint_gb(vl, 32768) == 33.5
    # Half the window → half the KV term (16k = 32 + 0.75).
    assert local_catalog.footprint_gb(vl, 16384) == 32.75
    # A measured on-disk size overrides the nominal weights estimate.
    assert local_catalog.footprint_gb(vl, 32768, disk_gb=31.9) == 33.4
    # A second slot doubles ONLY the KV term (weights are shared): gpt-oss at 128k with 2
    # slots = 59 + 2*4.5 = 68.0. slots<=1 leaves it unchanged.
    assert local_catalog.footprint_gb(gpt, 131072, slots=2) == 68.0
    assert local_catalog.footprint_gb(gpt, 131072, slots=1) == 63.5


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
