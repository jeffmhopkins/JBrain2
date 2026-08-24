#!/usr/bin/env bash
# Apply JBrain's local llama.cpp patches by ANCHOR (not line number), so they
# survive minor upstream drift around the touched code. Run inside the builder
# stage of Dockerfile.local-llm against a checked-out llama.cpp tree.
#
# The patch (deploy/patches/0001) makes slot save/restore preserve CONTEXT
# CHECKPOINTS via a sidecar file, which is what lets a SWA/hybrid/recurrent
# model (qwen3.8) REUSE a disk-restored prompt instead of re-prefilling it.
# Two insertions, one in each slot handler; each is idempotent by its own
# marker, and an anchor that cannot be found is a HARD failure (exit 1) — a
# silently-unapplied patch would ship the unpatched binary while claiming the
# fix. Validated end-to-end against a live hybrid model (LFM2-350M) at commit
# 758443071: a 2820-token restored prompt re-processed 4 tokens, not 2820.
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

# --- insertion 1: SLOT_SAVE writes the checkpoint sidecar --------------------------
# The anchor is inside the save handler's result-building (unique: the restore
# handler's twin line says `= false`). `slot`, `filepath` and the checkpoint list
# are all in scope there across the known base versions.
save = {
    "name": "save-side checkpoint sidecar",
    "marker": "JBrain patch: persist context checkpoints",
    "anchors": ["res->is_save  = true;"],
    "block": '''
                    // --- JBrain patch: persist context checkpoints beside the slot file ---
                    // (the restore-side block explains why; sidecar = <filepath>.ckpt)
                    {
                        const auto & cps = slot->prompt.checkpoints;
                        std::ofstream ck(filepath + ".ckpt", std::ios::binary | std::ios::trunc);
                        if (ck) {
                            const char ck_magic[4] = {'J','B','C','K'};
                            uint32_t ck_ver = 1;
                            uint32_t ck_count = (uint32_t) cps.size();
                            ck.write(ck_magic, 4);
                            ck.write((const char *) &ck_ver, sizeof(ck_ver));
                            ck.write((const char *) &ck_count, sizeof(ck_count));
                            for (const auto & cp : cps) {
                                const int64_t nt = cp.n_tokens;
                                const int32_t pmin = (int32_t) cp.pos_min;
                                const int32_t pmax = (int32_t) cp.pos_max;
                                ck.write((const char *) &nt, sizeof(nt));
                                ck.write((const char *) &pmin, sizeof(pmin));
                                ck.write((const char *) &pmax, sizeof(pmax));
                                for (const std::vector<uint8_t> * d : { &cp.data_tgt, &cp.data_dft, &cp.data_spec }) {
                                    const uint64_t len = (uint64_t) d->size();
                                    ck.write((const char *) &len, sizeof(len));
                                    if (len > 0) {
                                        ck.write((const char *) d->data(), (std::streamsize) len);
                                    }
                                }
                            }
                            if (!ck) {
                                SLT_WRN(*slot, "%s", "failed writing checkpoint sidecar (restore will re-prefill)\\n");
                            } else {
                                SLT_INF(*slot, "saved %u context checkpoint(s) to sidecar\\n", ck_count);
                            }
                        }
                    }
                    // --- end JBrain patch ---''',
}

# --- insertion 2: SLOT_RESTORE reloads the sidecar ---------------------------------
# The restore success path has been worded differently across llama.cpp versions;
# every known wording is an alternative anchor (first found wins — they never
# coexist in one version).
restore = {
    "name": "restore-side checkpoint sidecar",
    "marker": "JBrain patch: reload context checkpoints",
    "anchors": [
        "slot->prompt.tokens = std::move(restored);",  # b10603+ deserialize rework
        "slot->prompt.tokens.insert(tokens);",         # token-vector path (b10068 era)
    ],
    "block": '''
                        // --- JBrain patch: reload context checkpoints saved beside the slot file ---
                        // A slot restore rebuilds the memory state, but for SWA/hybrid/recurrent
                        // models prompt REUSE is gated on a context checkpoint holding state at an
                        // EARLIER position: the reuse path must re-decode at least the final token
                        // (TAG_PROMPT_LOGITS), which needs state-after-k for k < n, while the
                        // restored state is after-n — so no checkpoint seeded AT restore time can
                        // ever qualify (its pos_min == pos_max == n-1 fails the strict
                        // pos_min < pos_min_thold reuse bound; measured live 2026-08-24). The
                        // save-side twin persists the checkpoints that existed at save time;
                        // reloading them gives the restored slot exactly the reuse a live session
                        // had. Best-effort: a missing or invalid sidecar just means the old
                        // behaviour (full re-prefill).
                        {
                            std::ifstream ck(filepath + ".ckpt", std::ios::binary);
                            if (ck) {
                                char ck_magic[4] = {0};
                                uint32_t ck_ver = 0;
                                uint32_t ck_count = 0;
                                bool ck_ok = (bool) ck.read(ck_magic, 4)
                                    && ck_magic[0] == 'J' && ck_magic[1] == 'B'
                                    && ck_magic[2] == 'C' && ck_magic[3] == 'K'
                                    && (bool) ck.read((char *) &ck_ver, sizeof(ck_ver)) && ck_ver == 1
                                    && (bool) ck.read((char *) &ck_count, sizeof(ck_count)) && ck_count <= 64;
                                std::list<common_prompt_checkpoint> ck_loaded;
                                for (uint32_t ci = 0; ck_ok && ci < ck_count; ci++) {
                                    common_prompt_checkpoint cp;
                                    int64_t nt = 0;
                                    int32_t pmin = -1;
                                    int32_t pmax = -1;
                                    ck_ok = (bool) ck.read((char *) &nt, sizeof(nt))
                                        && (bool) ck.read((char *) &pmin, sizeof(pmin))
                                        && (bool) ck.read((char *) &pmax, sizeof(pmax))
                                        && nt > 0 && (size_t) nt <= slot->prompt.tokens.size()
                                        && pmin >= 0 && pmax >= pmin;
                                    for (std::vector<uint8_t> * d : { &cp.data_tgt, &cp.data_dft, &cp.data_spec }) {
                                        uint64_t len = 0;
                                        ck_ok = ck_ok && (bool) ck.read((char *) &len, sizeof(len))
                                            && len <= (1ull << 32);
                                        if (ck_ok && len > 0) {
                                            d->resize((size_t) len);
                                            ck_ok = (bool) ck.read((char *) d->data(), (std::streamsize) len);
                                        }
                                    }
                                    if (ck_ok) {
                                        cp.n_tokens = nt;
                                        cp.id_task  = -1;
                                        cp.pos_min  = (llama_pos) pmin;
                                        cp.pos_max  = (llama_pos) pmax;
                                        ck_loaded.push_back(std::move(cp));
                                    }
                                }
                                if (ck_ok && !ck_loaded.empty()) {
                                    slot->prompt.checkpoints.clear();
                                    for (auto & cp : ck_loaded) {
                                        slot->prompt.checkpoints.push_back(std::move(cp));
                                    }
                                    SLT_INF(*slot, "restored %zu context checkpoint(s) from sidecar\\n",
                                        slot->prompt.checkpoints.size());
                                } else if (!ck_ok) {
                                    SLT_WRN(*slot, "%s", "invalid checkpoint sidecar — ignoring (full re-prefill on reuse)\\n");
                                }
                            }
                        }
                        // --- end JBrain patch ---''',
}

for ins in (save, restore):
    if ins["marker"] in src:
        print(f"[llama-patch] {ins['name']}: already applied — skipping")
        continue
    ambiguous = [a for a in ins["anchors"] if src.count(a) > 1]
    if ambiguous:
        sys.stderr.write(
            f"[llama-patch] FATAL: anchor {ambiguous[0]!r} found more than once in {target}; "
            "refusing to patch ambiguously — re-verify against the base's commit\n"
        )
        sys.exit(1)
    anchor = next((a for a in ins["anchors"] if src.count(a) == 1), None)
    if anchor is None:
        sys.stderr.write(
            f"[llama-patch] FATAL: no anchor for {ins['name']} found in {target} "
            f"(tried {ins['anchors']!r}); upstream reworded the slot handlers — "
            "re-derive the anchors against the base image's commit\n"
        )
        sys.exit(1)
    src = src.replace(anchor, anchor + ins["block"], 1)
    print(f"[llama-patch] applied {ins['name']} (anchor: {anchor!r})")

# <fstream> and <list> ride in via the server's existing includes on the known
# bases; verify rather than assume so a slimmed-down include set fails loudly here
# instead of as a compile error mid-build.
for header in ("#include <fstream>", "#include <list>"):
    if header in src:
        continue
    if header == "#include <list>":
        # std::list is used; the type also reaches this TU via server-common.h's
        # checkpoint list member, but pull it in explicitly to be safe.
        src = src.replace("#include <fstream>", "#include <fstream>\n#include <list>", 1)
        print("[llama-patch] added #include <list>")
    else:
        sys.stderr.write(f"[llama-patch] FATAL: {header} missing from {target} — patch needs it\n")
        sys.exit(1)

open(target, "w").write(src)
print("[llama-patch] done")
PY
