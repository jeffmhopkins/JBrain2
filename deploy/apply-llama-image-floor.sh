#!/usr/bin/env bash
# Per-request picture detail for llama-server: a chat request may carry `image_min_tokens`,
# and its images are encoded to THAT floor instead of the served `--image-min-tokens`.
#
# Why (owner, 2026-11-12): the box serves Flash-Next with an image floor of 2048 tokens, so
# a note photo keeps its fine print. The GrowBot creature sends 320 px camera frames many
# times a minute; the floor blew each one up to ~2,100 tokens, three frames a turn, and a
# picture turn cost ~33 s of prefill. With this, the creature's lane asks for a small floor
# (api/growbot_llm.py) and every other caller keeps the served one.
#
# How: preprocessing runs on the HTTP thread that tokenizes the request, inside
# mtmd_tokenize, so the override is a THREAD-LOCAL set around that call (an RAII guard in
# handle_completions_impl) and read wherever mtmd-image.cpp reads `image_min_pixels`. No
# shared state changes, so concurrent requests cannot see each other's floor. A request
# without the field, or with a value <= 0, gets the served floor exactly as before. The
# floor is capped at the model's own `image_max_pixels`. An image's bitmap id (the prompt
# cache's key for it) gains `@min<N>`, so the same picture at two floors is never mistaken
# for one cached run of tokens.
#
# Applied by ANCHOR like apply-llama-patches.sh; a missing or doubled anchor is a hard
# failure, never a silently unpatched binary. Flash-Next's image only
# (deploy/Dockerfile.flash-next), against its pinned commit.
set -euo pipefail

SRC="${1:?usage: apply-llama-image-floor.sh <llama.cpp source dir>}"

python3 - "$SRC" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
MARK = "JBrain patch: per-request image floor"


def fail(msg: str) -> None:
    sys.stderr.write(f"[llama-image-floor] FATAL: {msg}\n")
    sys.exit(1)


def edit(rel: str, fn) -> None:
    path = root / rel
    if not path.is_file():
        fail(f"{path} not found (llama.cpp layout changed?)")
    src = path.read_text()
    if MARK in src:
        print(f"[llama-image-floor] {rel}: already applied — skipping")
        return
    path.write_text(fn(src))
    print(f"[llama-image-floor] patched {rel}")


def once(src: str, anchor: str, rel: str) -> None:
    n = src.count(anchor)
    if n != 1:
        fail(f"anchor {anchor!r} found {n} times in {rel}; re-derive it against the pinned commit")


# 1. mtmd-image.cpp: every read of the floor goes through the thread-local override.
def image(src: str) -> str:
    reads = src.count("hparams.image_min_pixels")
    if reads == 0:
        fail("no `hparams.image_min_pixels` read in mtmd-image.cpp; upstream moved the floor")
    anchor = '#include "mtmd-image.h"\n'
    once(src, anchor, "mtmd-image.cpp")
    helper = f"""
// {MARK}: set by mtmd_jb_set_image_min_tokens for the request on this thread.
thread_local int32_t mtmd_jb_image_min_tokens = -1;

static int jb_min_pixels(const clip_hparams & hp) {{
    if (mtmd_jb_image_min_tokens <= 0 || hp.image_min_pixels <= 0) {{
        return hp.image_min_pixels;
    }}
    const int64_t merge = hp.n_merge > 0 ? hp.n_merge : 1;
    const int64_t area  = (int64_t) hp.patch_size * hp.patch_size * merge * merge;
    int64_t px = (int64_t) mtmd_jb_image_min_tokens * area;
    if (hp.image_max_pixels > 0 && px > hp.image_max_pixels) {{
        px = hp.image_max_pixels;
    }}
    return (int) px;
}}
"""
    src = src.replace("hparams.image_min_pixels", "jb_min_pixels(hparams)")
    print(f"[llama-image-floor] mtmd-image.cpp: {reads} floor read(s) routed through the override")
    return src.replace(anchor, anchor + helper, 1)


# 2. mtmd.h / mtmd.cpp: the setter and getter, exported with the rest of the API.
def header(src: str) -> str:
    anchor = "MTMD_API void         mtmd_bitmap_set_id(mtmd_bitmap * bitmap, const char * id);\n"
    once(src, anchor, "mtmd.h")
    decl = f"""
// {MARK}: the image floor, in tokens, for images tokenized on THIS thread
// (<= 0 restores the model's own). Read by preprocessing inside mtmd_tokenize.
MTMD_API void    mtmd_jb_set_image_min_tokens(int32_t n_tokens);
MTMD_API int32_t mtmd_jb_get_image_min_tokens(void);
"""
    return src.replace(anchor, anchor + decl, 1)


def impl(src: str) -> str:
    return src.rstrip("\n") + f"""

// {MARK}
extern thread_local int32_t mtmd_jb_image_min_tokens;

void mtmd_jb_set_image_min_tokens(int32_t n_tokens) {{
    mtmd_jb_image_min_tokens = n_tokens;
}}

int32_t mtmd_jb_get_image_min_tokens(void) {{
    return mtmd_jb_image_min_tokens;
}}
"""


# 3. server-common.cpp: a bitmap tokenized at another floor is another cache entry.
def common(src: str) -> str:
    anchor = "        bitmaps.entries.emplace_back(out.bitmap);\n"
    once(src, anchor, "server-common.cpp")
    block = f"""        if (mtmd_jb_get_image_min_tokens() > 0) {{ // {MARK}
            const char * jb_id = mtmd_bitmap_get_id(out.bitmap);
            const std::string jb_keyed = std::string(jb_id ? jb_id : "")
                + "@min" + std::to_string(mtmd_jb_get_image_min_tokens());
            mtmd_bitmap_set_id(out.bitmap, jb_keyed.c_str());
        }}
"""
    return src.replace(anchor, anchor + block, 1)


# 4. server-context.cpp: the request's `image_min_tokens` holds for its own tokenization.
def context(src: str) -> str:
    anchor = "        // process prompt\n        std::vector<server_tokens> inputs;\n"
    once(src, anchor, "server-context.cpp")
    block = f"""        // {MARK}: the request's own floor for its images, unset after.
        struct jb_image_floor {{
            explicit jb_image_floor(int32_t n) {{ mtmd_jb_set_image_min_tokens(n); }}
            ~jb_image_floor() {{ mtmd_jb_set_image_min_tokens(-1); }}
        }};
        const jb_image_floor jb_floor(json_value(data, "image_min_tokens", (int32_t) -1));

"""
    return src.replace(anchor, block + anchor, 1)


edit("tools/mtmd/mtmd-image.cpp", image)
edit("tools/mtmd/mtmd.h", header)
edit("tools/mtmd/mtmd.cpp", impl)
edit("tools/server/server-common.cpp", common)
edit("tools/server/server-context.cpp", context)
print("[llama-image-floor] done")
PY
