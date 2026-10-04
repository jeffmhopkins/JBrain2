"""The /disk breakdown: df parsing, du parsing, helper failures, cache, route.

No daemon: a fake probe stands in for the gateway, and the gateway's own helper
runner is driven against a fake docker client to pin its sandboxing.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient

from supervisor import disk_usage
from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.disk_usage import (
    DOCKER_ROOT_MOUNT,
    PROJECT_MOUNT,
    DiskUsage,
    parse_build_cache,
    parse_containers,
    parse_du,
    parse_images,
    parse_statfs,
    parse_volumes,
)
from supervisor.gateway import (
    DISK_HELPER_IMAGE,
    DISK_HELPER_LABEL,
    ComposeDockerGateway,
    HelperRun,
)
from tests.conftest import AUTH, TOKEN, FakeGateway

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

GiB = 1 << 30

DF: dict[str, Any] = {
    "LayersSize": 50 * GiB,
    "Images": [
        {
            "Id": "sha256:aaaaaaaaaaaaffff",
            "RepoTags": ["jbrain2-api:local"],
            "Created": 1_790_000_000,
            "Size": 10 * GiB,
            "SharedSize": 2 * GiB,
            "Containers": 2,
        },
        {
            "Id": "sha256:bbbbbbbbbbbbffff",
            "RepoTags": ["<none>:<none>"],
            "Created": 1_780_000_000,
            "Size": 30 * GiB,
            "SharedSize": 2 * GiB,
            "Containers": 0,
        },
        {
            "Id": "sha256:cccccccccccc",
            "RepoTags": None,
            "Created": 0,
            "Size": 5 * GiB,
            "SharedSize": -1,
            "Containers": 0,
        },
        {
            "Id": "sha256:dddddddddddd",
            "RepoTags": ["x:1"],
            "Size": 1 * GiB,
            "SharedSize": 0,
            "Containers": -1,
        },
    ],
    "Containers": [
        {
            "Id": "c1" * 10,
            "Names": ["/jbrain-api-1"],
            "Image": "jbrain2-api:local",
            "State": "running",
            "SizeRw": 3 * GiB,
            "SizeRootFs": 13 * GiB,
            "Labels": {"com.docker.compose.project": "jbrain"},
        },
        {
            "Id": "c2" * 10,
            "Names": ["/old-thing"],
            "Image": "x:1",
            "State": "exited",
            "SizeRw": 4 * GiB,
            "Labels": {},
        },
    ],
    "Volumes": [
        {
            "Name": "jbrain_blobs",
            "Labels": {
                "com.docker.compose.project": "jbrain",
                "com.docker.compose.volume": "blobs",
            },
            "UsageData": {"Size": 20 * GiB, "RefCount": 2},
        },
        {
            "Name": "orphan",
            "Labels": None,
            "UsageData": {"Size": 7 * GiB, "RefCount": 0},
        },
        {"Name": "unknown", "Labels": {}, "UsageData": {"Size": -1, "RefCount": -1}},
        {
            "Name": "jbrain_db_data",
            "Labels": {"com.docker.compose.project": "jbrain"},
            "UsageData": {"Size": 90 * GiB, "RefCount": 1},
        },
    ],
    "BuildCache": [
        {"ID": "a", "Size": 6 * GiB, "InUse": False, "Shared": False},
        {"ID": "b", "Size": 2 * GiB, "InUse": True, "Shared": False},
        {"ID": "c", "Size": 1 * GiB, "InUse": False, "Shared": True},
    ],
}


def test_images_use_layers_size_and_dockers_reclaimable_formula() -> None:
    images = parse_images(DF)
    # LayersSize is the true total; summing Size would count the shared 2 GiB twice.
    assert images.total_bytes == 50 * GiB
    assert images.total_source == "LayersSize"
    assert images.count == 4
    # docker's formula: LayersSize less the unique bytes of in-use images — a
    # (10 - 2 shared) and d, whose uncomputed count (-1) reads as in use (1 - 0).
    assert images.reclaimable_bytes == (50 - 8 - 1) * GiB
    assert images.in_use_count == 2
    assert [i.size_bytes for i in images.top] == [30 * GiB, 10 * GiB, 5 * GiB, GiB]
    top = images.top[0]
    assert top.id == "bbbbbbbbbbbb"
    assert top.repo_tags == []  # the dangling <none>:<none> is not a tag
    assert top.unique_bytes == 28 * GiB and not top.in_use
    assert top.created is not None and top.created.startswith("2026-")
    assert images.top[2].created is None and images.top[2].shared_bytes is None


def test_images_fall_back_to_summed_sizes_without_layers_size() -> None:
    payload = {**DF, "LayersSize": None}
    images = parse_images(payload)
    assert images.total_source == "sum_of_sizes"
    assert images.total_bytes == 46 * GiB
    # Without LayersSize: the unique bytes of unused images — b (30 - 2) and c, whose
    # unknown SharedSize counts the whole 5.
    assert images.reclaimable_bytes == 33 * GiB


def test_an_in_use_image_with_unknown_shared_size_subtracts_all_of_it() -> None:
    payload = {
        "LayersSize": 10 * GiB,
        "Images": [{"Id": "x", "Size": 4 * GiB, "SharedSize": -1, "Containers": 1}],
    }
    assert parse_images(payload).reclaimable_bytes == 6 * GiB


def test_images_top_is_capped() -> None:
    many = {
        "Images": [
            {"Id": f"sha256:{i:012d}", "Size": i, "SharedSize": 0, "Containers": 1}
            for i in range(40)
        ]
    }
    images = parse_images(many)
    assert len(images.top) == disk_usage.TOP_IMAGES
    assert images.top[0].size_bytes == 39


def test_containers_sorted_with_reclaimable_and_project_flag() -> None:
    containers = parse_containers(DF, "jbrain")
    assert [c.name for c in containers.top] == ["old-thing", "jbrain-api-1"]
    assert containers.total_rw_bytes == 7 * GiB
    assert containers.reclaimable_bytes == 4 * GiB  # only the exited one
    assert containers.top[1].project and not containers.top[0].project
    assert containers.top[0].size_rootfs_bytes is None


def test_volumes_sorted_desc_unknown_last_and_project_flagged() -> None:
    volumes = parse_volumes(DF, "jbrain")
    assert [v.name for v in volumes.items] == [
        "jbrain_db_data",
        "jbrain_blobs",
        "orphan",
        "unknown",
    ]
    assert volumes.items[-1].size_bytes is None
    assert volumes.total_bytes == 117 * GiB
    assert volumes.project_bytes == 110 * GiB
    assert volumes.reclaimable_bytes == 7 * GiB
    assert volumes.items[1].compose_volume == "blobs"
    assert not volumes.items[2].project


def test_build_cache_follows_docker_system_df_rules() -> None:
    cache = parse_build_cache(DF)
    assert cache.count == 3
    assert cache.total_bytes == 8 * GiB  # the shared record is not counted twice
    # total less what a build holds (b); the shared record is in neither.
    assert cache.reclaimable_bytes == 6 * GiB


def test_parse_du_keeps_top_level_and_one_deeper_under_model_dirs() -> None:
    out = "\n".join(
        [
            f"100\t{PROJECT_MOUNT}/src/backend",  # depth 2 outside DEEP_DIRS: dropped
            f"200\t{PROJECT_MOUNT}/src",
            f"5000\t{PROJECT_MOUNT}/local-models/gpt oss 120b",  # a space survives
            f"7\t{PROJECT_MOUNT}/local-models/tab\there",  # so does a tab
            f"9\t{PROJECT_MOUNT}/local-models/a/deeper",  # depth 3: dropped
            f"6000\t{PROJECT_MOUNT}/local-models",
            f"300\t{PROJECT_MOUNT}/backups/jbrain-2026-10-01.tar",
            f"300\t{PROJECT_MOUNT}/backups",
            f"1\t{PROJECT_MOUNT}/.env",
            "garbage line",
            "12\tline-two-of-a-name-with-a-newline",
            "x1\t/mnt/project/bad-size",
            f"6600\t{PROJECT_MOUNT}",
        ]
    )
    total, entries = parse_du(out, PROJECT_MOUNT, "/opt/jbrain2/")
    assert total == 6600 * 1024
    assert [(e.path, e.bytes // 1024) for e in entries] == [
        ("/opt/jbrain2/local-models", 6000),
        ("/opt/jbrain2/local-models/gpt oss 120b", 5000),
        ("/opt/jbrain2/backups", 300),
        ("/opt/jbrain2/backups/jbrain-2026-10-01.tar", 300),
        ("/opt/jbrain2/src", 200),
        ("/opt/jbrain2/local-models/tab\there", 7),
        ("/opt/jbrain2/.env", 1),
    ]


def test_parse_du_ignores_a_sibling_sharing_the_prefix() -> None:
    total, entries = parse_du(
        f"5\t{PROJECT_MOUNT}-other/x\n", PROJECT_MOUNT, "/opt/jbrain2"
    )
    assert total is None and entries == []


def test_parse_statfs() -> None:
    stats = parse_statfs(
        f"{PROJECT_MOUNT} abc123 4096 100 40 30\nnonsense\n{DOCKER_ROOT_MOUNT} x 1 2\n"
    )
    assert list(stats) == [PROJECT_MOUNT]
    fs = stats[PROJECT_MOUNT]
    assert fs.fsid == "abc123"
    assert fs.total_bytes == 409600
    assert fs.used_bytes == 60 * 4096
    assert fs.free_bytes == 30 * 4096


class FakeProbe:
    """A DiskProbe recording helper invocations; outputs keyed by argv[0]."""

    def __init__(
        self,
        *,
        stat_out: str = "",
        du_out: str = "",
        du_exit: int | None = 0,
        du_raises: bool = False,
        df_raises: bool = False,
        root: str | None = "/var/lib/docker",
        gate: threading.Event | None = None,
    ) -> None:
        # When set, docker_df signals `entered` and blocks on `gate`: a build held
        # open so a test can race other callers against it.
        self.gate = gate
        self.entered = threading.Event()
        self.stat_out = stat_out
        self.du_out = du_out
        self.du_exit = du_exit
        self.du_raises = du_raises
        self.df_raises = df_raises
        self.root = root
        self.runs: list[tuple[list[str], dict[str, str], float]] = []
        self.df_calls = 0

    def docker_df(self) -> dict[str, Any]:
        self.df_calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.df_raises:
            raise RuntimeError("daemon said no")
        return DF

    def docker_root_dir(self) -> str | None:
        return self.root

    def run_readonly_helper(
        self, argv: Sequence[str], mounts: Mapping[str, str], timeout_s: float
    ) -> HelperRun:
        self.runs.append((list(argv), dict(mounts), timeout_s))
        if argv[0] == "stat":
            return HelperRun(exit_code=0, stdout=self.stat_out, stderr="")
        if self.du_raises:
            raise RuntimeError("image not found")
        return HelperRun(
            exit_code=self.du_exit, stdout=self.du_out, stderr="du: cannot read 'x'"
        )


def _vfs(total_blocks: int, free: int, avail: int) -> os.statvfs_result:
    return os.statvfs_result((4096, 4096, total_blocks, free, avail, 0, 0, 0, 0, 255))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _usage(probe: FakeProbe, clock: Clock | None = None) -> DiskUsage:
    return DiskUsage(
        probe,
        "jbrain",
        "/opt/jbrain2",
        clock=clock or Clock(),
        statvfs=lambda _p: _vfs(1000, 300, 250),
    )


_ONE_DISK = "fs1 4096 1000 300 250"
SAME_DISK_STAT = f"{PROJECT_MOUNT} {_ONE_DISK}\n{DOCKER_ROOT_MOUNT} {_ONE_DISK}\n"


def test_report_folds_one_disk_and_runs_helpers_sandboxed() -> None:
    probe = FakeProbe(
        stat_out=SAME_DISK_STAT,
        du_out=f"10\t{PROJECT_MOUNT}/backups\n20\t{PROJECT_MOUNT}\n",
    )
    report = _usage(probe).report()
    assert report.errors == []
    # Supervisor `/`, the data root and the project dir are all one disk.
    assert len(report.filesystem) == 1
    fs = report.filesystem[0]
    assert fs.paths == [
        "/ (supervisor)",
        "docker data root /var/lib/docker",
        "project /opt/jbrain2",
    ]
    assert fs.fsid == "fs1" and fs.total_bytes == 1000 * 4096
    assert report.docker is not None and report.docker.root_dir == "/var/lib/docker"
    assert report.project_dirs is not None
    assert report.project_dirs.total_bytes == 20 * 1024
    stat_run, du_run = probe.runs
    # argv lists with fixed in-container paths: no host path reaches a command line.
    assert stat_run[0][:4] == ["stat", "-f", "-c", "%n %i %S %b %f %a"]
    assert stat_run[1] == {
        "/opt/jbrain2": PROJECT_MOUNT,
        "/var/lib/docker": DOCKER_ROOT_MOUNT,
    }
    assert du_run[0] == ["du", "-a", "-x", "-k", "-d", "2", PROJECT_MOUNT]
    assert du_run[1] == {"/opt/jbrain2": PROJECT_MOUNT}
    assert du_run[2] == disk_usage.DU_TIMEOUT_S


def test_report_keeps_a_separate_project_disk_apart() -> None:
    probe = FakeProbe(
        stat_out=(
            f"{PROJECT_MOUNT} fs2 4096 5000 4000 3900\n"
            f"{DOCKER_ROOT_MOUNT} fs1 4096 1000 300 250\n"
        )
    )
    report = _usage(probe).report()
    assert [f.paths for f in report.filesystem] == [
        ["/ (supervisor)", "docker data root /var/lib/docker"],
        ["project /opt/jbrain2"],
    ]


def test_report_without_docker_root_stats_only_the_project() -> None:
    probe = FakeProbe(root=None, stat_out=f"{PROJECT_MOUNT} fs2 4096 5000 4000 3900\n")
    report = _usage(probe).report()
    assert probe.runs[0][1] == {"/opt/jbrain2": PROJECT_MOUNT}
    assert [f.paths for f in report.filesystem] == [
        ["/ (supervisor)"],
        ["project /opt/jbrain2"],
    ]
    assert report.docker is not None and report.docker.root_dir is None


def test_du_helper_failure_returns_the_rest_with_an_error() -> None:
    probe = FakeProbe(stat_out=SAME_DISK_STAT, du_raises=True)
    report = _usage(probe).report()
    assert report.project_dirs is None
    assert report.docker is not None
    assert report.filesystem
    assert report.errors == ["du helper: RuntimeError: image not found"]


def test_du_timeout_and_partial_exit_are_reported() -> None:
    timed_out = _usage(FakeProbe(stat_out=SAME_DISK_STAT, du_exit=None)).report()
    assert timed_out.project_dirs is None
    assert any("timed out" in e for e in timed_out.errors)

    # du prints each dir as it finishes, so a killed run still lists what it got.
    cut = _usage(
        FakeProbe(
            stat_out=SAME_DISK_STAT, du_out=f"5\t{PROJECT_MOUNT}/src\n", du_exit=None
        )
    ).report()
    assert cut.project_dirs is not None and cut.project_dirs.total_bytes is None
    assert [e.path for e in cut.project_dirs.entries] == ["/opt/jbrain2/src"]
    assert any("PARTIAL" in e for e in cut.errors)

    partial = _usage(
        FakeProbe(
            stat_out=SAME_DISK_STAT,
            du_out=f"5\t{PROJECT_MOUNT}/backups\n",
            du_exit=1,
        )
    ).report()
    assert partial.project_dirs is not None
    assert partial.project_dirs.entries[0].path == "/opt/jbrain2/backups"
    assert partial.errors == ["du helper: exit 1 (partial): du: cannot read 'x'"]


def test_df_failure_keeps_filesystem_and_dirs() -> None:
    probe = FakeProbe(
        stat_out=SAME_DISK_STAT, du_out=f"1\t{PROJECT_MOUNT}\n", df_raises=True
    )
    report = _usage(probe).report()
    assert report.docker is None
    assert report.project_dirs is not None
    assert report.errors == ["docker df: RuntimeError: daemon said no"]


def test_stat_helper_short_output_is_an_error() -> None:
    report = _usage(FakeProbe(stat_out="")).report()
    assert [f.paths for f in report.filesystem] == [["/ (supervisor)"]]
    assert any(e.startswith("stat helper: exit 0") for e in report.errors)


def test_report_is_cached_until_ttl_or_refresh() -> None:
    clock = Clock()
    probe = FakeProbe(stat_out=SAME_DISK_STAT)
    usage = _usage(probe, clock)
    first = usage.report()
    assert not first.cached and probe.df_calls == 1

    clock.t += 30
    second = usage.report()
    assert second.cached and second.age_s == 30.0 and probe.df_calls == 1

    forced = usage.report(refresh=True)
    assert not forced.cached and probe.df_calls == 2

    clock.t += disk_usage.CACHE_TTL_S + 1
    assert not usage.report().cached and probe.df_calls == 3


def test_concurrent_refreshes_share_one_build() -> None:
    # No cache yet, so the second refresh must wait — and then take the build that
    # finished after it arrived instead of running a second du.
    clock = Clock()
    probe = FakeProbe(stat_out=SAME_DISK_STAT, gate=threading.Event())
    usage = _usage(probe, clock)
    results: dict[str, Any] = {}

    first = threading.Thread(
        target=lambda: results.setdefault("a", usage.report(refresh=True))
    )
    first.start()
    assert probe.entered.wait(5)
    clock.t += 1  # the second caller arrives mid-build
    second = threading.Thread(
        target=lambda: results.setdefault("b", usage.report(refresh=True))
    )
    second.start()
    clock.t += 1  # the build finishes after it
    assert probe.gate is not None
    probe.gate.set()
    first.join(5)
    second.join(5)
    assert probe.df_calls == 1
    assert not results["a"].cached
    assert results["b"].cached and not results["b"].stale


def test_a_caller_during_a_build_gets_the_last_report_marked_stale() -> None:
    clock = Clock()
    probe = FakeProbe(stat_out=SAME_DISK_STAT)
    usage = _usage(probe, clock)
    usage.report()
    probe.gate = threading.Event()
    probe.entered.clear()
    worker = threading.Thread(target=lambda: usage.report(refresh=True))
    worker.start()
    assert probe.entered.wait(5)
    clock.t += 3
    served = usage.report(refresh=True)  # does not queue behind the build
    assert served.cached and served.stale and served.age_s == 3.0
    probe.gate.set()
    worker.join(5)
    assert probe.df_calls == 2


def _client(probe: FakeProbe | None) -> TestClient:
    disk = _usage(probe) if probe is not None else None
    app = create_app(
        Settings(supervisor_token=TOKEN), FakeGateway([]), watch_api=False, disk=disk
    )
    return TestClient(app)


def test_disk_route_requires_the_token() -> None:
    with _client(FakeProbe()) as client:
        assert client.get("/disk").status_code == 401


def test_disk_route_serves_and_refreshes() -> None:
    probe = FakeProbe(stat_out=SAME_DISK_STAT, du_out=f"1\t{PROJECT_MOUNT}\n")
    with _client(probe) as client:
        body = client.get("/disk", headers=AUTH).json()
        assert body["cached"] is False
        assert body["docker"]["volumes"]["items"][0]["name"] == "jbrain_db_data"
        assert client.get("/disk", headers=AUTH).json()["cached"] is True
        assert client.get("/disk?refresh=1", headers=AUTH).json()["cached"] is False
        assert probe.df_calls == 2


def test_disk_route_without_a_probe_is_503() -> None:
    with _client(None) as client:
        assert client.get("/disk", headers=AUTH).status_code == 503


class _HelperContainer:
    def __init__(
        self, *, hang: bool = False, fail_start: bool = False, fail_logs: bool = False
    ) -> None:
        self.hang = hang
        self.fail_start = fail_start
        self.fail_logs = fail_logs
        self.started = False
        self.killed = False
        self.removed = False

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("no such image")
        self.started = True

    def wait(self, timeout: float) -> dict[str, int]:
        if self.hang:
            raise TimeoutError("read timed out")
        return {"StatusCode": 1}

    def kill(self) -> None:
        self.killed = True

    def logs(self, stdout: bool, stderr: bool) -> bytes:
        if self.fail_logs:
            raise RuntimeError("log driver unreadable")
        return b"12\t/mnt/project\n" if stdout else b"du: denied\n"

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _HelperContainers:
    def __init__(
        self, container: _HelperContainer, leftovers: list[_HelperContainer]
    ) -> None:
        self.container = container
        self.leftovers = leftovers
        self.image = ""
        self.kwargs: dict[str, Any] = {}
        self.list_filters: dict[str, Any] = {}

    def create(self, image: str, **kwargs: Any) -> _HelperContainer:
        # Every leftover is already gone by the time a new helper is created.
        assert all(c.removed for c in self.leftovers)
        self.image, self.kwargs = image, kwargs
        return self.container

    def list(
        self, all: bool = False, filters: dict[str, Any] | None = None
    ) -> list[_HelperContainer]:
        self.list_filters = {"all": all, **(filters or {})}
        return self.leftovers


class _HelperClient:
    def __init__(
        self,
        container: _HelperContainer,
        leftovers: list[_HelperContainer] | None = None,
    ) -> None:
        self.containers = _HelperContainers(container, leftovers or [])


def _gw(client: _HelperClient) -> ComposeDockerGateway:
    return ComposeDockerGateway(cast(Any, client), "jbrain", "/opt/jbrain2")


def test_gateway_helper_is_read_only_offline_and_removed() -> None:
    container = _HelperContainer()
    client = _HelperClient(container)
    run = _gw(client).run_readonly_helper(
        ["du", "-k", PROJECT_MOUNT], {"/opt/jbrain2": PROJECT_MOUNT}, 5.0
    )
    # A non-zero exit still hands back what du printed.
    assert run == HelperRun(
        exit_code=1, stdout="12\t/mnt/project\n", stderr="du: denied\n"
    )
    kw = client.containers.kwargs
    assert client.containers.image == DISK_HELPER_IMAGE
    assert kw["command"] == ["du", "-k", PROJECT_MOUNT]
    assert kw["network_mode"] == "none"
    assert kw["read_only"] is True
    assert kw["cap_drop"] == ["ALL"] and kw["cap_add"] == ["DAC_READ_SEARCH"]
    assert kw["security_opt"] == ["no-new-privileges"]
    assert not kw.get("privileged")
    assert kw["mem_limit"] == "256m" and kw["pids_limit"] == 64
    assert kw["log_config"]["Type"] == "json-file"
    assert "volumes" not in kw and "auto_remove" not in kw
    (mount,) = kw["mounts"]
    assert mount["Source"] == "/opt/jbrain2" and mount["Target"] == PROJECT_MOUNT
    assert mount["ReadOnly"] is True and mount["Type"] == "bind"
    assert mount["BindOptions"] == {"NonRecursive": True}
    assert container.started and container.removed


def test_gateway_helper_kills_a_hung_run() -> None:
    container = _HelperContainer(hang=True)
    run = _gw(_HelperClient(container)).run_readonly_helper(["du"], {}, 0.1)
    assert run.exit_code is None
    assert container.killed and container.removed


def test_gateway_helper_removed_when_start_fails() -> None:
    container = _HelperContainer(fail_start=True)
    with pytest.raises(RuntimeError, match="no such image"):
        _gw(_HelperClient(container)).run_readonly_helper(["du"], {}, 1.0)
    assert container.removed


def test_gateway_helper_removed_when_logs_fail() -> None:
    container = _HelperContainer(fail_logs=True)
    with pytest.raises(RuntimeError, match="log driver"):
        _gw(_HelperClient(container)).run_readonly_helper(["du"], {}, 1.0)
    assert container.removed


def test_gateway_sweeps_leftover_helpers_first() -> None:
    leftovers = [_HelperContainer(), _HelperContainer()]
    client = _HelperClient(_HelperContainer(), leftovers)
    _gw(client).run_readonly_helper(["du"], {}, 1.0)
    assert all(c.removed for c in leftovers)
    assert client.containers.list_filters == {
        "all": True,
        "label": DISK_HELPER_LABEL,
    }
