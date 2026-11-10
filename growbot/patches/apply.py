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
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROWBOT = HERE.parent

# Upstream files we do not serve: the Custom-agent tab points the page at arbitrary endpoints
# (the CSP would block it anyway), face-original is a superseded copy, and the feedback widget
# posts to upstream's inbox. meSpeak, the in-page voice Kokoro replaces, is not in the repo at
# all (growbot.dev serves it), so dropping its script tag is the whole of removing it.
EXCLUDED = {
    "README.md",
    "feedback-widget.js",
    "tester/custom.html",
    "tester/face-original.html",
}

# What PWA flashing writes to a robot (the api's growbot/provision.py FILE_MAP), hashed into
# firmware/SHA256SUMS with the MicroPython UF2 so the api refuses a half-updated checkout.
FLASHED_FILES = (
    "relay_chip.py",
    "act_engine.py",
    "PicoRobotics.py",
    "wifi_store.py",
    "wifi_recovery.py",
)

PWA_FILES = (
    "manifest.webmanifest",
    "sw.js",
    "setup.html",
    "setup.js",
    "growbot-192.png",
    "growbot-512.png",
)

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
HOST = getattr(_box, "GROWBOT_HOST", None)
if not HOST:
    # Dialing upstream's hostname on the box's port would fail forever and quietly; say why.
    raise RuntimeError("secrets.py has no GROWBOT_HOST: reflash this robot from JBrain")
PORT = getattr(_box, "GROWBOT_PORT", 8444)
SNI = getattr(_box, "GROWBOT_SNI", "jbrain.local")
ROBOT_KEY = getattr(_box, "GROWBOT_KEY", "")
# The box's root CA, DER as a base64 string: wifi_store's restricted parser reads secrets.py
# and accepts no b"..." literal. With it the robot refuses an impostor before sending its
# key; without it (a hand-edited secrets.py) it connects unverified, as upstream always did.
_ca = getattr(_box, "GROWBOT_CA", None)
BOX_CA = binascii.a2b_base64(_ca) if _ca else None


def _wrap(raw):
    if not BOX_CA:
        return ssl.wrap_socket(raw, server_hostname=SNI)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=BOX_CA)
    return ctx.wrap_socket(raw, server_hostname=SNI)


# The clock. mbedTLS checks a certificate's dates against the RTC, which starts in the past on
# a fresh boot, and the box's leaf certificates live hours, so a verified dial needs the real
# time first. The box answers SNTP on GROWBOT_NTP_PORT (growbot/server/serve.py); the Wi-Fi
# gateway's NTP is the fallback. rp2's epoch is 1970 (other ports' 2000): gmtime(0) says which.
NTP_PORT = getattr(_box, "GROWBOT_NTP_PORT", 8123)
CLOCK_MIN_YEAR = 2026
_NTP_DELTA = 2208988800 if time.gmtime(0)[0] == 1970 else 3155673600


def _clock_ok():
    return time.gmtime()[0] >= CLOCK_MIN_YEAR


def _sntp(host, port):
    import struct
    query = bytearray(48)
    query[0] = 0x23  # LI 0, version 4, mode 3 (client)
    # A random transmit stamp the box echoes back: a LAN host racing the reply cannot know it,
    # so it cannot set the clock to when an old certificate was still valid.
    nonce = os.urandom(8)
    query[40:48] = nonce
    addr = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(1)
        s.sendto(query, addr)
        msg = s.recv(48)
    finally:
        s.close()
    if len(msg) < 48 or (msg[0] & 7) != 4 or not msg[1] or msg[24:32] != nonce:
        raise OSError("bad sntp reply")
    secs = struct.unpack("!I", msg[40:44])[0]
    if secs < 3913056000:  # before 2024: the 32-bit NTP era rolled over (2036)
        secs += 0x100000000
    return secs - _NTP_DELTA


def _set_clock(secs):
    tm = time.gmtime(secs)
    # RTC.datetime takes (year, month, day, weekday, hours, minutes, seconds, subseconds);
    # the weekday is set as ntptime.settime sets it.
    machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))


def sync_clock():
    if _clock_ok():
        return True
    for _ in range(3):
        feed()
        try:
            _set_clock(_sntp(HOST, NTP_PORT))
        except Exception as e:
            print("clock: box sntp", e)
        if _clock_ok():
            print("clock: set from the box")
            return True
    try:
        import ntptime
        ntptime.host = wlan.ifconfig()[2]
        ntptime.timeout = 1
        feed()
        ntptime.settime()
    except Exception as e:
        print("clock: gateway ntp", e)
    return _clock_ok()


def _need_clock():
    # Without the CA there is nothing to check dates against, as upstream always dialled.
    if BOX_CA and not sync_clock():
        print("handshake: FAIL clock")
        raise OSError("the clock is not set")
"""

SECRETS_EXAMPLE_TAIL = """
# The box: where the robot dials, and its key (PWA flashing writes these for you).
# GROWBOT_HOST = "192.168.1.20"   # the box's LAN IP
# GROWBOT_PORT = 8444
# GROWBOT_SNI = "jbrain.local"
# GROWBOT_KEY = "the key the box showed when it registered this robot"
# GROWBOT_CA = "MIIB..."   # the box's root certificate: DER, base64, one line
# GROWBOT_NTP_PORT = 8123   # the box's SNTP, to set the clock before checking its certificate
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
        '<!doctype html><html lang="en"><head><script src="/growbot-box.js"></script>'
        '<link rel="manifest" href="/manifest.webmanifest">',
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
    ),  # Speech (§2a), the cloud-voice settings: no key, no fish model, and an honest toast.
    (
        "v3/next.html",
        'vtFish.model=String(_vt.fish.model||"s2.1-pro-free");',
        'vtFish.model="kokoro";',
        1,
    ),
    (
        "v3/next.html",
        'fr.style.display=(vtEngine==="fish")?"block":"none"',
        'fr.style.display="none"',
        1,
    ),
    (
        "v3/next.html",
        "fishToast('fish voice unavailable (check key or credits),"
        " using the default growbot voice')",
        "fishToast('the box voice is unavailable right now')",
        2,
    ),
    # The Custom-agent tab is not served, so its button goes too.
    (
        "tester/index.html",
        '<button class="big exp" id=pkcustom>',
        '<button class="big exp" id=pkcustom hidden>',
        1,
    ),
    # The face (docs/mocks/growbot-face/): growbot-face.js draws the creature at full
    # resolution over upstream's pixel sky, from the state upstream drew its own eyes from.
    # Upstream's 12 px eyes and dream dots are skipped only while that face is on, so
    # ?face=pixel, or a throw in the new face, gives back exactly upstream's.
    (
        "v3/next.html",
        '<head><script src="/growbot-box.js"></script>',
        '<head><script src="/growbot-box.js"></script><script src="/growbot-face.js"></script>',
        1,
    ),
    (
        "v3/next.html",
        "function drawFace(t){ scheduleFace();",
        "function drawFace(t){ scheduleFace();"
        " var _gbF=window.gbFace&&window.gbFace.on()?window.gbFace:null;",
        1,
    ),
    (
        "v3/next.html",
        "  for(side=-1;side<=1;side+=2){ var ex=Math.round(PW/2+side*gap-eyeW/2)+gx,",
        "  if(!_gbF) for(side=-1;side<=1;side+=2){ var ex=Math.round(PW/2+side*gap-eyeW/2)+gx,",
        1,
    ),
    (
        "v3/next.html",
        "  if(dreaming){ pg.fillStyle=col; pg.fillRect(PW/2+22,cyE+8,3,3);",
        "  if(dreaming&&!_gbF){ pg.fillStyle=col; pg.fillRect(PW/2+22,cyE+8,3,3);",
        1,
    ),
    (
        "v3/next.html",
        "  cx.clearRect(0,0,W,H); cx.imageSmoothingEnabled=false;"
        " cx.drawImage(px,0,0,PW,PH,0,0,W,H); }",
        "  cx.clearRect(0,0,W,H); cx.imageSmoothingEnabled=false;"
        " cx.drawImage(px,0,0,PW,PH,0,0,W,H);"
        " if(_gbF) _gbF.draw(cx,W,H,{t:t,emotion:emotion,eye:P.eye,smile:_smileR,"
        "energy:moodCur.e,blink:t<blinkUntil,paused:paused,dreaming:dreaming,think:_think,"
        "orbit:_oa,looking:_looking,gx:gx2/3,gy:gy2/2,touchT:_lt?_lt.t:0,shake:shakeCur,"
        "talking:Date.now()<speakingUntil,glow:col}); }",
        1,
    ),
    # Recolouring, as on the panels: a `coat` verb beside `sky`, read with upstream's own
    # colour words (_hueOf), and logged like the sky so the diary shows when it changed.
    (
        "v3/prompts.js",
        "    '\"sky\":\"OMIT usually",
        "    '\"coat\":\"OMIT usually — a color word or hue number 0-360 to recolor YOURSELF"
        " (your head, crest and neck, the color people mean when they ask you to turn blue or"
        " be gold); it stays until asked again, and the word default goes back to your own"
        " steel blue\",' +\n"
        "    '\"sky\":\"OMIT usually",
        1,
    ),
    (
        "v3/next.html",
        '      if(typeof o.sky!=="undefined") setSky(o.sky);',
        '      if(typeof o.sky!=="undefined") setSky(o.sky);'
        ' if(typeof o.coat!=="undefined"&&window.gbFace&&window.gbFace.setColour(o.coat,'
        '_hueOf(o.coat))) logEv("coat",{c:window.gbFace.colour()});',
        1,
    ),
]

# Visual memories follow the persona (wave G4d; growbot-box.js does the sync).
VISUAL_MEMORY_PATCHES: list[tuple[str, str, str, int]] = [
    # Each persona's saved views are its own namespace on a shared phone.
    (
        "v3/visual-memory.js",
        'function vmNs(){ try{ return localStorage.getItem("pb2_sid")||',
        'function vmNs(){ try{ return window.__gbBoxVmNs||localStorage.getItem("pb2_sid")||',
        1,
    ),
    # The store opens only after the box's records are merged in.
    (
        "v3/visual-memory.js",
        "function vmOpen(){ return new Promise(function(res,rej){ if(_vmDb) return res(_vmDb);",
        "function vmOpen(){ var w=window.gbBoxMemReady; return w&&!_vmDb?"
        "w.then(vmOpenNow,vmOpenNow):vmOpenNow(); }\n"
        "function vmOpenNow(){ return new Promise(function(res,rej){ if(_vmDb) return res(_vmDb);",
        1,
    ),
    # Every write the creature makes to it is sent to the box.
    (
        "v3/visual-memory.js",
        "tx.oncomplete=function(){ done(); res(",
        'tx.oncomplete=function(){ done(); if(mode==="readwrite"&&'
        'typeof window.gbBoxMemChanged==="function"){ try{ window.gbBoxMemChanged(); }'
        "catch(e){} } res(",
        1,
    ),
    # A phone that cached the old file loads this one.
    (
        "v3/next.html",
        '<script src="/v3/visual-memory.js?v=1005-mem"></script>',
        '<script src="/v3/visual-memory.js?v=1005-mem-box"></script>',
        1,
    ),
]
PATCHES += VISUAL_MEMORY_PATCHES

FIRMWARE_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "relay_chip.py",
        'PATH = "/d/" + DEVID\n',
        'PATH = "/d/" + DEVID\n' + FIRMWARE_SETTINGS,
        1,
    ),
    ("relay_chip.py", "socket.getaddrinfo(HOST, 443)", "socket.getaddrinfo(HOST, PORT)", 1),
    # The clock comes before the dial, so a verified handshake never fails on dates.
    ("relay_chip.py", "def ws_open():\n    feed()\n", "def ws_open():\n    feed()\n    _need_clock()\n", 1),
    (
        "relay_chip.py",
        "ssl.wrap_socket(raw, server_hostname=HOST)  # SNI required by Cloudflare",
        "_wrap(raw)",
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


def _pinned_commit(upstream: Path, commit: str) -> None:
    head = upstream / ".git" / "HEAD"
    if not head.exists():
        return
    found = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if found != commit:
        raise PatchError(f"{upstream} is at {found or 'unknown'}, the pin is {commit}")


def _unsupported_handlers(markup: str) -> list[str]:
    """Inline-handler shapes csp_hashes cannot hash: they would be silently CSP-blocked."""
    found = []
    if re.search(r"setAttribute\(\s*[\"']on[a-z]+", markup, re.I):
        found.append("setAttribute('on…')")
    if re.search(r'\son[a-z]+=\\"', markup, re.I):
        found.append('on…=\\" inside a double-quoted string')
    return found


def _stage(upstream: Path, stage: Path, pin: dict, cache: Path | None) -> None:
    app = upstream / "app"
    site = stage / "site"
    for src in sorted(app.rglob("*")):
        if src.is_symlink() or not src.is_file():
            continue
        rel = src.relative_to(app).as_posix()
        if rel in EXCLUDED:
            continue
        dest = site / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    _patch(site, PATCHES)
    shutil.copyfile(HERE / "growbot-box.js", site / "growbot-box.js")
    shutil.copyfile(HERE / "growbot-face.js", site / "growbot-face.js")
    # The installed app (Android): served without a session, since Chrome fetches the
    # manifest and its icons with no cookie.
    (site / "icons").mkdir(exist_ok=True)
    for name in PWA_FILES:
        dest = site / ("icons/" + name if name.endswith(".png") else name)
        shutil.copyfile(HERE / "pwa" / name, dest)
    for entry in pin["vendor"]:
        dest = site / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_fetch(entry, cache))

    hashes: set[str] = set()
    handlers = False
    for page in sorted(site.rglob("*.html")):
        markup = page.read_text(encoding="utf-8")
        bad = _unsupported_handlers(markup)
        if bad:
            raise PatchError(f"{page.relative_to(site)}: unhashable inline handler {bad}")
        page_hashes, page_handlers = csp_hashes(markup)
        hashes |= page_hashes
        handlers = handlers or page_handlers
    csp = {"script_hashes": sorted(hashes), "unsafe_hashes": handlers}
    (site / "csp.json").write_text(json.dumps(csp, indent=1) + "\n")

    firmware = stage / "firmware"
    firmware.mkdir(parents=True)
    for name in FIRMWARE_FILES:
        shutil.copyfile(upstream / "firmware" / name, firmware / name)
    _patch(firmware, FIRMWARE_PATCHES)
    with (firmware / "secrets.example.py").open("a", encoding="utf-8") as f:
        f.write(SECRETS_EXAMPLE_TAIL)
    # MicroPython itself, pinned like every vendored asset: the endpoint sidecar never
    # fetches, so the box loads exactly these bytes onto a blank Pico.
    uf2 = pin["micropython"]
    (firmware / uf2["path"]).write_bytes(_fetch(uf2, cache))
    sums = [
        f"{hashlib.sha256((firmware / name).read_bytes()).hexdigest()}  {name}\n"
        for name in (uf2["path"], *FLASHED_FILES)
    ]
    (firmware / "SHA256SUMS").write_text("".join(sums))


def build(upstream: Path, out: Path, cache: Path | None) -> None:
    pin = json.loads((HERE / "upstream.json").read_text())
    if not (upstream / "app").is_dir():
        raise PatchError(f"{upstream} has no app/ (expected a GrowBot checkout)")
    _pinned_commit(upstream, pin["commit"])
    # Build beside the committed tree and swap only on success, so a patch that fails leaves
    # what is committed untouched.
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        stage = Path(tmp)
        _stage(upstream, stage, pin, cache)
        for name in ("site", "firmware"):
            if (out / name).exists():
                shutil.rmtree(out / name)
            (stage / name).rename(out / name)
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
