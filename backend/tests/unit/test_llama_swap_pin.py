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

_DOCKERFILE = Path(__file__).resolve().parents[3] / "deploy" / "Dockerfile.local-llm"


def _pin() -> str:
    m = re.search(r"^ARG LLAMA_SWAP_VERSION=(\S+)", _DOCKERFILE.read_text(), re.M)
    assert m, "Dockerfile.local-llm no longer declares ARG LLAMA_SWAP_VERSION"
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
