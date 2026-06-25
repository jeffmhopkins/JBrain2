"""Container entrypoint: wire real settings, the SDK adapter, and git workspaces."""

from jcode_ctl.agent import ClaudeCodeAgent
from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import GitWorkspace

# JCODE_TOKEN is required and comes from the environment at runtime.
settings = Settings()  # pyright: ignore[reportCallIssue]
sessions = SessionManager(
    ClaudeCodeAgent(settings.model),
    GitWorkspace(settings.egress_allowlist),
    settings.workspace_root,
    max_sessions=settings.max_sessions,
)
app = create_app(settings, sessions)
