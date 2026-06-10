"""Container entrypoint: wire real settings and a real docker client."""

import docker

from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.gateway import ComposeDockerGateway

# SUPERVISOR_TOKEN is required and comes from the environment at runtime.
settings = Settings()  # pyright: ignore[reportCallIssue]
gateway = ComposeDockerGateway(docker.from_env(), settings.compose_project)
app = create_app(settings, gateway)
