"""Container entrypoint: wire real settings and a real docker client."""

import docker

from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.disk_cleanup import DiskCleanup
from supervisor.disk_usage import DiskUsage
from supervisor.gateway import ComposeDockerGateway

# SUPERVISOR_TOKEN is required and comes from the environment at runtime.
settings = Settings()  # pyright: ignore[reportCallIssue]
gateway = ComposeDockerGateway(
    docker.from_env(), settings.compose_project, settings.project_dir
)
disk = DiskUsage(gateway, settings.compose_project, settings.project_dir)
cleanup = DiskCleanup(
    gateway,
    settings.compose_project,
    settings.project_dir,
    on_applied=disk.invalidate,
)
app = create_app(settings, gateway, disk=disk, cleanup=cleanup)
