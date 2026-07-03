"""Container entrypoint: wire real settings, the SDK adapter, and git workspaces."""

import logging

from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import GitWorkspace

# JCODE_TOKEN is required and comes from the environment at runtime.
settings = Settings()  # pyright: ignore[reportCallIssue]

# Configure logging before the app is built so every session/terminal/lifecycle line
# lands on stdout (→ docker logs → the owner debug console). Level is JCODE_LOG_LEVEL,
# or DEBUG whenever debug access is on (effective_log_level). basicConfig installs the
# stdout handler; we also pin the package logger's level so it isn't left at WARNING
# under uvicorn's own logging setup.
_level = getattr(logging, settings.effective_log_level.upper(), logging.INFO)
logging.basicConfig(
    level=_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("jcode_ctl").setLevel(_level)
if settings.debug_access_enabled:
    logging.getLogger("jcode_ctl").info("debug access on — jcode logging at DEBUG")
sessions = SessionManager(
    GitWorkspace(settings.egress_allowlist),
    settings.workspace_root,
    home_root=settings.home_root,
    max_sessions=settings.max_sessions,
)
# Host-served per-session preview (docs/archive/JCODE_PREVIEW_HOST_PLAN.md) — the sole
# preview
# path since the Wave P5b cutover retired the per-session cloudflared quick-tunnel. The
# allocator is enabled only when a base host is configured AND preview is on; with no
# base host (or preview off) it fail-closes (.enabled is False) and serves no preview.
host_preview = HostPreviewManager(
    base_host=settings.preview_base_host if settings.preview_enabled else "",
    port_low=settings.preview_port_low,
    port_high=settings.preview_port_high,
)
app = create_app(settings, sessions, host_preview)
