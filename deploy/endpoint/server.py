"""The `endpoint` sidecar: flash a room-endpoint panel over USB.

The only container that touches the panel, because the device node has to be visible
somewhere and this is the one place worth granting it. What it grants is narrower than
it looks: `/dev` plus cgroup rules for the tty character majors, NOT `privileged`.

WHY IT IS NOT THE `sdr` PATTERN. That sidecar maps `/dev/bus/usb`, which works because
an RTL-SDR is a raw libusb device. The ESP32-S3 presents its native USB as a kernel
CDC-ACM tty at `/dev/ttyACM0`, which does not live under that path at all — and does
not exist until the owner plugs the panel in, so a static compose `devices:` entry
would fail at container start on a box with nothing attached. Hence a `/dev` mount plus
`device_cgroup_rules`, which admits a hotplugged tty.

**Egress-free by topology**, like `sdr`: compose puts it on an `internal: true` network.
It never fetches firmware — the api hands over the images — which is precisely what
keeps a GitHub credential off the box.

**It keeps nothing.** The Wi-Fi password, device token and CA root arrive per request,
are written under a temporary directory, and that directory is removed when the flash
ends either way. There is no state in this process between requests.

Dependencies are pip, unlike `sdr`'s apt-only rule, and the reason that rule does not
apply here is that its reason does not: there is no compiled extension and no ABI
contract to honour. `esptool` and `esp-idf-nvs-partition-gen` are Espressif's own pure
Python, and vendoring either would mean maintaining a copy of a flasher.
"""

from __future__ import annotations

import base64
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import flash
import ports

PORT = int(os.environ.get("ENDPOINT_PORT", "8000"))

# A flash writes roughly a megabyte over a serial link. Generous, but bounded: a request
# that has stopped making progress should end rather than hold the only device forever.
MAX_BODY = 32 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        # The default writes to stderr per request; compose already timestamps container
        # output, so this only adds a second clock.
        return

    def _json(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        if self.path == "/ports":
            # An empty list is an ANSWER, not a failure: "this container cannot see the
            # panel" is the single most useful thing this sidecar can tell an owner who
            # has no terminal, and it must arrive as data rather than as a 500.
            self._json(200, {"ports": ports.as_json(ports.scan())})
            return
        self._json(404, {"error": "no such route"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path != "/flash":
            self._json(404, {"error": "no such route"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"error": "body missing or too large"})
            return

        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            self._json(400, {"error": "body is not JSON"})
            return

        try:
            port, images, values, offset, size, erase = _request(body)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return

        # Streamed as chunked plain text rather than returned as JSON: a flash takes tens
        # of seconds and the owner is watching a PWA with no other signal that it is
        # alive. The status line is already sent by the time anything can fail, so a
        # failure is reported as a final line in the stream — the api parses the last
        # line rather than the status code.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for line in flash.flash(port, images, values, offset, size, erase=erase):
                self.wfile.write(f"{line}\n".encode())
                self.wfile.flush()
        except flash.FlashError as exc:
            self.wfile.write(f"FAILED: {exc}\n".encode())
        except Exception:  # noqa: BLE001 - the stream must carry the reason, not drop
            self.wfile.write(f"FAILED: {traceback.format_exc(limit=3)}\n".encode())


def _request(
    body: dict[str, Any],
) -> tuple[str, list[tuple[str, bytes]], dict[str, str], str, str, bool]:
    """Validate a flash request, or say exactly what is wrong with it.

    The port is checked against what is actually present rather than taken on trust: this
    process runs as root with /dev mounted, so an arbitrary caller-supplied path is the
    one input here worth refusing by construction.
    """
    port = str(body.get("port") or "")
    known = {p.device for p in ports.scan()}
    if port not in known:
        raise ValueError(f"unknown port {port!r}; visible: {sorted(known) or 'none'}")

    raw_images = body.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise ValueError("images must be a non-empty list")
    images: list[tuple[str, bytes]] = []
    for item in raw_images:
        if not isinstance(item, dict):
            raise ValueError("each image must be an object")
        offset = str(item.get("offset") or "")
        try:
            int(offset, 16)
        except ValueError:
            raise ValueError(f"image offset {offset!r} is not hex") from None
        try:
            images.append((offset, base64.b64decode(str(item.get("b64") or ""), validate=True)))
        except Exception:
            raise ValueError(f"image at {offset} is not valid base64") from None

    raw_values = body.get("nvs") or {}
    if not isinstance(raw_values, dict):
        raise ValueError("nvs must be an object")
    values = {str(k): str(v) for k, v in raw_values.items()}

    nvs_offset = str(body.get("nvs_offset") or "0x9000")
    nvs_size = str(body.get("nvs_size") or "0x6000")
    return port, images, values, nvs_offset, nvs_size, bool(body.get("erase"))


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
