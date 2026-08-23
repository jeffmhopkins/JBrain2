"""The KV-slot disk layer's deploy guarantees, asserted on the compose file.

`jbrain.llm.kv_prefix` has llama-server save the primed jerv prefix under
`/models/.kvslots/<model>/` and the api prune stale files through its own mount of the
same volume. Both sides were unit-tested to death and the first LIVE save still failed —
`Read-only file system` — because the local-llm service mounts the weights read-only (as
it should: the inference process must never be able to touch a weight file) and nothing
asserted the one writable carve-out the feature depends on. These are that assertion."""

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _volumes(service: str) -> list[str]:
    spec = yaml.safe_load(_COMPOSE.read_text())
    return [str(v) for v in spec["services"][service].get("volumes", ())]


def test_llama_server_gets_exactly_one_writable_subtree_of_the_weights() -> None:
    volumes = _volumes("local-llm")
    assert "./local-models:/models:ro" in volumes, (
        "the weights mount must stay READ-ONLY — the carve-out below is the only write "
        "path the inference process may have"
    )
    kvslot = [v for v in volumes if "/models/.kvslots" in v]
    assert kvslot == ["./local-models/.kvslots:/models/.kvslots"], (
        "kv_prefix needs llama-server to write slot files under /models/.kvslots — "
        "without this nested rw bind every save fails 'Read-only file system' and the "
        "disk layer is silently inert (observed live, 2026-08-23)"
    )


def test_the_api_can_prune_what_the_server_saves() -> None:
    # The store's pruning and poison-file deletion run in the api against its own mount of
    # the same volume; an :ro there would turn every delete into a silent suppressed no-op.
    volumes = _volumes("api")
    assert "./local-models:/data/local-models" in volumes, (
        "the api must mount local-models READ-WRITE — it installs weights, writes "
        "llama-swap.yaml, and prunes .kvslots files"
    )
