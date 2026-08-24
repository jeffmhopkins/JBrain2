"""Operator CLI, run inside the api container (`python -m jbrain.cli <cmd>`).

The owner key is printed exactly once and stored only as a hash; there is no
way to display it again — `reset-owner-key` is the recovery path.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import get_settings
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import (
    LLM_TASK_OVERRIDES_KEY,
    LOCAL_LLM_PATCH_RESTORE_CHECKPOINT_KEY,
    SqlSettingsStore,
)


def _print_key_block(key: str) -> None:
    print()
    print("=" * 64)
    print("  YOUR OWNER KEY — copy it to paper now; it cannot be shown again")
    print()
    print(f"    {key}")
    print()
    print("  Lost keys are reset with: jbrain reset-owner-key")
    print("=" * 64)
    print()


async def _rotate() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        repo = SqlAuthRepo(async_sessionmaker(engine, expire_on_commit=False))
        key = await service.rotate_owner_key(repo)
        _print_key_block(key)
    finally:
        await engine.dispose()


async def _local_llm_unload() -> int:
    """Unload every model the gateway currently holds, then report what is left.

    Called before an update stops the gateway. Stopping the container alone relies on the
    kernel reclaiming tens of gigabytes of unified memory as the process dies, at exactly
    the moment the update is about to allocate for a build and a stack recreate — and on
    this box that race once spiked into a reclaim livelock that hard-locked the host,
    keyboard included. Asking the gateway to release its models first makes the memory go
    away in a controlled way, before anything else needs it.

    Best-effort by contract: a gateway that is already down, unreachable, or holding
    nothing is a success, not a failure. It must never abort an update."""
    from jbrain.llm import gpu_guard
    from jbrain.llm.local_gateway import LocalGatewayClient

    settings = get_settings()
    if not settings.local_llm_enabled:
        print("[unload] local hosting off — nothing to unload")
        return 0
    gateway = LocalGatewayClient(settings.local_llm_url, gpu_probe=gpu_guard.probe_for(settings))
    try:
        loaded = await gateway.running()
    except Exception as exc:  # noqa: BLE001
        print(f"[unload] gateway unreachable ({exc}) — nothing to unload")
        return 0
    if not loaded:
        print("[unload] gateway holds no models")
        return 0
    for served in sorted(loaded):
        try:
            await gateway.unload(served)
            print(f"[unload] released {served}")
        except Exception as exc:  # noqa: BLE001
            # Report and keep going: releasing three of four models is still most of the
            # memory, and the container stop that follows is the backstop for the rest.
            print(f"[unload] could not release {served}: {exc}")
    return 0


async def _print_auto_update() -> int:
    """Exit 0 when the gateway auto-update + smoke test is ON, 1 when the owner has turned
    it off from the PWA. An exit code, not stdout, so the update script reads it with a
    plain `if` and needs no parsing.

    Unreachable DB reads as ON: this gates a safety net (rebuild onto the newest llama.cpp,
    then verify it can still serve), and failing closed would silently freeze the gateway
    on an old base every time the database hiccuped during an update."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        return 0 if await store.local_llm_auto_update(SYSTEM_CTX) else 1
    except Exception:  # noqa: BLE001
        return 0
    finally:
        await engine.dispose()


async def _print_patch_restore_checkpoint() -> int:
    """Exit 0 when the Fast-Qwen-loads patch (patched llama-server) is ON, 1 when off. An
    exit code, not stdout, so the update script reads it with a plain `if` (mirrors
    `_print_auto_update`).

    Unreachable DB reads as OFF: this gates an opt-in rebuild that compiles llama.cpp from
    source, and failing OPEN would trigger a ~20-30 min rebuild on any DB hiccup during an
    update. Off is the conservative default (the stock binary keeps serving)."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        return 0 if await store.local_llm_patch_restore_checkpoint(SYSTEM_CTX) else 1
    except Exception:  # noqa: BLE001
        return 1
    finally:
        await engine.dispose()


async def _set_patch_restore_checkpoint(on: bool) -> None:
    """Persist the Fast-Qwen-loads patch setting. The update script calls this with `off`
    when a patched build fails its smoke test, so a bad build turns its own toggle back off
    (no stuck-on state that keeps rebuilding an engine that cannot serve). Owner-scoped like
    the queue commands (settings RLS is is_owner())."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.upsert(SYSTEM_CTX, LOCAL_LLM_PATCH_RESTORE_CHECKPOINT_KEY, on)
        print(f"[local-llm] Fast-Qwen-loads patch set {'on' if on else 'off'}")
    finally:
        await engine.dispose()


async def _print_provision_ids() -> None:
    """Print the install queue (one catalog id per line) for the update one-shot.
    Owner-scoped (settings RLS is is_owner()); empty output is the normal 'nothing
    queued' case, so the caller treats a clean exit with no lines as no-op."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        for model_id in await store.llm_local_provision_requested(SYSTEM_CTX):
            print(model_id)
    finally:
        await engine.dispose()


async def _clear_provision_ids() -> None:
    """Empty the install queue — called by the update one-shot after a successful
    provision so a completed install stops re-appearing as queued."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.set_llm_local_provision_requested(SYSTEM_CTX, [])
    finally:
        await engine.dispose()


async def _print_remove_ids() -> None:
    """Print the uninstall queue (one catalog id per line) for the update one-shot.
    Owner-scoped (settings RLS is is_owner()); empty output is the normal 'nothing
    queued' case, so the caller treats a clean exit with no lines as no-op."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        for model_id in await store.llm_local_remove_requested(SYSTEM_CTX):
            print(model_id)
    finally:
        await engine.dispose()


async def _clear_remove_ids() -> None:
    """Empty the uninstall queue — called by the update one-shot after a successful
    pass so a completed uninstall stops re-appearing as queued."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.set_llm_local_remove_requested(SYSTEM_CTX, [])
    finally:
        await engine.dispose()


async def _local_activate(model_id: str) -> None:
    """Re-point agent.turn to a just-installed local model so it becomes the box's ACTIVE
    chat model and the WarmKeeper keeps it hot (owner decision — 'install a model' also makes
    it active). The update one-shot calls this after a successful install with the model the
    operator queued. An unknown or non-tool-capable id is a logged no-op (agent.turn is a
    tool-using agent); every OTHER stored task override is preserved. Owner-scoped like the
    queue commands (settings RLS is is_owner())."""
    from jbrain.llm import local_catalog
    from jbrain.llm.providers import active_local_override
    from jbrain.llm.router import _PRIMARY_MODEL_TASK

    model = local_catalog.get(model_id)
    if model is None or not model.supports_tools:
        print(f"[local-llm] not activating {model_id!r} — unknown or not tool-capable")
        return
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        raw = await store.get(SYSTEM_CTX, LLM_TASK_OVERRIDES_KEY, {})
        overrides = dict(raw) if isinstance(raw, dict) else {}
        overrides[_PRIMARY_MODEL_TASK] = active_local_override(model)
        await store.upsert(SYSTEM_CTX, LLM_TASK_OVERRIDES_KEY, overrides)
        print(f"[local-llm] agent.turn now routes to {model.spec} — active chat model")
    finally:
        await engine.dispose()


async def _local_llm_smoketest() -> int:
    """Smoke-test the on-box gateway's current build (the opt-in LOCAL_LLM_AUTO_UPDATE
    path calls this after floating the gateway onto the newest llama.cpp). Exit 0 =
    the build loaded a model (and survived a gpt-oss tool turn when installed) and is
    safe to keep; exit 1 = the update path should roll back to the pinned base. Reads
    the installed set + gateway URL from settings (env-wired in the api container); no
    DB needed, so it runs under `docker compose run --rm --no-deps -T api`."""
    from jbrain.llm import gpu_guard, llama_swap_config, local_catalog
    from jbrain.llm.local_gateway import LocalGatewayClient
    from jbrain.llm.smoketest import run_smoketest

    settings = get_settings()
    if not settings.local_llm_enabled or not settings.local_models:
        print("[smoketest] local hosting off or no models installed — skipping (pass)")
        return 0
    # Guarded like every other load path: the smoketest LOADS a model, and it runs
    # unattended during an update, which is the worst possible moment to discover a
    # model's device footprint the hard way.
    #
    # The window/slot loaders read the llama-swap CONFIG rather than the settings table,
    # because this command runs `--no-deps` (no DB). That is not a compromise — the config
    # is what llama-swap executes, so it cannot disagree with the served command the way a
    # re-derivation from the catalog can.
    #
    # It used to pass no loaders at all and size off the catalog window, with a comment
    # claiming the watchdog was an adequate backstop. It was not: on 2026-08-19 the tiny
    # model was projected at the catalog's 32768 while being served at the operator's
    # 262144, so a normal 3.78 GiB load broke a 3.57 GiB ceiling and the watchdog aborted
    # a healthy build. The rollback that followed was spurious — and since the newer
    # llama.cpp is where the `no_alloc` estimator lives, the broken test was blocking the
    # fix for the thing it was failing on.
    _shapes = llama_swap_config.served_shape_from_config(settings.local_models_dir)
    _by_id = {
        model.id: shape
        for served, shape in _shapes.items()
        if (model := local_catalog.get_by_served(served)) is not None
    }

    async def _windows() -> dict[str, int]:
        return {mid: shape[0] for mid, shape in _by_id.items()}

    async def _slots() -> dict[str, int]:
        return {mid: shape[1] for mid, shape in _by_id.items()}

    gateway = LocalGatewayClient(
        settings.local_llm_url,
        gpu_probe=gpu_guard.probe_for(settings),
        windows_loader=_windows,
        slots_loader=_slots,
        # The smoketest loads a model and leaves a `--no-mmap` page-cache copy behind;
        # dropping it keeps an update from leaving the box's cache full of weights nothing
        # will read again. (It loads the SMALLEST tool-capable model, not "every installed
        # model in turn" as this comment used to claim — see jbrain.llm.smoketest.)
        models_dir=settings.local_models_dir,
    )
    ok, messages = await run_smoketest(settings.local_models, gateway)
    for message in messages:
        print(f"[smoketest] {message}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbrain-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the owner principal and print the owner key")
    sub.add_parser("reset-owner-key", help="revoke the owner key and print a new one")
    sub.add_parser("local-provision-ids", help="print the local-model install queue")
    sub.add_parser("local-provision-clear", help="empty the local-model install queue")
    sub.add_parser("local-remove-ids", help="print the local-model uninstall queue")
    sub.add_parser("local-remove-clear", help="empty the local-model uninstall queue")
    p_activate = sub.add_parser(
        "local-activate",
        help="make a just-installed local model the active chat model (agent.turn)",
    )
    p_activate.add_argument("model_id", help="catalog id of the installed model to activate")
    sub.add_parser(
        "local-llm-unload",
        help="release every model the on-box gateway holds (run before an update stops it)",
    )
    sub.add_parser(
        "local-llm-auto-update",
        help="exit 0 if the owner has the gateway auto-update + smoke test on, else 1",
    )
    sub.add_parser(
        "local-llm-smoketest",
        help="load a model (+ gpt-oss tool probe) to verify the gateway's llama.cpp build",
    )
    sub.add_parser(
        "local-llm-patch-restore-checkpoint",
        help="exit 0 if the Fast-Qwen-loads patched-engine rebuild is on, else 1",
    )
    p_set_patch = sub.add_parser(
        "set-local-llm-patch-restore-checkpoint",
        help="turn the Fast-Qwen-loads patch on/off (the update script clears it on a bad build)",
    )
    p_set_patch.add_argument("state", choices=["on", "off"])
    args = parser.parse_args(argv)

    if args.command in ("init", "reset-owner-key"):
        asyncio.run(_rotate())
        return 0
    if args.command == "local-provision-ids":
        asyncio.run(_print_provision_ids())
        return 0
    if args.command == "local-provision-clear":
        asyncio.run(_clear_provision_ids())
        return 0
    if args.command == "local-remove-ids":
        asyncio.run(_print_remove_ids())
        return 0
    if args.command == "local-remove-clear":
        asyncio.run(_clear_remove_ids())
        return 0
    if args.command == "local-activate":
        asyncio.run(_local_activate(args.model_id))
        return 0
    if args.command == "local-llm-unload":
        return asyncio.run(_local_llm_unload())
    if args.command == "local-llm-auto-update":
        return asyncio.run(_print_auto_update())
    if args.command == "local-llm-smoketest":
        return asyncio.run(_local_llm_smoketest())
    if args.command == "local-llm-patch-restore-checkpoint":
        return asyncio.run(_print_patch_restore_checkpoint())
    if args.command == "set-local-llm-patch-restore-checkpoint":
        asyncio.run(_set_patch_restore_checkpoint(args.state == "on"))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
