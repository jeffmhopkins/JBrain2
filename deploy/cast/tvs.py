"""The house's Cast TVs: found by mDNS, each held on an open connection, every status
logged.

The one module that imports pychromecast. Holding a connection per TV (rather than
asking on demand) is what makes the timeline possible: a TV pushes a receiver status
whenever its app, standby or input changes, and a media status whenever playback does,
so the history records the owner's experiments as they happen instead of whenever
somebody next asks.

A cast — the test card, or a notice the api rendered — plays one PNG on the Default
Media Receiver (no registered app, no HTTPS origin) and quits it after a while, but only
if that same cast is still what the TV is showing, so a child who picked up the remote
mid-notice is never interrupted by the quit.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid as uuidlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import pychromecast
import zeroconf
from pychromecast.config import APP_MEDIA_RECEIVER
from pychromecast.controllers.media import MediaStatus, MediaStatusListener
from pychromecast.controllers.receiver import CastStatus, CastStatusListener
from pychromecast.dial import get_cast_type
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    ConnectionStatus,
    ConnectionStatusListener,
)

import gate

TEST_TITLE = "jbrain cast test"


@dataclass
class Tv:
    cc: pychromecast.Chromecast
    history: gate.History = field(default_factory=gate.History)
    connected: bool = False
    found_at: float = field(default_factory=time.time)
    last_test_at: float | None = None
    #: The cast this box last put up, until it is quit or taken down: the only one a
    #: quit may end.
    casting: str | None = None


def local_address_for(host: str) -> str:
    """The box's own address on the route to `host`: what a TV must fetch the card from.
    A UDP connect sends nothing; it only asks the kernel to pick the route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 8009))
        return s.getsockname()[0]


class _Listen(CastStatusListener, MediaStatusListener, ConnectionStatusListener):
    def __init__(self, on_change: Callable[[], object], tv: Tv):
        self._on_change, self._tv = on_change, tv

    def new_cast_status(self, status: CastStatus) -> None:
        self._on_change()

    def new_media_status(self, status: MediaStatus) -> None:
        self._on_change()

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        self._tv.history.note("load_failed", error_code=error_code)

    def new_connection_status(self, status: ConnectionStatus) -> None:
        self._tv.connected = status.status == CONNECTION_STATUS_CONNECTED
        self._tv.history.note("connection", status=status.status)
        self._on_change()


def reading(tv: Tv) -> gate.Reading:
    cc = tv.cc
    st = cc.status
    media = cc.media_controller.status
    return gate.Reading(
        connected=tv.connected,
        has_status=st is not None,
        app_id=st.app_id if st else None,
        display_name=st.display_name if st else None,
        status_text=st.status_text if st else None,
        is_stand_by=st.is_stand_by if st else None,
        is_active_input=st.is_active_input if st else None,
        volume_level=st.volume_level if st else None,
        # The media controller keeps its last status after the app that sent it has
        # gone, so it only counts while the receiver still runs an app that could own
        # it.
        player_state=media.player_state
        if st and st.app_id and media.player_state
        else None,
        media_title=media.title if st and st.app_id else None,
    )


class Fleet:
    def __init__(self) -> None:
        self._tvs: dict[str, Tv] = {}
        self._ignored: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._zconf: zeroconf.Zeroconf | None = None
        self._browser: CastBrowser | None = None

    def start(self) -> None:
        self._zconf = zeroconf.Zeroconf()
        self._browser = CastBrowser(
            SimpleCastListener(add_callback=self._spawn, update_callback=self._spawn),
            self._zconf,
        )
        self._browser.start_discovery()

    def _spawn(self, uuid: uuidlib.UUID, service: str) -> None:
        # Off the zeroconf thread: _found may ask the device over HTTP, and a slow TV
        # must not hold up the discovery of the next one.
        threading.Thread(
            target=self._found, args=(uuid, service), daemon=True, name="cast-found"
        ).start()

    def _found(self, uuid: uuidlib.UUID, _service: str) -> None:
        key = str(uuid)
        with self._lock:
            if key in self._tvs or key in self._ignored or self._browser is None:
                return
            info = self._browser.devices.get(uuid)
        if info is None:
            return
        # Discovery leaves cast_type empty for a model it does not know, which is most
        # Android TVs; the device's own info endpoint says. Outside the lock: it is
        # HTTP.
        if not info.cast_type:
            info = get_cast_type(info, self._zconf)
        # Screens only. A speaker group especially: it can read idle while a TV in it is
        # busy, and a card cast to the group would land on that TV.
        if not gate.is_display(info.cast_type):
            with self._lock:
                self._ignored[key] = {
                    "id": key,
                    "name": info.friendly_name,
                    "model": info.model_name,
                    "cast_type": info.cast_type,
                }
            return
        cc = pychromecast.get_chromecast_from_cast_info(info, self._zconf)
        tv = Tv(cc=cc)
        with self._lock:
            if key in self._tvs:
                return
            self._tvs[key] = tv
        listen = _Listen(lambda: tv.history.record(reading(tv)), tv)
        cc.socket_client.receiver_controller.register_status_listener(listen)
        cc.media_controller.register_status_listener(listen)
        cc.socket_client.register_connection_listener(listen)
        tv.history.note("found", host=info.host, model=info.model_name)
        cc.start()

    def ignored(self) -> list[dict[str, Any]]:
        """Cast devices that are not screens (speakers, groups), listed so a missing TV
        can be told from one the box saw and set aside."""
        with self._lock:
            items = list(self._ignored.values())
        return sorted(items, key=lambda d: str(d["name"] or "").lower())

    def _get(self, key: str) -> Tv:
        with self._lock:
            tv = self._tvs.get(key)
        if tv is None:
            raise KeyError(key)
        return tv

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._tvs.items())
        out = []
        for key, tv in items:
            info = tv.cc.cast_info
            r = reading(tv)
            state, why = gate.verdict(r)
            out.append(
                {
                    "id": key,
                    "name": info.friendly_name,
                    "model": info.model_name,
                    "manufacturer": info.manufacturer,
                    "cast_type": info.cast_type,
                    "host": info.host,
                    "state": state,
                    "why": why,
                    "castable": gate.castable(state),
                    "reading": asdict(r),
                    "found_at": tv.found_at,
                    "last_test_at": tv.last_test_at,
                }
            )
        return sorted(out, key=lambda t: str(t["name"] or "").lower())

    def history(self, key: str, since: float = 0.0) -> list[dict[str, Any]]:
        return self._get(key).history.since(since)

    def host_of(self, key: str) -> str:
        return self._get(key).cc.cast_info.host

    def test(self, key: str, url: str, seconds: int) -> dict[str, Any]:
        """Re-check the gate NOW, cast the card, and quit after `seconds` if it is
        still ours. Raises KeyError for an unknown TV and gate.Refused when the gate
        says no."""
        tv = self._get(key)
        tv.last_test_at = time.time()
        out = self._play(tv, url, gate.clamp_seconds(seconds), TEST_TITLE, "test")
        out.pop("cast_id")
        return {"id": key, **out}

    def cast(self, key: str, url: str, seconds: int, title: str) -> dict[str, Any]:
        """A notice: the same gate, cast and quit as a test, under the notice's title.
        `cast_id` names it for `stop`."""
        out = self._play(
            self._get(key), url, gate.clamp_notice_seconds(seconds), title, "notice"
        )
        return {"id": key, **out}

    def stop(self, key: str, cast_id: str) -> bool:
        """Take a cast down now, if it is still the one on the screen."""
        return self._quit_if_ours(self._get(key), cast_id, "stop")

    def _play(
        self, tv: Tv, url: str, seconds: int, title: str, kind: str
    ) -> dict[str, Any]:
        before = reading(tv)
        state, why = gate.verdict(before)
        if not gate.castable(state):
            tv.history.note(f"{kind}_refused", state=state, why=why)
            raise gate.Refused(state, why)
        cast_id = uuidlib.uuid4().hex
        tv.history.note(
            f"{kind}_cast",
            state_before=state,
            seconds=seconds,
            url=url,
            cast_id=cast_id,
        )
        tv.casting = cast_id
        tv.cc.media_controller.play_media(url, "image/png", title=title)
        threading.Thread(
            target=self._end,
            args=(tv, seconds, cast_id, kind, state),
            daemon=True,
            name=f"cast-{kind}",
        ).start()
        return {
            "state_before": state,
            "why_before": why,
            "seconds": seconds,
            "cast_id": cast_id,
        }

    def _end(
        self, tv: Tv, seconds: int, cast_id: str, kind: str, state_before: str
    ) -> None:
        time.sleep(seconds)
        if not self._quit_if_ours(tv, cast_id, kind, state_before=state_before):
            r = reading(tv)
            # Somebody took the TV over, or a newer cast replaced this one: leave
            # whatever is on now alone.
            tv.history.note(
                f"{kind}_left", app_id=r.app_id, display_name=r.display_name
            )

    def _quit_if_ours(self, tv: Tv, cast_id: str, kind: str, **extra: Any) -> bool:
        if tv.casting != cast_id or reading(tv).app_id != APP_MEDIA_RECEIVER:
            return False
        tv.casting = None
        tv.cc.quit_app()
        tv.history.note(f"{kind}_quit", **extra)
        return True
