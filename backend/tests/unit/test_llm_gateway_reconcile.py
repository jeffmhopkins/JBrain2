"""Boot-time gateway-config reconcile (jbrain.api.llm_settings.reconcile_gateway_config): re-stamp
llama-swap.yaml with the operator's SAVED context-window/slot overrides so a deploy that regenerated
the config from the BASE catalog (dropping the overrides) self-heals on the next boot — the fix for
Nemotron reloading at its 32k base and overflowing every agent turn."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jbrain.api.llm_settings import (
    reconcile_gateway_config,
    reconcile_gateway_windows_on_boot,
)
from jbrain.db.session import SessionContext
from jbrain.llm import llama_swap_config
from jbrain.llm.local_gateway import LocalGatewayError

CTX = SessionContext(principal_kind="owner")


def _manifest() -> list[dict[str, object]]:
    return [
        {
            "id": "gpt-oss-120b",
            "served_model": "gpt-oss-120b",
            "gguf_include": "*mxfp4*.gguf",
            "mmproj_include": None,
            "context_window": 131072,
            "recommended": True,
        },
        {
            "id": "qwen3-vl-30b",
            "served_model": "qwen3-vl-30b-a3b",
            "gguf_include": "*Q8_0*.gguf",
            "mmproj_include": "mmproj*.gguf",
            "context_window": 32768,
            "recommended": True,
        },
    ]


def _lay_down(root: Path) -> None:
    (root / "gpt-oss-120b").mkdir()
    (root / "gpt-oss-120b" / "model-mxfp4.gguf").write_bytes(b"\0")
    (root / "qwen3-vl-30b").mkdir()
    (root / "qwen3-vl-30b" / "model-Q8_0.gguf").write_bytes(b"\0")
    (root / "qwen3-vl-30b" / "mmproj-f16.gguf").write_bytes(b"\0")


class _FakeGateway:
    def state_of(self, served_model: str) -> str:
        """No state known — this fake serves a path that never reads it. `""` is the
        honest answer and is never treated as ready."""
        return ""

    """Records unloads and reports a resident set; can simulate a down gateway on running()."""

    def __init__(self, running: set[str], *, running_raises: bool = False) -> None:
        self._running = set(running)
        self._running_raises = running_raises
        self.unloaded: list[str] = []

    async def running(self) -> set[str]:
        if self._running_raises:
            raise LocalGatewayError("gateway down")
        return set(self._running)

    async def unload(self, served_model: str) -> None:
        self.unloaded.append(served_model)
        self._running.discard(served_model)

    async def load(self, served_model: str) -> None:  # satisfies the LocalGateway protocol
        self._running.add(served_model)


async def test_reconcile_rewrites_a_base_stamped_config_and_evicts_the_resident_model(
    tmp_path: Path,
) -> None:
    _lay_down(tmp_path)
    manifest = _manifest()
    # Simulate the DEPLOY re-stamp: base config on disk, no override applied.
    llama_swap_config.write(str(tmp_path), manifest)
    assert "-c 131072" in (tmp_path / "llama-swap.yaml").read_text()  # gpt-oss base window
    gw = _FakeGateway({"gpt-oss-120b"})  # loaded at the (wrong) base -c
    changed = await reconcile_gateway_config(
        str(tmp_path), manifest, windows={"gpt-oss-120b": 65536}, slots={}, gateway=gw
    )
    assert changed is True
    # The saved override is now stamped, and the resident model was evicted so it reloads at it.
    assert "-c 65536" in (tmp_path / "llama-swap.yaml").read_text()
    assert gw.unloaded == ["gpt-oss-120b"]


async def test_reconcile_preserves_an_operator_flag_and_image_floor(tmp_path: Path) -> None:
    """The boot reconcile must not re-stamp AWAY the override kinds it wasn't told about.

    It rendered `desired` from windows+slots only, so a config carrying an operator launch flag
    or a raised image floor could never match it: every boot re-stamped both away and then
    evicted every resident model to "correct" the config it had just made wrong. A launch-flag
    experiment therefore silently reverted on the next restart — worse than not having the knob,
    because the flag reads as ineffective rather than absent and any measurement after it lies."""
    _lay_down(tmp_path)
    manifest = _manifest()
    windows = {"gpt-oss-120b": 65536}
    extra = {"gpt-oss-120b": ["--ctx-checkpoints", "8"]}  # 16 is now refused by the bound
    floors = {"qwen3-vl-30b": 4096}
    llama_swap_config.write(
        str(tmp_path), manifest, windows=windows, extra_args=extra, image_min_tokens=floors
    )
    gw = _FakeGateway({"gpt-oss-120b"})
    changed = await reconcile_gateway_config(
        str(tmp_path),
        manifest,
        windows=windows,
        slots={},
        gateway=gw,
        extra_args=extra,
        image_min_tokens=floors,
    )
    text = (tmp_path / "llama-swap.yaml").read_text()
    assert "--ctx-checkpoints 8" in text
    assert "--image-min-tokens 4096" in text
    # And because it now matches, this is the no-op path: no needless eviction of a warm model.
    assert changed is False
    assert gw.unloaded == []


async def test_reconcile_restores_a_flag_a_deploy_stamped_away(tmp_path: Path) -> None:
    """The other direction, and the reason the reconcile exists at all: a deploy re-stamps from
    the base catalog, so a saved flag is missing from disk and must be put back."""
    _lay_down(tmp_path)
    manifest = _manifest()
    llama_swap_config.write(str(tmp_path), manifest)  # deploy: base config, no overrides
    assert "--ctx-checkpoints 8" not in (tmp_path / "llama-swap.yaml").read_text()
    gw = _FakeGateway(set())
    changed = await reconcile_gateway_config(
        str(tmp_path),
        manifest,
        windows={},
        slots={},
        gateway=gw,
        extra_args={"gpt-oss-120b": ["--ctx-checkpoints", "8"]},
        image_min_tokens={"qwen3-vl-30b": 4096},
    )
    assert changed is True
    text = (tmp_path / "llama-swap.yaml").read_text()
    assert "--ctx-checkpoints 8" in text and "--image-min-tokens 4096" in text


async def test_reconcile_is_a_noop_when_the_config_already_matches(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    manifest = _manifest()
    windows = {"gpt-oss-120b": 65536}
    llama_swap_config.write(str(tmp_path), manifest, windows=windows)  # already correct
    gw = _FakeGateway({"gpt-oss-120b"})
    changed = await reconcile_gateway_config(
        str(tmp_path), manifest, windows=windows, slots={}, gateway=gw
    )
    # No rewrite, and crucially NO eviction — a plain restart keeps its warm model resident.
    assert changed is False
    assert gw.unloaded == []


async def test_reconcile_writes_when_no_config_exists_yet(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    gw = _FakeGateway(set())  # fresh post-deploy boot: gateway is empty
    changed = await reconcile_gateway_config(
        str(tmp_path), _manifest(), windows={"gpt-oss-120b": 65536}, slots={}, gateway=gw
    )
    assert changed is True
    assert "-c 65536" in (tmp_path / "llama-swap.yaml").read_text()
    assert gw.unloaded == []  # nothing resident to evict on a fresh boot


async def test_reconcile_survives_a_down_gateway_still_rewriting_the_config(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    manifest = _manifest()
    llama_swap_config.write(str(tmp_path), manifest)  # base
    gw = _FakeGateway({"gpt-oss-120b"}, running_raises=True)
    changed = await reconcile_gateway_config(
        str(tmp_path), manifest, windows={"gpt-oss-120b": 65536}, slots={}, gateway=gw
    )
    # The config is corrected regardless; a down gateway just means the stale process lingers
    # until it's next swapped (no eviction was possible).
    assert changed is True
    assert "-c 65536" in (tmp_path / "llama-swap.yaml").read_text()
    assert gw.unloaded == []


async def test_reconcile_is_best_effort_when_weights_are_missing(tmp_path: Path) -> None:
    # No weights on disk → resolve_weight raises inside render → reconcile returns False, never
    # raising into boot (a partial/absent weights dir must not wedge startup).
    gw = _FakeGateway(set())
    changed = await reconcile_gateway_config(
        str(tmp_path), _manifest(), windows={}, slots={}, gateway=gw
    )
    assert changed is False
    assert not (tmp_path / "llama-swap.yaml").exists()


async def test_on_boot_reconcile_is_inert_when_local_hosting_is_off(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        local_llm_enabled=False, local_models=[], local_models_dir=str(tmp_path)
    )
    # store is never touched when hosting is off — pass a bare object to prove it.
    changed = await reconcile_gateway_windows_on_boot(
        settings,  # type: ignore[arg-type]  # SimpleNamespace stands in for Settings
        object(),  # type: ignore[arg-type]  # store is untouched on the hosting-off path
        _FakeGateway(set()),
        CTX,
    )
    assert changed is False


async def test_on_boot_reconcile_carries_every_override_kind_through(tmp_path: Path) -> None:
    """The boot wiring itself, end to end — not just `reconcile_gateway_config` called directly.

    This is the path that made a launch-flag experiment silently revert on the next restart: it
    loaded windows and slots only, so `desired` could never match a config carrying an operator
    flag or a raised image floor, and every boot re-stamped both away. The mechanism was covered
    by calling `reconcile_gateway_config` with the new kwargs; the wiring that FEEDS it was not,
    so a regression in this function would have gone unnoticed."""
    _lay_down(tmp_path)

    class _Store:
        async def llm_local_context_windows(self, _ctx: object) -> dict[str, int]:
            return {"gpt-oss-120b": 65536}

        async def llm_local_parallel_slots(self, _ctx: object) -> dict[str, int]:
            return {}

        async def llm_local_extra_args(self, _ctx: object) -> dict[str, list[str]]:
            return {"gpt-oss-120b": ["--ctx-checkpoints", "8"]}

        async def llm_local_image_min_tokens(self, _ctx: object) -> dict[str, int]:
            return {"qwen3-vl-30b": 4096}

    settings = SimpleNamespace(
        local_llm_enabled=True,
        local_models=["qwen3-vl-30b", "gpt-oss-120b"],
        local_models_dir=str(tmp_path),
    )
    changed = await reconcile_gateway_windows_on_boot(
        settings,  # type: ignore[arg-type]
        _Store(),  # type: ignore[arg-type]
        _FakeGateway(set()),
        CTX,
    )
    assert changed is True
    text = (tmp_path / "llama-swap.yaml").read_text()
    # All four kinds reached the served config, not just the two this used to load.
    assert "-c 65536" in text
    assert "--ctx-checkpoints 8" in text
    assert "--image-min-tokens 4096" in text
