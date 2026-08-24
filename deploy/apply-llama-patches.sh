#!/usr/bin/env bash
# Apply JBrain's local llama.cpp patches by ANCHOR (not line number), so they
# survive minor upstream drift around the touched code. Run inside the builder
# stage of Dockerfile.local-llm against a checked-out llama.cpp tree.
#
# Each patch file in deploy/patches/*.patch documents its own anchor line and the
# block to insert after it; this script re-encodes that as an idempotent Python
# insertion. A patch that cannot find its anchor is a HARD failure (exit 1) — a
# silently-unapplied patch would ship the unpatched binary while claiming the fix.
set -euo pipefail

SRC="${1:?usage: apply-llama-patches.sh <llama.cpp source dir>}"
TARGET="$SRC/tools/server/server-context.cpp"

if [ ! -f "$TARGET" ]; then
  echo "[llama-patch] FATAL: $TARGET not found (llama.cpp layout changed?)" >&2
  exit 1
fi

python3 - "$TARGET" <<'PY'
import sys

target = sys.argv[1]
src = open(target).read()

# The restore success path has been worded differently across llama.cpp versions;
# support every known wording so the patch survives a base-image bump without a
# hand edit (first anchor found wins — they never coexist in one version).
#   - "insert(tokens)":      the token-vector path (base b10068 / 571d0d540)
#   - "std::move(restored)": the server_tokens::deserialize rework (b10603+)
anchors = [
    "slot->prompt.tokens.insert(tokens);",
    "slot->prompt.tokens = std::move(restored);",
]

# The block mirrors create_checkpoint() (server-context.cpp) minus its
# completion-task coupling: a restore runs as a slot-action task, so there is no
# owning task (id_task = -1). n_tokens is the count already IN the KV — after a
# restore, the whole prompt (the reuse path clamps n_past to it, so 0 here would
# make the checkpoint useless). pos_min/pos_max are read from the model's own
# memory exactly like create_checkpoint's caller does — for SWA/hybrid memory the
# floor is not necessarily 0, and overclaiming coverage corrupts reuse decisions.
block = '''
                        // --- JBrain patch: reuse-on-restore for SWA/hybrid/recurrent ---
                        // A slot-restore rebuilds the memory state and repopulates
                        // slot->prompt.tokens, but for a SWA/hybrid/recurrent model the
                        // completion-path prompt reuse is gated on a CONTEXT CHECKPOINT.
                        // With none registered the reuse hits do_reset and re-prefills the
                        // whole prompt. Seed one checkpoint spanning the restored prompt so
                        // the restore is actually reusable (harmless for pure-attention).
                        if (params_base.n_ctx_checkpoints > 0 && slot->prompt.tokens.size() > 0) {
                            const auto ckpt_pos_min = llama_memory_seq_pos_min(llama_get_memory(ctx_tgt), slot->id);
                            const auto ckpt_pos_max = llama_memory_seq_pos_max(llama_get_memory(ctx_tgt), slot->id);
                            if (ckpt_pos_min >= 0 && ckpt_pos_max >= ckpt_pos_min) {
                                auto & cur = slot->prompt.checkpoints.emplace_back();
                                cur.id_task = -1;  // not owned by a completion task
                                cur.update_pos((int64_t) slot->prompt.tokens.size(), ckpt_pos_min, ckpt_pos_max);
                                cur.update_tgt(ctx_tgt, slot->id, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
                                cur.update_dft(ctx_dft, slot->id, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
                                common_speculative_get_state(spec.get(), slot->id, cur.data_spec);
                                SLT_TRC(*slot, "seeded restore checkpoint (n_tokens = %d, pos_min = %d, pos_max = %d)\\n",
                                    (int) slot->prompt.tokens.size(), (int) ckpt_pos_min, (int) ckpt_pos_max);
                            }
                        }
                        // --- end JBrain patch ---'''

if "JBrain patch: reuse-on-restore" in src:
    print("[llama-patch] already applied — skipping")
    sys.exit(0)

anchor = next((a for a in anchors if src.count(a) == 1), None)
ambiguous = [a for a in anchors if src.count(a) > 1]
if ambiguous:
    sys.stderr.write(
        f"[llama-patch] FATAL: anchor {ambiguous[0]!r} found more than once in {target}; "
        "refusing to patch ambiguously — re-verify against the base's commit\n"
    )
    sys.exit(1)
if anchor is None:
    sys.stderr.write(
        f"[llama-patch] FATAL: no known restore-path anchor found in {target} "
        f"(tried {anchors!r}); upstream reworded the slot-restore handler — "
        "re-derive the anchor against the base image's commit\n"
    )
    sys.exit(1)

src = src.replace(anchor, anchor + block, 1)
open(target, "w").write(src)
print(f"[llama-patch] applied reuse-on-restore checkpoint seed (anchor: {anchor!r})")
PY
