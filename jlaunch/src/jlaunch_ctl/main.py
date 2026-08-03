"""Container entrypoint: wire real settings and the job manager."""

import logging

from jlaunch_ctl.app import create_app
from jlaunch_ctl.config import Settings
from jlaunch_ctl.jobs import JobManager
from jlaunch_ctl.terminal import TerminalRegistry

# JLAUNCH_TOKEN is required and comes from the environment at runtime.
settings = Settings()  # pyright: ignore[reportCallIssue]

_level = getattr(logging, settings.effective_log_level.upper(), logging.INFO)
logging.basicConfig(
    level=_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("jlaunch_ctl").setLevel(_level)

registry = TerminalRegistry()
jobs = JobManager(
    settings.workspace_root,
    settings.state_root,
    settings.artifact_root,
    registry,
    max_concurrent_jobs=settings.max_concurrent_jobs,
)
app = create_app(settings, jobs, registry)
