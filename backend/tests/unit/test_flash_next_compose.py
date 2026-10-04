"""The Flash-Next engine's deploy guarantees, asserted on the compose file.

The second on-box engine (docs/plans/FLASH_NEXT_ENGINE_PLAN.md) is the ALTERNATIVE to the
standard `local-llm` gateway, never its companion: both up is ~170 GiB on a 128 GB box. What
keeps them apart on the compose side is that Flash-Next never starts on its own, and what
keeps it interchangeable is that it mirrors `local-llm` in everything but image and config."""

from pathlib import Path

import yaml

from jbrain.llm import engine

_REPO = Path(__file__).resolve().parents[3]
_COMPOSE = _REPO / "deploy" / "docker-compose.yml"


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())["services"]


def test_each_engine_is_the_service_the_engine_module_names() -> None:
    services = _services()
    for name in engine.ENGINES:
        assert engine.SERVICE[name] in services, name


def test_flash_next_never_starts_with_the_stack() -> None:
    """Profile-gated: a plain `up -d` (the update's recreate, a host restart) must never bring
    it up beside `local-llm`. Only the engine-aware start paths do."""
    assert _services()["flash-next"]["profiles"] == ["flash-next"]


def test_flash_next_serves_its_own_llama_swap_config() -> None:
    command = _services()["flash-next"]["command"]
    config = command[command.index("--config") + 1]
    assert config == "/models/" + engine.CONFIG_FILE[engine.FLASH_NEXT]
    assert "--watch-config" in command
    std = _services()["local-llm"]["command"]
    assert std[std.index("--config") + 1] == "/models/" + engine.CONFIG_FILE[engine.STANDARD]


def test_flash_next_has_its_own_image() -> None:
    svc = _services()["flash-next"]
    assert svc["build"]["dockerfile"] == "deploy/Dockerfile.flash-next"
    assert svc["image"] != _services()["local-llm"]["image"]


def test_flash_next_mirrors_the_standard_gateway_hardware_access() -> None:
    """The same iGPU, through the same device and groups: an engine that cannot open
    /dev/dri fails every load, and the setup script writes these GIDs for both."""
    services = _services()
    for key in ("devices", "group_add", "security_opt", "volumes", "restart"):
        assert services["flash-next"][key] == services["local-llm"][key], key


def test_the_flash_next_patch_build_arg_is_on_unless_set() -> None:
    # F4: the disk prefix cache needs the checkpoint sidecar on this hybrid; the api proves the
    # patch per save, so an operator override to 0 degrades to no disk layer, not garbage.
    args = _services()["flash-next"]["build"]["args"]
    assert args["PATCH_RESTORE_CHECKPOINT"] == "${FLASH_NEXT_PATCH_RESTORE_CHECKPOINT:-1}"


def test_the_flash_next_dockerfile_builds_the_patch_by_default() -> None:
    text = (_REPO / "deploy" / "Dockerfile.flash-next").read_text()
    assert "ARG PATCH_RESTORE_CHECKPOINT=1" in text
    assert "/apply-llama-patches.sh /llama" in text
