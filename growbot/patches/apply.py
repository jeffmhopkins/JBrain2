"""Build growbot/site/ and growbot/firmware/ from a pinned upstream checkout plus our patches.

upstream.json pins the britcruise9/GrowBot commit and every third-party asset by SHA-256.
Each patch is an exact replacement with an expected count, so an upstream change that moves
the text fails the build loudly instead of shipping a half-patched app (docs/plans/
GROWBOT_PLAN.md §2). The output is committed: the box serves it straight from the bind mount.

    python3 growbot/patches/apply.py --upstream /path/to/GrowBot [--vendor-cache DIR]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROWBOT = HERE.parent

# Upstream files we do not serve: the Custom-agent tab points the page at arbitrary endpoints
# (the CSP would block it anyway), face-original is a superseded copy, the feedback widget
# posts to upstream's inbox, and meSpeak is the in-page voice the box replaces with Kokoro.
EXCLUDED = {
    "README.md",
    "feedback-widget.js",
    "tester/custom.html",
    "tester/face-original.html",
}

FIRMWARE_FILES = (
    "relay_chip.py",
    "act_engine.py",
    "PicoRobotics.py",
    "hello_legs.py",
    "wifi_store.py",
    "wifi_recovery.py",
    "secrets.example.py",
)

FIRMWARE_SETTINGS = """
# The box (growbot/): dial the box's relay instead of upstream's Cloudflare one. secrets.py
# names the box by LAN IP because MicroPython has no mDNS resolver, and sends jbrain.local as
# the TLS server name so the box's Caddy site matches. The key is the one the box minted for
# this robot; without it the relay drops the socket.
try:
    import secrets as _box
except ImportError:
    _box = None
HOST = getattr(_box, "GROWBOT_HOST", HOST)
PORT = getattr(_box, "GROWBOT_PORT", 8444)
SNI = getattr(_box, "GROWBOT_SNI", "jbrain.local")
ROBOT_KEY = getattr(_box, "GROWBOT_KEY", "")
"""

SECRETS_EXAMPLE_TAIL = """
# The box: where the robot dials, and its key (PWA flashing writes these for you).
# GROWBOT_HOST = "192.168.1.20"   # the box's LAN IP
# GROWBOT_PORT = 8444
# GROWBOT_SNI = "jbrain.local"
# GROWBOT_KEY = "the key the box showed when it registered this robot"
"""

VOICE_PICKER_OLD = (
    '<option value="robot">default growbot voice</option>'
    '<option value="device">device voice</option>'
    '<option value="fish">🐟 fish voice</option>'
)

# (file, old, new, expected count). Every one is the minimum change for one plan item.
PATCHES: list[tuple[str, str, str, int]] = [
    # Relay: the box serves the relay on the page's own origin.
    (
        "relay-transport.js",
        "export const RELAY_DEFAULT = 'wss://growbot-relay.growbot.workers.dev';",
        "export const RELAY_DEFAULT = (location.protocol === 'https:' ? 'wss://' : 'ws://')"
        " + location.host;",
        1,
    ),
    # Speech (§2a): the box's speech shim runs before any page script.
    (
        "v3/next.html",
        '<!doctype html><html lang="en"><head>',
        '<!doctype html><html lang="en"><head><script src="/growbot-box.js"></script>',
        1,
    ),
    *[
        (
            page,
            "<!DOCTYPE html><html><head><meta charset=utf-8>",
            "<!DOCTYPE html><html><head><meta charset=utf-8>"
            '<script src="/growbot-box.js"></script>',
            1,
        )
        for page in (
            "tester/index.html",
            "tester/motor.html",
            "tester/policy.html",
            "tester/face.html",
        )
    ],
    (
        "v3/next.html",
        '<script src="/v2/mespeak.js?v=0624c" onerror="window.__loadErr='
        "(window.__loadErr||'')+'mespeak.js '\"></script>",
        "",
        1,
    ),
    (
        "v3/next.html",
        'var vtEngine="robot", vtUri=null, vtFish={key:"",voice:"",model:"s2.1-pro-free"};',
        'var vtEngine="fish", vtUri=null, vtFish={key:"box",voice:"",model:"kokoro"};',
        1,
    ),
    (
        "v3/next.html",
        'vtEngine=(_vt.engine==="device"||_vt.engine==="fish")?_vt.engine:"robot";',
        'vtEngine="fish";',
        1,
    ),
    (
        "v3/next.html",
        'vtFish.key=String(_vt.fish.key||"");',
        'vtFish.key=String(_vt.fish.key||"box");',
        1,
    ),
    (
        "v3/next.html",
        'vtEngine=(this.value==="device"||this.value==="fish")?this.value:"robot";',
        'vtEngine="fish";',
        1,
    ),
    ("v3/next.html", VOICE_PICKER_OLD, '<option value="fish">box voice</option>', 1),
    # No third-party origins: Google Fonts (the pixel face falls back to the system monospace)
    # and the feedback widget.
    (
        "v3/next.html",
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>',
        "",
        1,
    ),
    (
        "v3/next.html",
        '<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap"'
        ' rel="stylesheet"/>',
        "",
        1,
    ),
    ("v3/next.html", '<script src="/feedback-widget.js" data-surface="v3"></script>', "", 1),
    (
        "tester/index.html",
        '<script src="/feedback-widget.js" data-surface="controller"></script>',
        "",
        1,
    ),
    (
        "tester/face.html",
        "const V='https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.18';",
        "const V='/vendor/mediapipe/tasks-vision-0.10.18';",
        1,
    ),
    (
        "tester/face.html",
        "modelAssetPath:'https://storage.googleapis.com/mediapipe-models/face_detector/"
        "blaze_face_short_range/float16/1/blaze_face_short_range.tflite'",
        "modelAssetPath:'/vendor/mediapipe/models/blaze_face_short_range-float16-1.tflite'",
        1,
    ),
]

FIRMWARE_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "relay_chip.py",
        'PATH = "/d/" + DEVID\n',
        'PATH = "/d/" + DEVID\n' + FIRMWARE_SETTINGS,
        1,
    ),
    ("relay_chip.py", "socket.getaddrinfo(HOST, 443)", "socket.getaddrinfo(HOST, PORT)", 1),
    (
        "relay_chip.py",
        "ssl.wrap_socket(raw, server_hostname=HOST)  # SNI required by Cloudflare",
        "ssl.wrap_socket(raw, server_hostname=SNI)",
        1,
    ),
    ("relay_chip.py", "% (PATH, HOST, key)", "% (PATH, SNI, key)", 1),
    (
        "relay_chip.py",
        'return json.dumps({"t": "hello", "id": DEVID, "caps": ["hb"],',
        'return json.dumps({"t": "hello", "id": DEVID, "key": ROBOT_KEY, "caps": ["hb"],',
        1,
    ),
]

_SCRIPT = re.compile(r"<script(\s[^>]*)?>(.*?)</script>", re.S | re.I)
_HTML_HANDLER = re.compile(r"\son[a-z]+=\"([^\"]*)\"", re.I)
# Handlers written into innerHTML from a JS string: the attribute's value is the string's
# text after JS unescaping, which is what the browser hashes.
_JS_HANDLER = re.compile(r"\son[a-z]+=\"((?:[^\"\\]|\\.)*)\"", re.I)


class PatchError(Exception):
    pass


def _sha(data: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def _js_unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def csp_hashes(markup: str) -> tuple[set[str], bool]:
    """The CSP hashes one HTML page needs, and whether it carries inline handlers."""
    hashes: set[str] = set()
    handlers = False
    outside = _SCRIPT.sub(" ", markup)
    for value in _HTML_HANDLER.findall(outside):
        hashes.add(_sha(html.unescape(value).encode()))
        handlers = True
    for match in _SCRIPT.finditer(markup):
        attrs, body = match.group(1) or "", match.group(2)
        tag = "<script" + attrs + ">"
        for value in _HTML_HANDLER.findall(tag):
            hashes.add(_sha(html.unescape(value).encode()))
            handlers = True
        if re.search(r"\ssrc\s*=", attrs, re.I) is None:
            hashes.add(_sha(body.encode()))
            for value in _JS_HANDLER.findall(body):
                hashes.add(_sha(_js_unescape(value).encode()))
                handlers = True
    return hashes, handlers


def _patch(root: Path, patches: list[tuple[str, str, str, int]]) -> None:
    for rel, old, new, expected in patches:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        found = text.count(old)
        if found != expected:
            raise PatchError(f"{rel}: expected {expected} of {old[:60]!r}, found {found}")
        path.write_text(text.replace(old, new), encoding="utf-8")


def _fetch(entry: dict[str, str], cache: Path | None) -> bytes:
    cached = cache / entry["path"] if cache else None
    if cached and cached.exists():
        data = cached.read_bytes()
    else:
        with urllib.request.urlopen(entry["url"], timeout=60) as resp:  # noqa: S310 - pinned URL
            data = resp.read()
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["sha256"]:
        raise PatchError(f"{entry['path']}: sha256 {digest} != pinned {entry['sha256']}")
    return data


def build(upstream: Path, out: Path, cache: Path | None) -> None:
    pin = json.loads((HERE / "upstream.json").read_text())
    app = upstream / "app"
    if not app.is_dir():
        raise PatchError(f"{upstream} has no app/ (expected a GrowBot checkout)")

    site = out / "site"
    if site.exists():
        shutil.rmtree(site)
    for src in sorted(p for p in app.rglob("*") if p.is_file()):
        rel = src.relative_to(app).as_posix()
        if rel in EXCLUDED:
            continue
        dest = site / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    _patch(site, PATCHES)
    shutil.copyfile(HERE / "growbot-box.js", site / "growbot-box.js")
    for entry in pin["vendor"]:
        dest = site / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_fetch(entry, cache))

    hashes: set[str] = set()
    handlers = False
    for page in sorted(site.rglob("*.html")):
        page_hashes, page_handlers = csp_hashes(page.read_text(encoding="utf-8"))
        hashes |= page_hashes
        handlers = handlers or page_handlers
    csp = {"script_hashes": sorted(hashes), "unsafe_hashes": handlers}
    (site / "csp.json").write_text(json.dumps(csp, indent=1) + "\n")

    firmware = out / "firmware"
    if firmware.exists():
        shutil.rmtree(firmware)
    firmware.mkdir(parents=True)
    for name in FIRMWARE_FILES:
        shutil.copyfile(upstream / "firmware" / name, firmware / name)
    _patch(firmware, FIRMWARE_PATCHES)
    with (firmware / "secrets.example.py").open("a", encoding="utf-8") as f:
        f.write(SECRETS_EXAMPLE_TAIL)

    shutil.copyfile(upstream / "LICENSE", out / "LICENSE")
    (out / "UPSTREAM").write_text(f"{pin['repo']} {pin['commit']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--vendor-cache", type=Path)
    parser.add_argument("--out", type=Path, default=GROWBOT)
    args = parser.parse_args(argv)
    try:
        build(args.upstream, args.out, args.vendor_cache)
    except PatchError as e:
        print(f"apply.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
