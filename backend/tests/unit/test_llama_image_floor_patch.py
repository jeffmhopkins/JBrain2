"""deploy/apply-llama-image-floor.sh, run for real against stand-in llama.cpp files: every
anchor is found once, every floor read goes through the override, a second run changes
nothing, and a moved anchor is a hard failure rather than an unpatched engine.

The stand-ins carry the exact anchor lines of Flash-Next's pinned commit (869034b); the
patched files were also syntax-checked with g++ against that tree when this was written.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "apply-llama-image-floor.sh"
_FILES = {
    "tools/mtmd/mtmd-image.cpp": (
        '#include "mtmd-image.h"\n\n#include <vector>\n'
        "void a() { f(hparams.image_min_pixels, hparams.image_max_pixels); }\n"
        "void b() { g(std::max(0, hparams.image_min_pixels)); }\n"
    ),
    "tools/mtmd/mtmd.h": (
        "MTMD_API const char * mtmd_bitmap_get_id(const mtmd_bitmap * bitmap);\n"
        "MTMD_API void         mtmd_bitmap_set_id(mtmd_bitmap * bitmap, const char * id);\n"
        "MTMD_API void         mtmd_bitmap_set_mergeable(mtmd_bitmap * bitmap, bool m);\n"
    ),
    "tools/mtmd/mtmd.cpp": "int last() {\n    return 0;\n}\n",
    "tools/server/server-common.cpp": (
        "    for (auto & file : files) {\n"
        "        bitmaps.entries.emplace_back(out.bitmap);\n"
        "    }\n"
    ),
    "tools/server/server-context.cpp": (
        "    try {\n        // process prompt\n        std::vector<server_tokens> inputs;\n    }\n"
    ),
}


def _tree(root: Path, **overrides: str) -> Path:
    for rel, text in {**_FILES, **overrides}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), str(root)], capture_output=True, text=True, check=False
    )


def test_every_file_is_patched_once(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    first = _run(root)
    assert first.returncode == 0, first.stderr
    image = (root / "tools/mtmd/mtmd-image.cpp").read_text()
    # The helper reads the model's own floor; nothing else does any more.
    assert image.count("hparams.image_min_pixels") == 0
    assert image.count("jb_min_pixels(hparams)") == 2
    assert "thread_local int32_t mtmd_jb_image_min_tokens = -1;" in image
    assert (
        "mtmd_jb_set_image_min_tokens(int32_t n_tokens);"
        in (root / "tools/mtmd/mtmd.h").read_text()
    )
    impl = (root / "tools/mtmd/mtmd.cpp").read_text()
    assert "extern thread_local int32_t mtmd_jb_image_min_tokens;" in impl
    common = (root / "tools/server/server-common.cpp").read_text()
    assert '"@min" + std::to_string(mtmd_jb_get_image_min_tokens())' in common
    context = (root / "tools/server/server-context.cpp").read_text()
    # The guard is set before the prompt is tokenized, inside the same scope.
    assert context.index('json_value(data, "image_min_tokens"') < context.index("// process prompt")

    patched = {rel: (root / rel).read_text() for rel in _FILES}
    again = _run(root)
    assert again.returncode == 0, again.stderr
    assert "already applied" in again.stdout
    assert {rel: (root / rel).read_text() for rel in _FILES} == patched


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("tools/server/server-context.cpp", "    // tokenize the prompt\n"),
        ("tools/server/server-common.cpp", "bitmaps.entries.push_back(out.bitmap);\n"),
        ("tools/mtmd/mtmd-image.cpp", '#include "mtmd-image.h"\nint x = image_min;\n'),
        (
            "tools/mtmd/mtmd.h",
            "MTMD_API void         mtmd_bitmap_set_id(mtmd_bitmap * bitmap, const char * id);\n"
            * 2,
        ),
    ],
)
def test_a_moved_or_doubled_anchor_fails_the_build(tmp_path: Path, rel: str, text: str) -> None:
    got = _run(_tree(tmp_path, **{rel: text}))
    assert got.returncode != 0
    assert "FATAL" in got.stderr


def test_the_flash_next_image_applies_it() -> None:
    dockerfile = (_SCRIPT.parent / "Dockerfile.flash-next").read_text()
    assert "COPY deploy/apply-llama-image-floor.sh /apply-llama-image-floor.sh" in dockerfile
    assert "    /apply-llama-image-floor.sh /llama; \\" in dockerfile
