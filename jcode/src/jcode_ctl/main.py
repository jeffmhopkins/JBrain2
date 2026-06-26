"""Container entrypoint: wire real settings, the SDK adapter, and git workspaces."""

import logging

from jcode_ctl.agent import ClaudeCodeAgent
from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.preview import CloudflaredTunnel, PreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import GitWorkspace

# JCODE_TOKEN is required and comes from the environment at runtime.
settings = Settings()  # pyright: ignore[reportCallIssue]

# Configure logging before the app is built so every turn/agent/lifecycle line lands on
# stdout (→ docker logs → the owner debug console). Level is JCODE_LOG_LEVEL (DEBUG adds
# every SDK message). basicConfig installs the stdout handler; we also pin the package
# logger's level so it isn't left at WARNING under uvicorn's own logging setup.
_level = getattr(logging, settings.log_level.upper(), logging.INFO)
logging.basicConfig(
    level=_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("jcode_ctl").setLevel(_level)
sessions = SessionManager(
    ClaudeCodeAgent(settings.model),
    GitWorkspace(settings.egress_allowlist),
    settings.workspace_root,
    max_sessions=settings.max_sessions,
)
preview = PreviewManager(
    CloudflaredTunnel,
    enabled=settings.preview_enabled,
    default_port=settings.preview_default_port,
)
app = create_app(settings, sessions, preview)
