"""POST /disk/cleanup: what each action may remove, dry run vs apply, and the route.

A fake probe stands in for the gateway; the gateway's own docker calls are pinned
against a fake docker client at the bottom.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient

from supervisor import disk_cleanup
from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.disk_cleanup import (
    ORPHAN_VOLUME_ALLOWLIST,
    DiskCleanup,
    compose_image_repos,
    dockerfile_repos,
    image_variables,
    parse_env,
    repo_of,
)
from supervisor.gateway import ComposeDockerGateway, HelperRun
from tests.conftest import AUTH, TOKEN, FakeGateway

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

GiB = 1 << 30

COMPOSE = """
name: jbrain
services:
  api:
    image: jbrain2-api:local
  comfyui:
    image: ${COMFYUI_IMAGE:-docker.io/kyuz0/amd-strix-halo-comfyui:latest}
  reader:
    image: ${READER_IMAGE:-ghcr.io/x/reader@sha256:abc}
  db:
    image: "timescale/timescaledb-ha:pg17"   # quoted, with a comment
  wall:
    build: ./wall
  llm:
    image: jbrain2-local-llm:local
    build:
      args:
        LLM_BASE: ${LLM_BASE:-docker.io/kyuz0/toolboxes@sha256:cea7}
        GIT_SHA: ${GIT_SHA:-unknown}
    environment:
      DB_URL: postgresql://x:${APP_DB_PASSWORD}@db/jbrain
"""

DOCKERFILE_LINES = """FROM python:3.12-slim AS base
ARG WHISPER_BASE=ghcr.io/mostlygeek/llama-swap:vulkan
FROM ${WHISPER_BASE} AS build
ARG LLAMA_CPP_COMMIT=869034b4
ARG WIKITEXT_URL=https://huggingface.co/x.zip
FROM --platform=linux/amd64 golang:1.23-bookworm AS swap
"""


def _image(
    image_id: str,
    tags: list[str] | None,
    *,
    size: int,
    shared: int = 0,
    containers: int = 0,
    digests: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "Id": f"sha256:{image_id}",
        "RepoTags": tags,
        "RepoDigests": digests or [],
        "Size": size,
        "SharedSize": shared,
        "Containers": containers,
    }


DF: dict[str, Any] = {
    "Images": [
        _image("used", ["jbrain2-api:local"], size=10 * GiB, containers=3),
        # A profile service that is not running: no container, still the stack's.
        _image("jcode", ["jbrain2-jcode:local"], size=4 * GiB),
        _image("wall", ["jbrain-wall:latest"], size=GiB),
        # An older comfyui tag: any tag of a compose repository is kept.
        _image("comfy", ["kyuz0/amd-strix-halo-comfyui:old"], size=20 * GiB),
        _image(
            "reader",
            None,
            size=GiB,
            digests=["ghcr.io/x/reader@sha256:abc"],
        ),
        _image("alpine", ["alpine:latest"], size=8 << 20),
        _image("dockercli", ["docker:cli"], size=100 << 20),
        _image("dangling", ["<none>:<none>"], size=3 * GiB, shared=GiB),
        _image("stale", ["someone/tool:1", "someone/tool:latest"], size=2 * GiB),
        _image("shared", ["base/thing:1"], size=GiB, shared=GiB),
        _image("unknown", ["x/y:1"], size=GiB, containers=-1),
        # Containers==0 in df but a container's ImageID names it: kept.
        _image("racing", ["x/race:1"], size=GiB),
    ],
    "Containers": [{"Id": "c1", "ImageID": "sha256:racing"}],
    "Volumes": [
        {"Name": "jbrain_llm_kv", "UsageData": {"Size": 26 * GiB, "RefCount": 0}},
        {"Name": "jbrain_blobs", "UsageData": {"Size": 20 * GiB, "RefCount": 0}},
        {"Name": "jbrain_db_data", "UsageData": {"Size": 90 * GiB, "RefCount": 0}},
        {"Name": "random_orphan", "UsageData": {"Size": 5 * GiB, "RefCount": 0}},
    ],
    "BuildCache": [
        {"ID": "a", "Size": 6 * GiB, "InUse": False, "Shared": False},
        {"ID": "b", "Size": 2 * GiB, "InUse": True, "Shared": False},
    ],
}


class FakeCleanupProbe:
    def __init__(
        self,
        df: dict[str, Any] | None = None,
        *,
        compose: str | Exception = COMPOSE,
        env: HelperRun | Exception | None = None,
        dockerfiles: HelperRun | Exception | None = None,
        fail_images: set[str] | None = None,
        fail_volume: bool = False,
        fail_prune: bool = False,
    ) -> None:
        self.df = df if df is not None else DF
        self.compose = compose
        # grep exits 1 when no image variable is overridden: the common case.
        self.env: HelperRun | Exception = env or HelperRun(1, "", "")
        self.dockerfiles: HelperRun | Exception = dockerfiles or HelperRun(
            0, DOCKERFILE_LINES, ""
        )
        self.fail_images = fail_images or set()
        self.fail_volume = fail_volume
        self.fail_prune = fail_prune
        self.helper_runs: list[tuple[list[str], dict[str, str]]] = []
        self.pruned = 0
        self.removed_images: list[str] = []
        self.removed_volumes: list[str] = []
        # Called at the start of an apply, to act while the cleanup holds its slot.
        self.during: Any = None

    def docker_df(self) -> dict[str, Any]:
        if self.during is not None:
            self.during()
        return self.df

    def run_readonly_helper(
        self, argv: Sequence[str], mounts: Mapping[str, str], timeout_s: float
    ) -> HelperRun:
        self.helper_runs.append((list(argv), dict(mounts)))
        (inside,) = mounts.values()
        answer: HelperRun | Exception | str = {
            disk_cleanup.COMPOSE_MOUNT: self.compose,
            disk_cleanup.ENV_MOUNT: self.env,
            disk_cleanup.SRC_MOUNT: self.dockerfiles,
        }[inside]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return HelperRun(exit_code=0, stdout=answer, stderr="")
        return answer

    def prune_build_cache(self) -> int:
        if self.fail_prune:
            raise RuntimeError("builder busy")
        self.pruned += 1
        return 6 * GiB

    def remove_image(self, ref: str) -> None:
        if ref in self.fail_images:
            raise RuntimeError("conflict: image is being used")
        self.removed_images.append(ref)

    def remove_volume(self, name: str) -> None:
        if self.fail_volume:
            raise RuntimeError("volume is in use")
        self.removed_volumes.append(name)


def _cleanup(probe: FakeCleanupProbe, applied: list[int] | None = None) -> DiskCleanup:
    hook = applied if applied is not None else []
    return DiskCleanup(
        probe, "jbrain", "/opt/jbrain2/", on_applied=lambda: hook.append(1)
    )


def test_repo_of_folds_tags_digests_and_hub_prefixes() -> None:
    assert repo_of("docker.io/library/alpine:3.20") == "alpine"
    assert repo_of("alpine") == "alpine"
    assert repo_of("localhost:5000/x/y:1") == "localhost:5000/x/y"
    assert repo_of("ghcr.io/x/reader@sha256:abc") == "ghcr.io/x/reader"
    assert repo_of("docker.io/kyuz0/comfy:latest") == "kyuz0/comfy"


def test_compose_repos_resolve_defaults_and_refuse_the_unresolvable() -> None:
    assert compose_image_repos(COMPOSE, {}) == {
        "jbrain2-api",
        "jbrain2-local-llm",
        "kyuz0/amd-strix-halo-comfyui",
        "ghcr.io/x/reader",
        "timescale/timescaledb-ha",
        # The build-base arg's default: the next build needs it.
        "kyuz0/toolboxes",
    }
    bare = "services:\n  a:\n    image: ${NO_DEFAULT}\n"
    assert compose_image_repos(bare, {}) is None
    assert compose_image_repos(bare, {"NO_DEFAULT": "me/x:1"}) == {"me/x"}
    assert compose_image_repos("services: {}\n", {}) is None


def test_env_overrides_are_protected_beside_the_defaults() -> None:
    repos = compose_image_repos(
        COMPOSE, {"COMFYUI_IMAGE": "me/comfy:2", "LLM_BASE": "me/base@sha256:1"}
    )
    assert repos is not None
    assert {"me/comfy", "me/base", "kyuz0/toolboxes"} <= repos
    # The overridden image: line resolves to the override, and the default stays too.
    assert "kyuz0/amd-strix-halo-comfyui" in repos


def test_only_image_variables_are_read_from_the_env_file() -> None:
    names = image_variables(COMPOSE)
    assert names == {"COMFYUI_IMAGE", "READER_IMAGE", "LLM_BASE"}
    # Never a secret: APP_DB_PASSWORD is referenced but not image-shaped.
    assert "APP_DB_PASSWORD" not in names and "GIT_SHA" not in names


def test_parse_env_takes_plain_quoted_and_exported_lines() -> None:
    assert parse_env("A=x/y:1\nexport B='q/r'\n# C=no\nD\n") == {
        "A": "x/y:1",
        "B": "q/r",
    }


def test_dockerfile_bases_and_image_args_are_protected() -> None:
    assert dockerfile_repos(DOCKERFILE_LINES) == {
        "python",
        "ghcr.io/mostlygeek/llama-swap",
        "golang",
    }


def test_the_real_compose_file_and_dockerfiles_parse_and_name_the_stack() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "deploy" / "docker-compose.yml").read_text()
    repos = compose_image_repos(compose, {})
    assert repos is not None
    assert {"jbrain2-api", "jbrain2-supervisor", "timescale/timescaledb-ha"} <= repos
    # Build bases reach the protected set through compose args.
    assert "kyuz0/amd-strix-halo-toolboxes" in repos
    assert "ghcr.io/mostlygeek/llama-swap" in repos
    names = image_variables(compose)
    assert "APP_DB_PASSWORD" not in names and "SUPERVISOR_TOKEN" not in names
    dockerfiles = "\n".join(
        p.read_text() for p in (root / "deploy").glob("Dockerfile.*")
    )
    assert {"python", "caddy", "node"} <= dockerfile_repos(dockerfiles)


def test_dry_run_reports_and_removes_nothing() -> None:
    probe = FakeCleanupProbe()
    applied: list[int] = []
    result = _cleanup(probe, applied).run(
        ["orphan_volumes", "unused_images", "build_cache"], dry_run=True
    )
    assert result.dry_run
    assert [a.action for a in result.actions] == [
        "build_cache",
        "unused_images",
        "orphan_volumes",
    ]
    cache, images, volumes = result.actions
    assert cache.bytes == 6 * GiB
    assert {i.name for i in images.items} == {
        "dangling",
        "someone/tool:1, someone/tool:latest",
    }
    assert images.bytes == 2 * GiB + 2 * GiB
    assert [v.name for v in volumes.items] == ["jbrain_llm_kv"]
    assert result.total_bytes == 6 * GiB + 4 * GiB + 26 * GiB
    assert probe.pruned == 0
    assert probe.removed_images == [] and probe.removed_volumes == []
    assert applied == []


def test_compose_and_stack_images_are_protected() -> None:
    result = _cleanup(FakeCleanupProbe()).run(["unused_images"], dry_run=True)
    (images,) = result.actions
    kept = {k.name: k.reason for k in images.kept}
    for name in (
        "jbrain2-jcode:local",
        "jbrain-wall:latest",
        "kyuz0/amd-strix-halo-comfyui:old",
        "reader",
        "alpine:latest",
        "docker:cli",
    ):
        assert kept[name] == "compose/stack image", name
    assert kept["base/thing:1"] == "frees nothing"
    listed = {i.name for i in images.items} | set(kept)
    # In use (by count, an uncomputed count, or a container's ImageID): not touched,
    # not even listed.
    assert not listed & {"jbrain2-api:local", "x/y:1", "x/race:1"}


def test_the_stack_is_read_read_only_from_the_box() -> None:
    probe = FakeCleanupProbe()
    _cleanup(probe).run(["unused_images"], dry_run=True)
    compose, env, src = probe.helper_runs
    assert compose == (
        ["cat", disk_cleanup.COMPOSE_MOUNT],
        {"/opt/jbrain2/docker-compose.yml": disk_cleanup.COMPOSE_MOUNT},
    )
    # Only the image variables' lines are asked for, never the whole .env.
    assert env[0][:2] == ["grep", "-E"] and env[0][-1] == disk_cleanup.ENV_MOUNT
    assert env[0][2] == (
        "^[[:space:]]*(export[[:space:]]+)?(COMFYUI_IMAGE|LLM_BASE|READER_IMAGE)="
    )
    assert env[1] == {"/opt/jbrain2/.env": disk_cleanup.ENV_MOUNT}
    assert src[0][0] == "grep" and "--include=Dockerfile*" in src[0]
    assert src[1] == {"/opt/jbrain2/src": disk_cleanup.SRC_MOUNT}


@pytest.mark.parametrize(
    "probe",
    [
        FakeCleanupProbe(compose=RuntimeError("no such file")),
        FakeCleanupProbe(env=HelperRun(2, "", "grep: /mnt/env: Permission denied")),
        FakeCleanupProbe(env=RuntimeError("daemon hiccup")),
        FakeCleanupProbe(dockerfiles=HelperRun(1, "", "")),
        FakeCleanupProbe(dockerfiles=RuntimeError("bind source path does not exist")),
    ],
    ids=["compose", "env-exit-2", "env-raises", "no-dockerfiles", "no-src"],
)
def test_anything_unreadable_removes_no_image(probe: FakeCleanupProbe) -> None:
    (images,) = _cleanup(probe).run(["unused_images"], dry_run=False).actions
    assert images.items == [] and images.bytes == 0
    assert probe.removed_images == []
    assert "no image removed" in images.errors[0]


def test_a_box_without_an_env_file_runs_the_defaults() -> None:
    probe = FakeCleanupProbe(
        env=RuntimeError("bind source path does not exist: /opt/jbrain2/.env")
    )
    (images,) = _cleanup(probe).run(["unused_images"], dry_run=True).actions
    assert images.errors == [] and images.items


def test_build_bases_and_env_overrides_are_kept() -> None:
    df = {
        **DF,
        "Images": [
            _image("py", ["python:3.12-slim"], size=GiB),
            _image("swap", ["ghcr.io/mostlygeek/llama-swap:vulkan"], size=GiB),
            _image("tb", None, size=GiB, digests=["kyuz0/toolboxes@sha256:cea7"]),
            _image("mine", ["me/comfy:2"], size=GiB),
            _image("junk", ["junk/x:1"], size=GiB),
        ],
    }
    probe = FakeCleanupProbe(df, env=HelperRun(0, "COMFYUI_IMAGE=me/comfy:2\n", ""))
    (images,) = _cleanup(probe).run(["unused_images"], dry_run=False).actions
    assert probe.removed_images == ["sha256:junk"]
    assert {k.name for k in images.kept} == {
        "python:3.12-slim",
        "ghcr.io/mostlygeek/llama-swap:vulkan",
        "tb",
        "me/comfy:2",
    }


def test_an_image_with_unknown_shared_size_is_kept() -> None:
    df = {**DF, "Images": [_image("odd", ["odd/x:1"], size=GiB, shared=-1)]}
    probe = FakeCleanupProbe(df)
    (images,) = _cleanup(probe).run(["unused_images"], dry_run=False).actions
    assert probe.removed_images == [] and images.bytes == 0
    assert [(k.name, k.reason) for k in images.kept] == [
        ("odd/x:1", "shared size unknown")
    ]


def test_apply_removes_by_id_or_by_each_tag_and_reports_freed() -> None:
    probe = FakeCleanupProbe()
    applied: list[int] = []
    result = _cleanup(probe, applied).run(["unused_images"], dry_run=False)
    (images,) = result.actions
    # One tag or none: by id. Several: each tag, since an unforced rmi of the id
    # refuses an image referenced from more than one repository.
    assert probe.removed_images == [
        "sha256:dangling",
        "someone/tool:1",
        "someone/tool:latest",
    ]
    assert images.bytes == 4 * GiB and images.errors == []
    assert applied == [1]


def test_image_errors_are_surfaced_and_not_counted() -> None:
    probe = FakeCleanupProbe(fail_images={"sha256:dangling"})
    (images,) = _cleanup(probe).run(["unused_images"], dry_run=False).actions
    assert images.bytes == 2 * GiB
    assert [i.name for i in images.items] == ["someone/tool:1, someone/tool:latest"]
    assert images.errors == ["dangling: RuntimeError: conflict: image is being used"]


def test_only_the_allowlisted_orphan_volume_is_removed() -> None:
    probe = FakeCleanupProbe()
    (volumes,) = _cleanup(probe).run(["orphan_volumes"], dry_run=False).actions
    # db_data, blobs and an unknown orphan all have ref_count 0 here: none is touched.
    assert probe.removed_volumes == ["jbrain_llm_kv"]
    assert volumes.bytes == 26 * GiB


def test_the_allowlist_never_names_owner_data() -> None:
    assert frozenset({"jbrain_llm_kv"}) == ORPHAN_VOLUME_ALLOWLIST
    assert not {"jbrain_blobs", "jbrain_db_data"} & ORPHAN_VOLUME_ALLOWLIST


@pytest.mark.parametrize(("refs", "reason"), [(1, "in use (1)"), (-1, "unknown")])
def test_an_allowlisted_volume_in_use_is_refused(refs: int, reason: str) -> None:
    df = {
        **DF,
        "Volumes": [
            {"Name": "jbrain_llm_kv", "UsageData": {"Size": GiB, "RefCount": refs}}
        ],
    }
    probe = FakeCleanupProbe(df)
    (volumes,) = _cleanup(probe).run(["orphan_volumes"], dry_run=False).actions
    assert probe.removed_volumes == [] and volumes.bytes == 0
    assert reason in volumes.kept[0].reason


def test_volume_and_prune_errors_are_surfaced() -> None:
    probe = FakeCleanupProbe(fail_volume=True, fail_prune=True)
    cache, volumes = (
        _cleanup(probe).run(["build_cache", "orphan_volumes"], dry_run=False).actions
    )
    assert cache.bytes == 0
    assert cache.errors == ["builder prune: RuntimeError: builder busy"]
    assert volumes.bytes == 0
    assert volumes.errors == ["jbrain_llm_kv: RuntimeError: volume is in use"]


def test_apply_build_cache_reports_what_the_daemon_reclaimed() -> None:
    probe = FakeCleanupProbe()
    (cache,) = _cleanup(probe).run(["build_cache"], dry_run=False).actions
    assert probe.pruned == 1 and cache.bytes == 6 * GiB and not cache.dry_run


def test_a_df_failure_fails_every_action_without_removing() -> None:
    class Broken(FakeCleanupProbe):
        def docker_df(self) -> dict[str, Any]:
            raise RuntimeError("daemon said no")

    probe = Broken()
    result = _cleanup(probe).run(["build_cache", "orphan_volumes"], dry_run=False)
    assert [a.errors for a in result.actions] == [
        ["docker df: RuntimeError: daemon said no"]
    ] * 2
    assert probe.pruned == 0 and probe.removed_volumes == []


# --- route ------------------------------------------------------------------


def _client(
    probe: FakeCleanupProbe | None, gateway: FakeGateway | None = None
) -> TestClient:
    cleanup = _cleanup(probe) if probe is not None else None
    app = create_app(
        Settings(supervisor_token=TOKEN),
        gateway or FakeGateway([]),
        watch_api=False,
        cleanup=cleanup,
    )
    return TestClient(app)


def test_cleanup_route_requires_the_token() -> None:
    probe = FakeCleanupProbe()
    with _client(probe) as client:
        resp = client.post(
            "/disk/cleanup", json={"actions": ["build_cache"], "dry_run": False}
        )
        assert resp.status_code == 401
    assert probe.pruned == 0


def test_cleanup_route_defaults_to_a_dry_run() -> None:
    probe = FakeCleanupProbe()
    with _client(probe) as client:
        resp = client.post(
            "/disk/cleanup", headers=AUTH, json={"actions": ["build_cache"]}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True and body["total_bytes"] == 6 * GiB
    assert probe.pruned == 0


def test_cleanup_route_applies_when_asked() -> None:
    probe = FakeCleanupProbe()
    with _client(probe) as client:
        resp = client.post(
            "/disk/cleanup",
            headers=AUTH,
            json={"actions": ["orphan_volumes"], "dry_run": False},
        )
    assert resp.status_code == 200
    assert probe.removed_volumes == ["jbrain_llm_kv"]


@pytest.mark.parametrize(
    "body",
    [
        {"actions": []},
        {"actions": ["everything"]},
        {"actions": ["build_cache"], "volumes": ["jbrain_db_data"]},
        {},
    ],
)
def test_cleanup_route_rejects_anything_off_the_fixed_set(body: dict[str, Any]) -> None:
    probe = FakeCleanupProbe()
    with _client(probe) as client:
        assert client.post("/disk/cleanup", headers=AUTH, json=body).status_code == 422


def test_cleanup_route_refuses_to_apply_under_a_running_one_shot() -> None:
    probe = FakeCleanupProbe()
    gateway = FakeGateway([])
    gateway.updater_running = True
    with _client(probe, gateway) as client:
        applied = client.post(
            "/disk/cleanup",
            headers=AUTH,
            json={"actions": ["unused_images"], "dry_run": False},
        )
        assert applied.status_code == 409
        assert "update" in applied.json()["detail"]
        # A dry run only reads, so it is still answered.
        dry = client.post(
            "/disk/cleanup", headers=AUTH, json={"actions": ["unused_images"]}
        )
        assert dry.status_code == 200
    assert probe.removed_images == []


def test_cleanup_route_without_cleanup_is_503() -> None:
    with _client(None) as client:
        resp = client.post(
            "/disk/cleanup", headers=AUTH, json={"actions": ["build_cache"]}
        )
        assert resp.status_code == 503


def _app_with(cleanup: DiskCleanup, gateway: FakeGateway) -> TestClient:
    app = create_app(
        Settings(supervisor_token=TOKEN), gateway, watch_api=False, cleanup=cleanup
    )
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/update", None),
        ("/export", None),
        ("/import", {"archive": "import-20261004-120000.jbrain.tar"}),
        ("/reset", None),
        ("/provision", None),
    ],
)
def test_no_one_shot_starts_while_a_cleanup_applies(
    path: str, body: dict[str, Any] | None
) -> None:
    cleanup = _cleanup(FakeCleanupProbe())
    gateway = FakeGateway([])
    with _app_with(cleanup, gateway) as client:
        assert cleanup.reserve_apply()
        try:
            resp = client.post(path, headers=AUTH, json=body)
            assert resp.status_code == 409, resp.text
            assert "disk cleanup" in resp.json()["detail"]
        finally:
            cleanup.run_reserved([])
        assert gateway.oneshots_started == [] and not gateway.updater_running
        # Once it ends, the same start goes through.
        assert client.post(path, headers=AUTH, json=body).status_code == 202


def test_an_applied_cleanup_holds_the_slot_for_its_whole_run() -> None:
    probe = FakeCleanupProbe()
    cleanup = _cleanup(probe)
    seen: list[bool] = []
    probe.during = lambda: seen.append(cleanup.applying)
    with _app_with(cleanup, FakeGateway([])) as client:
        resp = client.post(
            "/disk/cleanup",
            headers=AUTH,
            json={"actions": ["build_cache"], "dry_run": False},
        )
        assert resp.status_code == 200
    assert seen == [True] and not cleanup.applying
    # A dry run never claims it: a one-shot may start beside a read.
    cleanup.run(["build_cache"], dry_run=True)
    assert seen == [True, False]


def test_a_second_cleanup_while_one_runs_is_refused() -> None:
    cleanup = _cleanup(FakeCleanupProbe())
    cleanup._lock.acquire()  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(disk_cleanup.CleanupBusyError):
            cleanup.run(["build_cache"], dry_run=True)
    finally:
        cleanup._lock.release()  # pyright: ignore[reportPrivateUsage]


# --- gateway docker calls ---------------------------------------------------


class _Api:
    def __init__(self) -> None:
        self.prune_kwargs: dict[str, Any] = {}

    def prune_builds(self, **kwargs: Any) -> dict[str, Any]:
        self.prune_kwargs = kwargs
        return {"CachesDeleted": ["a"], "SpaceReclaimed": 123}


class _Images:
    def __init__(self) -> None:
        self.removed: list[tuple[str, bool]] = []

    def remove(self, image: str, force: bool = False, noprune: bool = False) -> None:
        self.removed.append((image, force))


class _Volume:
    def __init__(self) -> None:
        self.force: bool | None = None

    def remove(self, force: bool = False) -> None:
        self.force = force


class _Volumes:
    def __init__(self) -> None:
        self.volume = _Volume()
        self.asked: list[str] = []

    def get(self, name: str) -> _Volume:
        self.asked.append(name)
        return self.volume


class _Client:
    def __init__(self) -> None:
        self.api = _Api()
        self.images = _Images()
        self.volumes = _Volumes()


def _gw(client: _Client) -> ComposeDockerGateway:
    return ComposeDockerGateway(cast(Any, client), "jbrain", "/opt/jbrain2")


def test_gateway_prunes_all_unused_build_cache() -> None:
    client = _Client()
    assert _gw(client).prune_build_cache() == 123
    assert client.api.prune_kwargs == {"all": True}


def test_gateway_never_forces_an_image_or_volume_removal() -> None:
    client = _Client()
    gw = _gw(client)
    gw.remove_image("sha256:abc")
    gw.remove_volume("jbrain_llm_kv")
    assert client.images.removed == [("sha256:abc", False)]
    assert client.volumes.asked == ["jbrain_llm_kv"]
    assert client.volumes.volume.force is False
