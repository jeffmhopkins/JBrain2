"""The llama-swap build is pinned to an immutable commit, not a moving tag.

llama-swap tags releases `vNNN`, which Go module resolution rejects (it wants semver with a
matching major-version import path), so the Dockerfile has always pinned a commit. That
started as a workaround and is now the guarantee: a floating ref would let the gateway change
under the box on any rebuild, and the gateway is the single process serving every model.

Worth a test because the failure is silent and remote. The owner updates from the PWA with no
terminal (CLAUDE.md #10); a tag that moved would arrive as "models behave differently since
the update" with nothing in the diff to point at.
"""

import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_DOCKERFILE = _DEPLOY / "Dockerfile.local-llm"
_FLASH_NEXT = _DEPLOY / "Dockerfile.flash-next"


def _pin(dockerfile: Path = _DOCKERFILE, arg: str = "LLAMA_SWAP_VERSION") -> str:
    m = re.search(rf"^ARG {arg}=(\S+)", dockerfile.read_text(), re.M)
    assert m, f"{dockerfile.name} no longer declares ARG {arg}"
    return m.group(1)


def test_the_gateway_is_pinned_to_a_full_commit_sha() -> None:
    pin = _pin()
    assert re.fullmatch(r"[0-9a-f]{40}", pin), (
        f"llama-swap pin {pin!r} is not a 40-char commit SHA. A tag or short SHA here lets the"
        " gateway move under the box on a rebuild — see this file's docstring."
    )


def test_the_pin_is_explained_in_the_file_that_carries_it() -> None:
    """A bare SHA is unreviewable: nobody can tell v228 from v250 by looking, so the reason for
    the CURRENT pin has to sit beside it. This caught a real gap — the comment claimed v228 long
    after the question "are we missing upstream fixes?" became worth asking, and the answer
    (#875, a browser tab stalling llama.cpp) had been sitting unread upstream for weeks."""
    text = _DOCKERFILE.read_text()
    assert "v250" in text, "the pin's comment does not name the release the SHA corresponds to"


def test_both_engines_run_the_same_gateway() -> None:
    """Flash-Next keeps llama-swap so the api's admin client (`/running`, unload, upstream
    health) has ONE contract to honour (FLASH_NEXT_ENGINE_PLAN §4). Two pins would let the
    engines' gateways drift apart one bump at a time."""
    assert _pin(_FLASH_NEXT) == _pin()


def test_flash_next_llama_cpp_is_a_full_commit_with_its_reason_beside_it() -> None:
    """Flash-Next builds llama.cpp from source rather than taking the base image's, so the
    commit IS the engine. A branch or short SHA would let it move on any rebuild."""
    pin = _pin(_FLASH_NEXT, "LLAMA_CPP_COMMIT")
    assert re.fullmatch(r"[0-9a-f]{40}", pin), f"llama.cpp pin {pin!r} is not a full SHA"
    text = _FLASH_NEXT.read_text()
    # Every stage that declares it agrees (the final stage re-checks the binary against it).
    assert set(re.findall(r"^ARG LLAMA_CPP_COMMIT=(\S+)", text, re.M)) == {pin}
    # The fixes the plan requires are named where the pin is, so a bump is reviewable.
    for pr in ("#27742", "#27941", "#29028"):
        assert pr in text, f"the pin's comment no longer accounts for {pr}"


def test_flash_next_checkpoint_patch_is_on_by_default() -> None:
    """F4 re-validated the sidecar patch's anchors against this pin; a pin bump re-validates
    them (they fail the build hard on drift), and the api proves the patch per save."""
    assert _pin(_FLASH_NEXT, "PATCH_RESTORE_CHECKPOINT") == "1"


def test_flash_next_bakes_a_checksummed_wikitext_sample() -> None:
    text = _FLASH_NEXT.read_text()
    assert re.search(r"^ARG WIKITEXT_ZIP_SHA256=[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^ARG WIKITEXT_TEST_SHA256=[0-9a-f]{64}$", text, re.M)
    assert "sha256sum -c" in text, "the sample must be verified, not just downloaded"
    # Revision-pinned, not `resolve/main`, so the bytes cannot move under the checksum.
    assert "/resolve/main/" not in text
    assert "/opt/jbrain/eval/wiki.test.raw" in text


def test_flash_next_puts_llama_perplexity_on_path() -> None:
    """The perplexity one-shot runs /opt/llama.cpp/bin/llama-perplexity; the same build is
    installed over whatever `llama-perplexity` PATH resolves to (as llama-server is), and
    the image fails to build unless the binary on PATH reports the pinned commit."""
    text = _FLASH_NEXT.read_text()
    assert "--target llama-server llama-perplexity" in text
    assert "for b in llama-server llama-perplexity" in text
    assert 'command -v "$b"' in text
    assert "llama-perplexity --version" in text
    assert "/opt/llama.cpp/bin/" in text


def test_flash_next_runtime_has_an_ffmpeg_that_decodes_mjpeg() -> None:
    """llama-server decodes `input_video` with ffmpeg from PATH, and the api sends Motion-JPEG
    because Fedora's ffmpeg-free cannot be trusted with H.264 — so the RUNTIME stage installs
    it when missing and fails the build without an mjpeg decoder or a working ffprobe."""
    text = _FLASH_NEXT.read_text()
    runtime = text[text.rindex("\nFROM ") :]
    assert "install ffmpeg-free" in runtime
    assert "ffmpeg -hide_banner -decoders" in runtime and "mjpeg" in runtime
    assert "ffprobe -version" in runtime
