"""Restart the API when it stops answering — the one recovery path that does not
run through the API.

Every operator route the owner has goes THROUGH the api container: the PWA's restart
control posts to `/api/ops/restart`, and the debug console is mounted there too. So the
single component whose failure most needs recovering is the one whose recovery path dies
with it. `restart: unless-stopped` does not cover this: it acts when the process EXITS,
and the failure seen in practice was a process still running and still holding its
socket
open while serving nothing — the API's own metrics sampler recorded a 328-second gap
while
Cloudflare answered 530. Docker saw a healthy container the whole time.

The supervisor is the right place for the fix. It already holds the docker socket, it is
already `restart: unless-stopped`, and it is on the internal network — so this adds NO
new
externally reachable surface, no new credential, and nothing for the browser to hold. It
simply notices that the API stopped answering and restarts it.

Three guards keep it from being worse than the problem:

- **Consecutive** failures, not one. A single slow probe during a model load is normal.
- **Never during a one-shot.** An update stops and recreates the api container on
purpose;
  restarting it mid-update would fight the updater and could leave the box half-
  migrated.
- **A cooldown.** If the API is crash-looping, restarting it every minute masks the
fault
  and adds load to a box that is already unwell. One intervention per cooldown, then
  leave
  it alone and let the logs tell the story.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from supervisor.gateway import DockerGateway

log = logging.getLogger(__name__)

# The api container's own liveness endpoint, on the internal network. Deliberately the
# same one the compose healthcheck uses: it touches no database, so a failure means the
# event loop is not serving rather than that a dependency is slow.
DEFAULT_PROBE_URL = "http://api:8000/api/healthz"

PROBE_INTERVAL_SECONDS = 10.0
# Shorter than the interval, so a hung probe cannot stack up behind the next one.
PROBE_TIMEOUT_SECONDS = 5.0
# ~60s of silence before intervening. Long enough that a heavy model load or a slow
# request does not trip it, far shorter than the multi-minute wedge this exists for.
FAILURES_BEFORE_RESTART = 6
# One intervention per five minutes. A crash-loop must surface as a crash-loop.
RESTART_COOLDOWN_SECONDS = 300.0

API_SERVICE = "api"


def http_ok(url: str, timeout: float) -> bool:
    """True when the endpoint answers. Blocking on purpose — the caller runs it in a
    thread, because a probe of a wedged server is exactly the call that hangs."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def _update_running(gateway: DockerGateway) -> bool:
    """Whether a one-shot (update, restore, provision…) is in flight.

    The updater is NOT an `oneshot_status` kind. It carries its own label
    (`jbrain.updater=1`) and is read through `update_status`, while export/import/reset/
    provision/rebuild carry `jbrain.oneshot=<kind>`. Asking `oneshot_status("update")`
    silently returns "none" — no error, just a quiet no — which would have left the api
    unguarded during the exact operation this check exists to protect: a restart landing
    mid-migration.

    provision/rebuild carry `jbrain.oneshot=<kind>`. Asking `oneshot_status("update")`
    DURING an update, so an unreadable state must not read as permission to act."""
    try:
        if gateway.update_status(0).state == "running":
            return True
    except Exception:
        log.warning("watchdog: could not read updater status; assuming busy")
        return True
    for kind in ("export", "import", "reset", "provision", "rebuild"):
        try:
            if gateway.oneshot_status(kind, 0).state == "running":
                return True
        except Exception:
            log.warning("watchdog: could not read %s status; assuming busy", kind)
            return True
    return False


async def watch_api(
    gateway: DockerGateway,
    *,
    url: str = DEFAULT_PROBE_URL,
    service: str = API_SERVICE,
    interval: float = PROBE_INTERVAL_SECONDS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    failures_before_restart: int = FAILURES_BEFORE_RESTART,
    cooldown: float = RESTART_COOLDOWN_SECONDS,
    probe: Callable[[str, float], bool] = http_ok,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Poll the API forever; restart it when it goes quiet for long enough.

    Never raises: a watchdog that dies on an unexpected error is worse than no watchdog,
    because nothing is left watching and nothing says so."""
    consecutive = 0
    last_restart: float | None = None

    while True:
        try:
            alive = await asyncio.to_thread(probe, url, timeout)
            consecutive = 0 if alive else consecutive + 1

            if consecutive >= failures_before_restart:
                if _update_running(gateway):
                    # An update stops the api container on purpose. Reset the counter so
                    # the run-up doesn't carry over and fire the moment the update ends.
                    log.info("watchdog: api down but a one-shot is running; leaving it")
                    consecutive = 0
                elif last_restart is not None and clock() - last_restart < cooldown:
                    log.warning(
                        "watchdog: api still down, but within the restart cooldown"
                    )
                else:
                    log.error(
                        "watchdog: api has failed %d consecutive probes; restarting it",
                        consecutive,
                    )
                    await asyncio.to_thread(gateway.restart, service)
                    last_restart = clock()
                    consecutive = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watchdog: probe cycle failed; continuing")

        await asyncio.sleep(interval)


def start(gateway: DockerGateway) -> asyncio.Task[None]:
    """Launch the watchdog as a background task."""
    return asyncio.create_task(watch_api(gateway))


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
