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

anchor = "slot->prompt.tokens.insert(tokens);"
block = '''
                        // --- JBrain patch: reuse-on-restore for SWA/hybrid/recurrent ---
                        // A slot-restore rebuilds the memory state and sets
                        // slot->prompt.tokens, but for a SWA/hybrid/recurrent model the
                        // completion-path prompt reuse is gated on a CONTEXT CHECKPOINT.
                        // With none registered the reuse hits do_reset and re-prefills the
                        // whole prompt. Seed one checkpoint spanning the restored prompt so
                        // the restore is actually reusable (harmless for pure-attention).
                        if (params_base.n_ctx_checkpoints > 0 && slot->prompt.tokens.size() > 0) {
                            auto & cur = slot->prompt.checkpoints.emplace_back();
                            cur.id_task = -1;  // not owned by a completion task
                            cur.update_pos(0, 0, (llama_pos) slot->prompt.tokens.size() - 1);
                            cur.update_tgt(ctx_tgt, slot->id, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
                            cur.update_dft(ctx_dft, slot->id, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
                            if (spec) {
                                common_speculative_get_state(spec.get(), slot->id, cur.data_spec);
                            }
                            SLT_TRC(slot, "seeded restore checkpoint (n_tokens = %d)\\n",
                                (int) slot->prompt.tokens.size());
                        }
                        // --- end JBrain patch ---'''

if "JBrain patch: reuse-on-restore" in src:
    print("[llama-patch] already applied — skipping")
    sys.exit(0)

count = src.count(anchor)
if count != 1:
    sys.stderr.write(
        f"[llama-patch] FATAL: anchor found {count} times (expected 1) in {target}; "
        "refusing to patch ambiguously — re-verify against the pinned commit\\n"
    )
    sys.exit(1)

src = src.replace(anchor, anchor + block, 1)
open(target, "w").write(src)
print("[llama-patch] applied reuse-on-restore checkpoint seed")
PY
