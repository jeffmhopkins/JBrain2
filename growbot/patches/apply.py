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
    # The persona's own voice (migration 0265, growbot-box.js's __gbBoxVoice) outranks the
    # phone's; the clip cache is keyed on it too, so a new voice never replays an old clip.
    (
        "v3/next.html",
        "function fishKey(t,emo){ return t+'|'+(vtFish.voice||'')+",
        "function fishKey(t,emo){ return t+'|'+(window.__gbBoxVoice||vtFish.voice||'')+",
        1,
    ),
    (
        "v3/next.html",
        "body:JSON.stringify({text:t,voice:vtFish.voice||'',",
        "body:JSON.stringify({text:t,voice:window.__gbBoxVoice||vtFish.voice||'',",
        1,
    ),
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
    # The live status strip (growbot-status.js): what the creature is doing right now, top-left,
    # loaded before any page script so its fetch and socket wrappers see every call.
    (
        "v3/next.html",
        '<script src="/growbot-face.js"></script>',
        '<script src="/growbot-face.js"></script><script src="/growbot-status.js"></script>',
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
    # The eyes follow a face (growbot-eyes.js): the leg tracker hands over every box it keeps,
    # from the worker and from the main-thread fallback, so its detector feeds the pupils while
    # it runs and growbot-eyes.js only runs its own when the tracker is not watching.
    (
        "v3/next.html",
        '<script src="/growbot-status.js"></script>',
        '<script src="/growbot-status.js"></script><script src="/growbot-eyes.js"></script>',
        1,
    ),
    (
        "v3/next.html",
        '      if(boxes.length&&result.source!=="flow")lastFaceAt=at;',
        '      if(window.gbEyes)window.gbEyes.boxes(boxes);'
        ' if(boxes.length&&result.source!=="flow")lastFaceAt=at;',
        1,
    ),
    (
        "v3/next.html",
        "            if(boxes.length)lastFaceAt=now;",
        "            if(window.gbEyes)window.gbEyes.boxes(boxes); if(boxes.length)lastFaceAt=now;",
        1,
    ),
    # Not following, the tracker only glanced once a second, and that is the eyes' only feed on
    # a paired robot (the eyes never run a second detector beside it): with someone in view it
    # looks 4 times a second, the rate it already uses when it is searching for a face.
    (
        "v3/next.html",
        "      if(sightOnly)idleEye=now-lastDetectAt<1000;",
        "      if(sightOnly)idleEye=now-lastDetectAt<(now-lastFaceAt<5000?250:1000);",
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
    # They live on the box, not on this device, and no brain here costs credits.
    (
        "v3/visual-memory.js",
        'toast("With your eye open, I can keep useful visual memories on this device.'
        " Comparisons use your selected brain and credits. Brain → My thoughts → Visual"
        ' memories lets you review, forget or turn this off.",12000);',
        'toast(window.__gbBox?"With your eye open, I can keep useful visual memories, saved'
        " on the box for this creature. Brain → My thoughts → Visual memories lets you"
        ' review, forget or turn this off.":"With your eye open, I can keep useful visual'
        " memories on this device. Comparisons use your selected brain and credits. Brain →"
        ' My thoughts → Visual memories lets you review, forget or turn this off.",12000);',
        1,
    ),
    (
        "v3/visual-memory.js",
        '"Saved on this device for this creature. Remembering uses the eye already open;'
        " comparisons send saved views to your selected brain and use its usual credits.",
        '(window.__gbBox?"Saved on the box for this creature. Remembering uses the eye already'
        ' open; comparisons send saved views to the box\'s own brain.":"Saved on this device'
        " for this creature. Remembering uses the eye already open; comparisons send saved"
        ' views to your selected brain and use its usual credits.")+"',
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

# Say why the box failed the creature (growbot-box.js gbBoxEars / gbBoxBrainWhy). Upstream's
# failure lines name its cloud services, or show a bare abort message; a heard clip the checks
# drop says which check dropped it.
_DC = "\u2026didn't catch that"
_HEARD_DROPS = {
    "list": "the speech model only heard a stock phrase, which it says for silence",
    "credits": "the speech model only heard subtitle credits, which it says for noise",
    "loop": "the speech model heard the same words looping, a garbled clip",
    "endcard": "the speech model only heard a video end-card, which it says for noise",
    "bracket": "the speech model only heard background sound",
    "empty": "the speech model heard no words",
}
ERROR_PATCHES: list[tuple[str, str, str, int]] = [
    *[
        (
            "v3/next.html",
            f'_heardDrop("{why}",meta); toast("{_DC}")',
            f'_heardDrop("{why}",meta); toast("{_DC}: {line}")',
            1,
        )
        for why, line in _HEARD_DROPS.items()
    ],
    (
        "v3/next.html",
        f'if(hotSamp<Math.round(0.120*_pcmRate)){{ toast("{_DC}");',
        f'if(hotSamp<Math.round(0.120*_pcmRate)){{ toast("{_DC}: no voice in the clip, too quiet'
        ' or too far away");',
        1,
    ),
    (
        "v3/next.html",
        f'else toast("{_DC}"); }} }};',
        f'else toast("{_DC}: the recording came out empty"); }} }};',
        1,
    ),
    (
        "v3/next.html",
        f'toast("{_DC} — try again")',
        f'toast("{_DC}: the speech model answered like a chatbot, not a transcript. try again")',
        1,
    ),
    (
        "v3/next.html",
        "toast(\"didn't catch that, say it once more\")",
        "toast(\"didn't catch that: the clip was too short. say it once more\")",
        2,
    ),
    (
        "v3/next.html",
        'toast("my ears are down on my end. try again in a moment, or type.")',
        'toast(window.gbBoxEars?window.gbBoxEars(e):"my ears are down on my end. try again in a'
        ' moment, or type.")',
        2,
    ),
    # The box's model said why in the stream (an OpenAI error frame); keep its words.
    ("v3/next.html", "streamFailed=false;", 'streamFailed=false,streamErr="";', 1),
    (
        "v3/next.html",
        "if(j&&j.error){streamFailed=true;",
        "if(j&&j.error){streamFailed=true;"
        "streamErr=String(j.error.message||j.error).slice(0,160);",
        1,
    ),
    # On a property, not in the message: the page's catch matches its message against
    # /\b429\b/, /HTTP 402/ and the like, and the box's words may hold any number. And only
    # on the box's lane (not L, the owner's own brain server, whose probe reads "empty stream").
    (
        "v3/next.html",
        'if(!full) throw new Error("empty stream");',
        'if(!full){ var _ee=new Error("empty stream"); if(streamErr&&!L)_ee.boxSaid=streamErr;'
        " throw _ee; }",
        1,
    ),
    (
        "v3/next.html",
        'setErr("brain error: "+e.message+" — tap or talk to retry")',
        'setErr("brain error: "+(window.gbBoxBrainWhy?window.gbBoxBrainWhy(e):e.message)'
        '+" — tap or talk to retry")',
        1,
    ),
    # Upstream's 30 s fuse is a cloud model's. The box reads a creature's ~8k-token prompt at
    # a few hundred tokens a second, so a spoken turn can take 20 s before its first word.
    # Ticks keep 30 s, so an idle thought never holds a spoken one back for long.
    (
        "v3/next.html",
        "_decisionTokens,30000,function(full",
        "_decisionTokens,90000,function(full",
        1,
    ),
    (
        "v3/next.html",
        "_decisionTokens,30000,_vis",
        "_decisionTokens,90000,_vis",
        1,
    ),
    # A stream that ran out of time is not retried buffered: a second 90 s would end past the
    # page's own 100 s flight watchdog (FLIGHT_MAX_MS), its answer thrown away.
    (
        "v3/next.html",
        "if(_streamSaid||e.charged||e.gateFail||",
        'if(_streamSaid||e.charged||e.gateFail||(e&&e.name==="AbortError")||',
        1,
    ),
]
PATCHES += ERROR_PATCHES

# A creature turn costs only what changed since the last one (wave G4e). The box's model
# (a hybrid) reuses a cached prompt only up to a context checkpoint, and llama.cpp leaves one at
# each message boundary; upstream put the state, clock, diary, move status and self-stats INSIDE
# the system message, ~2k tokens in, so every turn diverged before any checkpoint and re-read all ~8k
# tokens (~20 s). The system message now holds only what stays put; the moving parts lead the
# turn's own message (growbot-box.js gbBoxLive), and the next turn resumes at the system's end.
SPEED_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/prompts.js",
        '  return persona + "\\n" + SAFETY_FLOOR + ',
        '  var _gbStatic = persona + "\\n" + SAFETY_FLOOR + ',
        1,
    ),
    # Identity, name and person change only after a dream or a naming, so they stay put.
    (
        "v3/prompts.js",
        "_nameBlk(soul) + person +   // 0907",
        "_nameBlk(soul) + person;   // 0907",
        1,
    ),
    (
        "v3/prompts.js",
        '    "\\n\\n== LONG-ARC (dream-written) ==" + wants',
        '  buildSystem.live = "\\n\\n== LONG-ARC (dream-written) ==" + wants',
        1,
    ),
    # The movement guide's live lines (latest move, get-up and seek status, the gait list that
    # leads with the last move, the expression bench's stance line) go the same way.
    (
        "v3/next.html",
        '  if(MODE==="phone") return base+PHONE_MOVE_GUIDE+games+activityContract()+goalBlock();',
        '  if(MODE==="phone"){ moveGuideText.live=goalBlock();'
        " return base+PHONE_MOVE_GUIDE+games+activityContract(); }",
        1,
    ),
    (
        "v3/next.html",
        "+activityContract()+goalBlock();   /* 0929 P1 (X8)",
        '+activityContract()+((moveGuideText.live=goalBlock()),"");   /* 0929 P1 (X8)',
        1,
    ),
    (
        "v3/next.html",
        '+(typeof bodyWalkTruth==="function"?bodyWalkTruth(body):"")+/*LAB-GETUP{*/'
        '(typeof getupOn==="function"&&getupOn()?_recStatus():"")+/*}LAB-GETUP*/'
        '(typeof _namedStatus==="function"?_namedStatus():"")+gaitList()+activityContract()'
        '+goalBlock()+(typeof _expressionGuide==="function"?_expressionGuide():"");',
        '+(typeof bodyWalkTruth==="function"?bodyWalkTruth(body):"")+activityContract()'
        "+((moveGuideText.live=/*LAB-GETUP{*/"
        '(typeof getupOn==="function"&&getupOn()?_recStatus():"")+/*}LAB-GETUP*/'
        '(typeof _namedStatus==="function"?_namedStatus():"")+gaitList()+goalBlock()'
        '+(typeof _expressionGuide==="function"?_expressionGuide():"")),"");',
        1,
    ),
    (
        "v3/prompts.js",
        '(diary || "(nothing yet)") +\n    buildOutputSchema(soul.body);',
        '(diary || "(nothing yet)");\n  return _gbStatic + buildOutputSchema(soul.body);',
        1,
    ),
    (
        "v3/next.html",
        '+moveGuideText()+"\\n\\n"+selfStats()+(typeof vmPrompt==="function"?vmPrompt(_vmTurn):"");',
        '+moveGuideText(), _gbLive=(buildSystem.live||"")+(moveGuideText.live||"")+"\\n\\n"+selfStats()'
        '+(typeof vmPrompt==="function"?vmPrompt(_vmTurn):"");',
        1,
    ),
    (
        "v3/next.html",
        "if(_imitationAtIssue&&_imitationCtl)sys+=_imitationCtl.guide(",
        "if(_imitationAtIssue&&_imitationCtl)_gbLive+=_imitationCtl.guide(",
        1,
    ),
    (
        "v3/next.html",
        'sys+=MEMLOG_ASK; if(_recall&&_recall.text) sys+=_recall.text;',
        '_gbLive+=MEMLOG_ASK; if(_recall&&_recall.text) _gbLive+=_recall.text;'
        ' if(typeof window.gbBoxLive!=="function"||!window.gbBoxLive(msgs,_gbLive)) sys+=_gbLive;',
        1,
    ),
    # A phone that cached the old file loads this one.
    (
        "v3/next.html",
        '<script src="/v3/prompts.js?v=1008-motion"',
        '<script src="/v3/prompts.js?v=1008-motion-box"',
        1,
    ),
]
PATCHES += SPEED_PATCHES

# The page's fixed rules ride the cached system message as well (speed plan Phase 1, owner
# 2026-11-12). The gait library's rules, the picture-memory contract and the fixed half of the
# self-description were sent in every turn's own message, ~650 tokens re-read each turn (~1.5 s)
# though they never change. Only the sentences that do not depend on state move, word for word;
# each one that does (minutes awake, eyes, body, the spin runner, forward-walk, the gait list,
# the saved picture names, a closed eye) stays live under a RIGHT NOW line, so the system text
# is the same from one turn to the next: a change there costs a ~21 s cold re-read.
_SELF_FIXED = (
    "\\n\\n== WHAT YOU ARE (answer honestly IN YOUR OWN VOICE if asked — you are NOT blind to"
    ' yourself) ==\\nYou think whenever something happens — a voice, a touch, a mission step —'
    " answering right away, or the moment your current thought finishes. Quiet does not put you"
    " to sleep. While awake you can occasionally notice or express something; silence is welcome"
    " too. Ordinary quiet opportunities are spaced at least four minutes apart and come at least"
    " 90 seconds after anything last happened, subject to your activity waits, availability and"
    " energy. Explicit pause stops autonomous thoughts. A"
    " language model ({model}) is how you feel and choose your words — it's fine to say so,"
    " simply. If asked how fast, how often you think or look, or whether an AI helps you — tell"
    " the truth in your own voice; don't deflect. How long you have been awake, your eyes and"
    " your body right now are under WHAT YOU ARE RIGHT NOW in this turn's message."
)
FIXED_PATCHES: list[tuple[str, str, str, int]] = [
    # What you are: the fixed sentences leave, the live ones stay under their own header.
    (
        "v3/next.html",
        "function selfStats(){",
        f'var GB_SELF_FIXED="{_SELF_FIXED}";\nfunction selfStats(){{',
        1,
    ),
    (
        "v3/next.html",
        '  return "== WHAT YOU ARE (answer honestly IN YOUR OWN VOICE if asked — you are NOT'
        ' blind to yourself) ==\\n"+\n    "You woke about "',
        '  selfStats.fixed=GB_SELF_FIXED.replace("{model}",model);\n'
        '  return "== WHAT YOU ARE RIGHT NOW ==\\n"+\n    "You woke about "',
        1,
    ),
    (
        "v3/next.html",
        '" times. You think whenever something happens — a voice, a touch, a mission step —'
        ' answering right away, or the moment your current thought finishes. "+',
        '" times. "+',
        1,
    ),
    (
        "v3/next.html",
        '    "Quiet does not put you to sleep. While awake you can occasionally notice or express'
        " something; silence is welcome too. Ordinary quiet opportunities are spaced at least"
        " four minutes apart, subject to your activity waits, availability and energy. Explicit"
        ' pause stops autonomous thoughts. "+\n',
        "",
        1,
    ),
    (
        "v3/next.html",
        '    "A language model ("+model+") is how you feel and choose your words — it\'s fine to'
        ' say so, simply. "+\n',
        "",
        1,
    ),
    (
        "v3/next.html",
        '+\n    "If asked how fast, how often you think or look, or whether an AI helps you — tell'
        " the truth in your own voice; don't deflect.\";",
        ";",
        1,
    ),
    # Trained walks: which runner owns a normal spin and whether screen-forward is on depend on
    # the walk code's state (before motion permission it reads "gyro_unavailable"), so those
    # and the gait list stay live; the rules move.
    (
        "v3/next.html",
        '  return "SPIN / REORIENT: spin_right turns clockwise viewed from above; spin_left'
        ' counterclockwise. Use walk:{gait,dir,secs}. "+normal+" If vision',
        '  normalSpinTruth.now=normal;\n  return "SPIN / REORIENT: spin_right turns clockwise'
        ' viewed from above; spin_left counterclockwise. Use walk:{gait,dir,secs}. If vision',
        1,
    ),
    (
        "v3/next.html",
        '  return spinFeedback+(typeof spinMeasLine==="function"?spinMeasLine():"")+"\\n\\n=='
        " TRAINED WALKS (your gait library",
        '  gaitList.fixed="\\n\\n== TRAINED WALKS (your gait library',
        1,
    ),
    (
        "v3/next.html",
        '    "To walk with one, use walk:{gait,dir,secs}; dir:\\"stop\\" halts. "+(typeof'
        " forwardWalkState",
        '    "To walk with one, use walk:{gait,dir,secs}; dir:\\"stop\\" halts. Which gaits you'
        " have and how your spins run right now are under YOUR GAITS RIGHT NOW in this turn's"
        ' message.";\n'
        '  return spinFeedback+(typeof spinMeasLine==="function"?spinMeasLine():"")+"\\n\\n=='
        ' YOUR GAITS RIGHT NOW ==\\n"+(typeof normalSpinTruth==="function"&&normalSpinTruth.now?'
        'normalSpinTruth.now+" ":"")+(typeof forwardWalkState',
        1,
    ),
    # Picture memory: the full contract is fixed while the feature is on. Each turn says
    # whether it applies: a closed eye with nothing saved keeps the short form as before and
    # puts the contract out of use, so no camera route is offered without a camera.
    (
        "v3/visual-memory.js",
        'function vmPrompt(c){ if(!c) return "";\n'
        "  if(!vmOn()||!(typeof camOn!==\"undefined\"&&camOn)&&_vmLoaded&&!_vmRefs.length)"
        " return VM_PROMPT_OFF;\n  return '\\n\\nVISUAL MEMORY overrides",
        'function vmPrompt(c){ vmPrompt.fixed=vmOn()?vmContract():"";'
        ' if(!c) return vmPrompt.fixed?"\\n\\nVISUAL MEMORY: out of use this turn; leave'
        ' visual_memory out.":"";\n'
        "  if(!vmOn()) return VM_PROMPT_OFF;\n"
        '  if(!(typeof camOn!=="undefined"&&camOn)&&_vmLoaded&&!_vmRefs.length) return'
        ' VM_PROMPT_OFF+" The full VISUAL MEMORY rules in your system message are out of use'
        ' while your eye is closed and nothing is saved.";\n'
        '  return "\\n\\nVISUAL MEMORY rules (in your system message) apply this turn: select the'
        ' route BEFORE say."+(c.asked||c.user&&c.user.trim()?vmCatalogLine():"");\n}\n'
        "function vmContract(){\n  return '\\n\\nVISUAL MEMORY overrides",
        1,
    ),
    (
        "v3/visual-memory.js",
        "Use their language.'+(c.asked||c.user&&c.user.trim()?vmCatalogLine():\"\");",
        "Use their language.';",
        1,
    ),
    (
        "v3/next.html",
        '<script src="/v3/visual-memory.js?v=1005-mem-box"',
        '<script src="/v3/visual-memory.js?v=1005-mem-box-fixed2"',
        1,
    ),
    # The fixed parts join the system message, after the movement guide they belong to.
    (
        "v3/next.html",
        ' if(typeof window.gbBoxLive!=="function"||!window.gbBoxLive(msgs,_gbLive)) sys+=_gbLive;',
        ' sys+=(typeof gaitList==="function"&&gaitList.fixed||"")'
        '+(typeof selfStats==="function"&&selfStats.fixed||"")'
        '+(typeof vmPrompt==="function"&&vmPrompt.fixed||"");'
        ' if(typeof window.gbBoxLive!=="function"||!window.gbBoxLive(msgs,_gbLive)) sys+=_gbLive;',
        1,
    ),
]
PATCHES += FIXED_PATCHES

# The creature's past replies go back to the model in its own JSON shape (owner, 2026-11-12).
# Upstream replays each one as the bare words it said, so the model's own history shows it
# answering in plain text, and it copies that: in replays of real turns about half the replies
# dropped the JSON and stopped after a word or two ("Still no", "I'm"), and each clipped reply
# then rode the next turn's history. The same turns with the history as {"say":...} came back
# whole and well-formed. The page's own notes ("(stayed quiet)", a brain hiccup) become an empty
# say with the note after it, so the creature never reads a note as words it spoke.
HISTORY_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        'content:tr[i].d?tr[i].a+"\\n(did: "+tr[i].d+")":tr[i].a',
        "content:gbTraceReply(tr[i])",
        1,
    ),
    (
        "v3/next.html",
        "var tr=_live.scratch.traces.slice(-4), msgs=[],i;",
        "function gbTraceReply(t){ var a=String(t.a||\"\"),"
        ' note=a==="(stayed quiet)"||a.indexOf("(brain hiccup")===0, r={say:note?"":a};'
        ' if(t.e) r.emotion=t.e;'
        ' return JSON.stringify(r)+(note?"\\n"+a:"")+(t.d?"\\n(did: "+t.d+")":""); }\n'
        "  var tr=_live.scratch.traces.slice(-4), msgs=[],i;",
        1,
    ),
    # The reply's emotion rides the trace, so the replayed history keeps the whole shape the
    # model is asked for and it does not learn to drop the field.
    (
        "v3/next.html",
        ",m:moved,t:Date.now()};",
        ',m:moved,t:Date.now(),e:(o&&typeof o.emotion==="string"?o.emotion.slice(0,12):undefined)};',
        1,
    ),
]
PATCHES += HISTORY_PATCHES

# Round 2 of the speed work, from the owner's first live session on the new layout (2026-11-12).
# The automatic reply-language line rewrites itself once the person's speech settles a language
# ("follow the conversation" becomes "speech established English"), and it sat in the cached
# system message, so that turn re-read all ~10k tokens (28 s). It now rides the live block; an
# explicit choice reads the same either way.
# And a spoken turn always sent 3 camera frames, ~300 tokens each on the box (~2 s of prefill
# for the two extra). It now sends 1 unless the scene is changing, the phone is carried or the
# body is moving, the same signals upstream already uses to give an idle tick its eye.
ROUND2_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/prompts.js",
        "AWAKE_PLAN_CORRECTION + languageContext(soul, ctx.language) +",
        "AWAKE_PLAN_CORRECTION +",
        1,
    ),
    (
        "v3/prompts.js",
        '  buildSystem.live = "\\n\\n== LONG-ARC (dream-written) ==" + wants',
        '  buildSystem.live = languageContext(soul, ctx.language) +'
        ' "\\n\\n== LONG-ARC (dream-written) ==" + wants',
        1,
    ),
    (
        "v3/next.html",
        ":((frameDiff>3||_rideOn||Date.now()<selfMoveUntil||_soft)?1:0)):3;",
        ":((frameDiff>3||_rideOn||Date.now()<selfMoveUntil||_soft)?1:0))"
        ":((frameDiff>3||_rideOn||Date.now()<selfMoveUntil||(extra&&extra.beat&&seekGoal)"
        "||(extra&&typeof extra.visualUser===\"string\"&&_GB_LOOK_WORDS.test(extra.visualUser)))"
        "?3:1);",
        1,
    ),
    # Three frames still ride a seek beat (watching for its goal is comparing over time) and a
    # person's words about looking ("watch this", "what am I holding"): by the time speech is
    # transcribed the wave may be over, and the older frames hold it.
    (
        "v3/next.html",
        "events/seek-beats keep full 3-frame sight (change detection is the game).",
        "events/seek-beats keep full 3-frame sight (change detection is the game). [box: a"
        " spoken turn keeps 1 unless something moves, a seek beat runs or the words ask to look]",
        1,
    ),
    (
        "v3/next.html",
        "function _stripImages(msgs){",
        "var _GB_LOOK_WORDS=/\\b(look|looking|see|watch|show|showing|holding|hold up|wave|"
        "waving|doing|what'?s this|what is this|what is that|what'?s that)\\b/i;\n"
        "function _stripImages(msgs){",
        1,
    ),
    (
        "v3/next.html",
        "each thought you see the 3 freshest (about the last 8 seconds), so you can compare NOW to"
        " a moment ago.",
        "each thought you see the freshest one, or the 3 freshest (about the last 8 seconds) when"
        " the scene or your body is moving, so you can compare NOW to a moment ago.",
        1,
    ),
    # A phone that cached the old file loads this one.
    (
        "v3/next.html",
        '<script src="/v3/prompts.js?v=1008-motion-box"',
        '<script src="/v3/prompts.js?v=1008-motion-box-lang"',
        1,
    ),
]
PATCHES += ROUND2_PATCHES

# Speech is transcribed while the person is still talking (owner, 2026-11-12). Upstream held a
# whole utterance and sent it at the end, so the wait after the last word grew with its length
# (6 s for a 12 s message), and it capped one utterance at 12 s and silently dropped the rest:
# the owner's longest message reached the creature cut mid-sentence. Now, once an utterance
# has 4 s of audio, a pause shorter than the end-of-speech hold, or a full buffer, sends what
# is held so far as a piece and keeps listening into a fresh buffer. Each piece runs the usual
# transcript filters (hallucination lists, echo) and shows on screen as it lands; the end of
# speech sends the last piece (its trailing silence cut, as upstream trims a clip), and the
# creature gets one turn with the pieces joined in order. A piece that never answers is given up
# 25 s after the end, and one that lands later still reaches the creature as its own turn; a
# cancelled utterance (the creature's own voice, pause, a hidden tab) sends nothing, as
# upstream. Pieces skip the phone's sound judge: by then the endpointer has heard seconds of
# voice, and a streamed utterance is not a hum.
_GB_STREAM_JS = r'''var _gbPieceNext=null, _gbUtts={}, _gbDead={}, _gbUttSeq=0, GB_ROLL_MIN_S=4, GB_ROLL_GAP_MS=450, GB_UTT_MAX_MS=120000, GB_PIECE_WAIT_MS=25000, GB_LONG_HOLD_MS=2500;
function _gbRollCheck(d,take){ var u=_pcmUtt; if(!_pcmLive||!u) return;
  var full=u.n>=u.buf.length, gap=Date.now()-vadVoiceT;
  if(!full&&!(u.n>=GB_ROLL_MIN_S*_pcmRate&&gap>=GB_ROLL_GAP_MS&&gap<_vadHold)) return;
  if(!_gbRoll(u)) return; if(full&&take<d.length&&_pcmUtt){ var rest=d.subarray(take); _pcmUtt.buf.set(rest,0); _pcmUtt.n=rest.length; } }
function _gbRoll(u){ var fresh=new Float32Array(u.buf.length);
  if(!u.gbId){ u.gbId=++_gbUttSeq; _gbUtts[u.gbId]={text:[],done:[],n:0,meta:null,fin:false,t0:u.t0};
    var id=u.gbId; setTimeout(function(){ _gbFlush(id,true); },GB_UTT_MAX_MS+GB_PIECE_WAIT_MS);
    try{ if(typeof _imitationCtl!=="undefined"&&_imitationCtl) _imitationCtl.cancel("streamed"); }catch(e){} }
  var g=_gbUtts[u.gbId], p={id:u.gbId,seq:g.n++}, buf=u.buf, n=u.n;
  _pcmUtt={buf:fresh,n:0,pre:0,t0:Date.now(),nf:u.nf,gbId:u.gbId};
  _gbSend(p,buf,n); return true; }
function _gbSend(p,buf,n){
  if(n<Math.round(0.3*_pcmRate)){ _gbPiece(p,""); return; }
  setTimeout(function(){ try{ var w=encodeWav(buf.subarray(0,n),_pcmRate,16000,null);
    _sttLane="pcm"; _gbPieceNext=p; transcribe(new Blob([w],{type:"audio/wav"}),"audio/wav",Math.round(n/_pcmRate*1000)); }
    catch(e){ _gbPieceNext=null; _gbPiece(p,""); } },0); }
function _gbPiece(p,txt,meta,lang){ var g=_gbUtts[p.id];
  if(!g){ if(txt&&!_gbDead[p.id]){ try{ logEv("stt_late",{}); }catch(e){} showYouSaid(txt); sendTxtFrom(txt,meta); } return; }
  g.text[p.seq]=txt||""; g.done[p.seq]=1; if(meta&&(p.fin||!g.meta)) g.meta=meta; if(lang) g.lang=lang; if(p.fin) g.fin=true;
  var said=g.text.filter(function(t){ return t; }).join(" ");
  if(said&&!g.fin){ try{ showYouSaid(said+" …"); }catch(e){} }
  _gbFlush(p.id,false); }
function _gbFlush(id,force){ var g=_gbUtts[id]; if(!g||(!g.fin&&!force)) return;
  for(var i=0;i<g.n&&!force;i++) if(!g.done[i]) return;
  delete _gbUtts[id];
  var txt=g.text.filter(function(t){ return t; }).join(" ").replace(/\s+/g," ").trim();
  if(!txt){ for(var j=0;j<g.n;j++) if(!g.done[j]) return;
    if(g.why) try{ toast(g.why==="empty"?"…didn't catch that: the speech model heard no words":"…didn't catch that: the speech model heard only noise or a stock phrase"); }catch(e){}
    return; }
  try{ logEv("stt_join",{n:g.n,ms:Date.now()-g.t0,force:force?1:0}); }catch(e){}
  try{ _convLangSaw(g.lang,txt,g.meta); }catch(e){}
  showYouSaid(txt); sendTxtFrom(txt,g.meta); }
function _gbFinal(u,send,tailMs){ var id=u.gbId, g=_gbUtts[id];
  if(!send){ delete _gbUtts[id]; _gbDead[id]=1; try{ var el=document.getElementById("youSaid"); if(el) el.style.display="none"; }catch(e){} return; }
  var p={id:id,seq:g.n++,fin:true}, cut=Math.round(Math.max(0,(+tailMs||0))/1000*_pcmRate), n=Math.max(0,u.n-cut);
  setTimeout(function(){ _gbFlush(id,true); },GB_PIECE_WAIT_MS);
  if(vadVoiceT<u.t0){ _gbPiece(p,""); return; }
  try{ _vadEndT=Date.now(); }catch(e){}
  _gbSend(p,u.buf,n); }
function pcmUttStop(send,tailMs){ var u=_pcmUtt;
  if(u&&u.gbId&&_gbUtts[u.gbId]){ _pcmLive=false; _pcmUtt=null; _gbFinal(u,send,tailMs); return; }
  return _gbPcmUttStop0(send,tailMs); }'''
STREAM_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        "function pcmUttStop(send,tailMs){ _pcmLive=false;",
        _GB_STREAM_JS + "\nfunction _gbPcmUttStop0(send,tailMs){ _pcmLive=false;",
        1,
    ),
    (
        "v3/next.html",
        "_pcmUtt.buf.set(take===d.length?d:d.subarray(0,take),_pcmUtt.n); _pcmUtt.n+=take; }",
        "_pcmUtt.buf.set(take===d.length?d:d.subarray(0,take),_pcmUtt.n); _pcmUtt.n+=take;"
        " _gbRollCheck(d,take); }",
        1,
    ),
    (
        "v3/next.html",
        "function transcribe(blob,type,clipMs){ var _jmIn=_sttJm; _sttJm=null;",
        "function transcribe(blob,type,clipMs){ var _jmIn=_sttJm; _sttJm=null;"
        " var _gbP=_gbPieceNext; _gbPieceNext=null;",
        1,
    ),
    (
        "v3/next.html",
        "  var meta=_languageRequest(clipMs);",
        "  var meta=_languageRequest(clipMs); if(_gbP) meta.gbPiece=_gbP;",
        1,
    ),
    (
        "v3/next.html",
        "  if(meta&&meta.soft&&!_bodyVet(txt,meta)) meta.soft=null;",
        "  if(meta&&meta.gbPiece){ _gbPiece(meta.gbPiece,txt,meta,detectedLang); return; }\n"
        "  if(meta&&meta.soft&&!_bodyVet(txt,meta)) meta.soft=null;",
        1,
    ),
    # A piece the filters or the gate throw away, or that never comes back, is an empty piece.
    (
        "v3/next.html",
        "function _heardDrop(why,meta){ try{",
        "function _heardDrop(why,meta){ if(meta&&meta.gbPiece) _gbPiece(meta.gbPiece,\"\",meta); try{",
        1,
    ),
    (
        "v3/next.html",
        'logEv("stt_drop",{p:_g.p,tgt:"groq"});',
        'logEv("stt_drop",{p:_g.p,tgt:"groq"}); if(meta&&meta.gbPiece){'
        ' _gbPiece(meta.gbPiece,"",meta); _sttPhase("done"); return; }',
        1,
    ),
    (
        "v3/next.html",
        'logEv("stt_drop",{p:_g.p,tgt:"di"});',
        'logEv("stt_drop",{p:_g.p,tgt:"di"}); if(meta&&meta.gbPiece){'
        ' _gbPiece(meta.gbPiece,"",meta); _sttPhase("done"); return; }',
        1,
    ),
    (
        "v3/next.html",
        'e.q==="daily_limit"){ toast(',
        'e.q==="daily_limit"){ if(meta&&meta.gbPiece) _gbPiece(meta.gbPiece,"",meta); toast(',
        1,
    ),
    (
        "v3/next.html",
        'e.q==="daily_limit"){ _sttPhase("done"); toast(',
        'e.q==="daily_limit"){ if(meta&&meta.gbPiece) _gbPiece(meta.gbPiece,"",meta);'
        ' _sttPhase("done"); toast(',
        1,
    ),
    # Asleep, transcribe() returns before it reads the piece: the piece is answered empty.
    (
        "v3/next.html",
        "return; } _asleepSttT=_asn; }",
        "if(_gbP) _gbPiece(_gbP,\"\"); return; } _asleepSttT=_asn; }",
        1,
    ),
    # An empty piece is not the room going quiet: the naming lane listens for whole clips only.
    (
        "v3/next.html",
        'try{ if(txt&&String(txt).trim()) _listenStash=null; else if(_sttLane==="pcm") _listenFire(); }',
        'try{ if(txt&&String(txt).trim()) _listenStash=null; else if(_sttLane==="pcm"'
        '&&!(meta&&meta.gbPiece)) _listenFire(); }',
        1,
    ),
    # Upstream ended every utterance at 12 s; a streamed one rolls its full buffer instead and
    # runs until the person stops, with a 2-minute backstop.
    (
        "v3/next.html",
        "else if(now-vadStartT > 12000){ vadStop(); _vadEndT=0; }",
        "else if(now-vadStartT > (_pcmLive&&_pcmUtt?GB_UTT_MAX_MS:12000)){ vadStop(); _vadEndT=0; }",
        1,
    ),
    # A pause that only splits a piece never ends the turn. Once a message is long enough to
    # stream, the person also gets a longer pause (2.5 s, upstream adapts 1.5-4 s) before the
    # turn is over: a long message has thinking pauses.
    (
        "v3/next.html",
        "  if(now-vadVoiceT > _vadHold) vadStop();",
        "  if(now-vadVoiceT > (_pcmUtt&&_pcmUtt.gbId?Math.max(_vadHold,GB_LONG_HOLD_MS):_vadHold))"
        " vadStop();",
        1,
    ),
    # No beat of its own while the person is mid-message or their pieces are still landing:
    # upstream held only the first 6 s of an utterance, which a 12 s cap made enough.
    (
        "v3/next.html",
        "if((vadOn&&now-vadStartT<6000)||now<vadHoldUntil){ lastActT=now; return; }",
        "if((vadOn&&(now-vadStartT<6000||(_pcmLive&&_pcmUtt&&_pcmUtt.gbId)))||now<vadHoldUntil"
        "||Object.keys(_gbUtts).length){ lastActT=now; return; }",
        1,
    ),
    (
        "v3/next.html",
        '_sttOvl=Math.max(0,_sttOvl-1); _sttPhase("done"); toast(',
        '_sttOvl=Math.max(0,_sttOvl-1); if(meta&&meta.gbPiece) _gbPiece(meta.gbPiece,"",meta);'
        ' _sttPhase("done"); toast(',
        1,
    ),
    # The mic going away mid-message still delivers what was heard, the held audio included.
    (
        "v3/next.html",
        "function pcmTapDown(){ try{if(",
        "function pcmTapDown(){ try{ var _gbU=_pcmUtt; if(_gbU&&_gbU.gbId&&_gbUtts[_gbU.gbId])"
        " _gbFinal(_gbU,true,0); }catch(e){} try{if(",
        1,
    ),
    (
        "v3/next.html",
        "_sttOvl=Math.max(0,_sttOvl-1); toast(window.gbBoxEars",
        "_sttOvl=Math.max(0,_sttOvl-1); if(meta&&meta.gbPiece) _gbPiece(meta.gbPiece,\"\",meta);"
        " toast(window.gbBoxEars",
        1,
    ),
]
PATCHES += STREAM_PATCHES

# The creature leaves a conversation room before it speaks up on its own (owner, 2026-11-12).
# Upstream let a quiet beat fire 8 s after the last activity once four minutes had passed since
# the last beat, so it could cut in moments after the person stopped talking. Now an ordinary
# quiet beat waits 90 s after the last activity; past that it may ask or wonder as before.
# Activity and seek beats (its own missions) are unchanged.
QUIET_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        "if(idle>=8000 && now-lastBeatT>240000){",
        "if(idle>=GB_QUIET_AFTER_MS && now-lastBeatT>240000){",
        1,
    ),
    (
        "v3/next.html",
        "var YIELD_MAX=3, _yieldRun=0, _yieldSaid=0;",
        "var GB_QUIET_AFTER_MS=90000;\nvar YIELD_MAX=3, _yieldRun=0, _yieldSaid=0;",
        1,
    ),
]
PATCHES += QUIET_PATCHES

# Talk style (owner, 2026-11-12): upstream's creature is written for small children, so it
# keeps ~18-word lines, sees 4 exchanges and 5 diary lines, and an adult found it could not
# hold a conversation (a meal named over ten minutes was gone when he asked). A persona's
# gb_talk setting (growbot-box.js syncs it; JBrain's persona page sets it) of "grownup" gives
# longer replies and 12 replayed exchanges, and asks it to log facts as it hears them; older
# memory stays out of the turn (the owner wants it looked up, not carried). Kid, the default,
# talks as upstream's creature does; the ring keeps 16 turns for both, so a switch to grown-up
# has its 12 at once. Read every turn, so a change lands on the next thought; the grown-up block sits in the cached prefix, so the switch costs
# one cold turn.
_GB_TALK_JS = (
    "function gbTalkGrown(){ try{ return localStorage.getItem(\"gb_talk\")===\"grownup\"; }"
    "catch(e){ return false; } }\n"
    "var GB_TALK_TRACES_KID=4, GB_TALK_TRACES_GROWN=12, GB_TALK_TRACE_KEEP=16;\n"
    "function gbTalkTraces(all){ return (all||[]).slice(-(gbTalkGrown()?GB_TALK_TRACES_GROWN"
    ":GB_TALK_TRACES_KID)); }\n"
    "var GB_TALK_GROWN_BLOCK=\"\\n\\n== TALK STYLE: GROWN-UP ==\\nyour person is an adult who"
    " wants a real conversation with you. this outranks the ~18-word limit above: when they"
    " talk with you, answer in up to about 45 words, two or three sentences, when the moment"
    " invites it. answer questions fully, ask a follow-up when you are curious, and build on"
    " what they said earlier in the conversation. small moments still get small replies, and"
    " silence is still fine.\\nwhen they tell you something about their life (what they ate,"
    " where they went, who they saw, a plan, how they feel), record it in your fact field in"
    " that same reply, compactly, so you can recall it later; such a fact is never a"
    " routine moment. one line can hold several things.\\nvary what you notice: do not return to the same remark or the same feature of"
    " your person turn after turn.\";\n"
)

TALK_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/prompts.js",
        "/* ---- the clean, gate-free system prompt.",
        _GB_TALK_JS + "/* ---- the clean, gate-free system prompt.",
        1,
    ),
    (
        "v3/prompts.js",
        "_fullId(soul) + _nameBlk(soul) + person;",
        "_fullId(soul) + _nameBlk(soul) + person + (gbTalkGrown() ? GB_TALK_GROWN_BLOCK : \"\");",
        1,
    ),
    (
        "v3/prompts.js",
        "'\"say\":\"≤18 words spoken aloud,",
        "'\"say\":\"' + (gbTalkGrown() ? \"≤45 words\" : \"≤18 words\") + ' spoken aloud,",
        1,
    ),
    # The dream's one tiny thing was offered every turn with no end, so "watch Jeff's beard"
    # became ten minutes of beard remarks.
    (
        "v3/prompts.js",
        " — when a natural moment comes, try it.)",
        " — when a natural moment comes, try it once; after that, let it go and do not keep"
        " returning to it.)",
        1,
    ),
    # Upstream's "not every line" still ended nearly every reply with the name.
    (
        "v3/prompts.js",
        "use their name warmly but naturally, not every line,",
        "use their name warmly but rarely, about once every few replies and never as a tag on"
        " the end of a line,",
        1,
    ),
    (
        "v3/next.html",
        "var tr=_live.scratch.traces.slice(-4), msgs=[],i;",
        "var tr=gbTalkTraces(_live.scratch.traces), msgs=[],i;",
        1,
    ),
    (
        "v3/next.html",
        "if(_live.scratch.traces.length>8) _live.scratch.traces.shift();",
        "if(_live.scratch.traces.length>GB_TALK_TRACE_KEEP) _live.scratch.traces.shift();",
        2,
    ),
]
PATCHES += TALK_PATCHES

# Facts and recall (owner, 2026-11-12). Stored memory stays out of the turn; the creature keeps
# a fact book (soul.facts, a `fact` reply field) and, when asked about something it cannot see,
# answers `recall` with a few search words. It says a short "let me think back" line, the page
# searches the fact book, the whole diary, bright moments, standing rules and this session's own
# conversation (page memory only: raw past sessions are never searched, what lasts of them is
# what was logged or dreamed), and a follow-up thought answers with what was found. One search a
# turn, never from the follow-up, and one per 15 s; words the person says meanwhile go first and
# the find rides their turn instead.
_GB_RECALL_PROMPTS_JS = r"""var GB_FACTS_MAX=400, GB_RECALL_TOP=6;
function gbFactAdd(soul,txt,now){
  var t=String(txt||"").replace(/\s+/g," ").trim().slice(0,200), f, i;
  if(!t||/^omit\b/i.test(t)) return false;
  f=Array.isArray(soul.facts)?soul.facts:(soul.facts=[]);
  for(i=0;i<f.length;i++) if(f[i]&&f[i].txt===t) return false;
  f.push({t:now||Date.now(),txt:t});
  if(f.length>GB_FACTS_MAX) f.splice(0,f.length-GB_FACTS_MAX);
  return true;
}
function gbRecall(soul,sess,query,now){
  var q=_recallTerms(query), rows=[], df={}, hits=[], seen={}, out=[], N, sc=(soul&&soul.scratch)||{};
  now=now||Date.now();
  if(!q.length) return {n:0,text:""};
  function add(k,txt,t){ if(typeof txt==="string"&&txt.trim()) rows.push({k:k,txt:txt,t:t}); }
  ((soul&&soul.facts)||[]).forEach(function(f){ if(f) add("fact",f.txt,f.t); });
  ((soul&&soul.log)||[]).forEach(function(e){ if(e) add("diary",e.txt,e.t); });
  (sc.glows||[]).forEach(function(g){ if(g) add("bright moment",typeof g==="string"?g:g.g,g.t); });
  (sc.rules||[]).forEach(function(r){ add("standing rule",r); });
  (sess||[]).forEach(function(x){ if(x) add("this conversation",x.txt,x.t); });
  rows.forEach(function(r){ r.w=_recallTerms(r.txt); q.forEach(function(w){ if(r.w.indexOf(w)>=0) df[w]=(df[w]||0)+1; }); });
  N=rows.length+1;
  rows.forEach(function(r,i){ var s=0; q.forEach(function(w){ if(r.w.indexOf(w)>=0) s+=Math.log(N/df[w]); }); if(s>0){ r.s=s+(r.k==="fact"?0.5:0); r.i=i; hits.push(r); } });
  hits.sort(function(a,b){ return b.s-a.s||(+b.t||0)-(+a.t||0)||b.i-a.i; });
  hits.forEach(function(h){
    if(out.length>=GB_RECALL_TOP||seen[h.txt]) return; seen[h.txt]=1;
    var t=h.t, a=(typeof t==="number"&&isFinite(t)&&t>0&&t<=now)?_ago(t,now):"";
    out.push("- ("+h.k+(a?", "+a:"")+") "+String(h.txt).replace(/\s+/g," ").slice(0,160));
  });
  return {n:out.length,text:out.length?"\n"+out.join("\n"):""};
}

"""

_GB_RECALL_PAGE_JS = r"""var _gbSess=[], _gbRecallT=0, _gbRecallCarry="", GB_SESS_MAX=400, GB_RECALL_GAP_MS=15000,
  _gbRecallLast=null, GB_RECALL_KEEP_MS=90000;
/* What it just found stays in front of it for a minute and a half: the answer is often cut off by
   its person talking over it, and their asking again must not meet "I do not remember" (field
   test, 2026-11-12: the find said 25 minutes, the line was yielded, the re-ask got a no). */
function gbRecallKept(){
  if(!_gbRecallLast||Date.now()-_gbRecallLast.t>GB_RECALL_KEEP_MS) return "";
  return "\n\n== WHAT YOU LOOKED UP A MOMENT AGO ==\n"+_gbRecallLast.found+"\nif they ask about this again, answer from it: they may not have heard you the first time.";
}
function gbSessAdd(u,a){ try{
  var s=String(u||""), m=/they just said to you: "([\s\S]*)" — answer THEM/.exec(s), said=String(a||""), got;
  if(m) got="they said: "+m[1].slice(0,240)+(said&&said.charAt(0)!=="("?" | you said: "+said.slice(0,200):"");
  else if(s.indexOf("you looked back through your memory")===0&&said&&said.charAt(0)!=="(") got="you remembered: "+said.slice(0,200);
  if(!got) return;
  _gbSess.push({t:Date.now(),txt:got});
  if(_gbSess.length>GB_SESS_MAX) _gbSess.shift();
}catch(e){} }
/* Called from inside the reply handler, so the follow-up is never started there: the turn that
   asked must finish first (its trace, its speech, its visual work), and a follow-up that cannot
   run hands its find to the person's next turn instead of leaving "let me think back" unanswered. */
function gbRecallAsk(q,kind,extra){
  var now=Date.now(), r, ev, tries=0, words=String(q||"").replace(/["\s]+/g," ").trim().slice(0,60);
  if(!words||/^omit\b/i.test(words)) return;
  if(kind!=="event"){ logEv("recall_skip",{why:"kind"}); return; }
  if(extra&&extra.recallFollow){ logEv("recall_skip",{why:"follow"}); return; }
  if(now-_gbRecallT<GB_RECALL_GAP_MS){ logEv("recall_skip",{why:"gap"}); return; }
  _gbRecallT=now;
  try{ r=gbRecall(_live,_gbSess,words,now); }catch(e){ r={n:0,text:""}; }
  logEv("recall",{n:r.n});
  var found="you looked back through your memory for \""+words+"\" and "+(r.n?"found:"+r.text:"found nothing about it.");
  _gbRecallLast={t:now,found:found};
  ev="you looked back through your memory for \""+words+"\" (what you found is under WHAT YOU JUST LOOKED UP). now answer your person with it, in your own voice; if it does not answer what they asked, say plainly that you do not remember.";
  function carry(why){ _gbRecallCarry=found; logEv("recall_carry",{why:why}); }
  function go(){
    if(pendingUser) return carry("person");
    var busy=inFlight||paused||document.hidden||Date.now()<coolUntil||(typeof vmBusy==="function"&&vmBusy())||energyState==="asleep";
    if(busy){ if(++tries<120) setTimeout(go,250); else carry("timeout"); return; }
    _gbRecallCarry=found;
    think(ev,"event",{recallFollow:true});
    if(!inFlight) carry("refused");
  }
  setTimeout(go,0);
  return true;
}
/* The line said while it looks: the model's own when it only says it is thinking back, else one of
   these. It has not looked yet, so an answer or "I don't know" there is a guess the find then
   contradicts (field, 2026-11-13: "I found Lydian and Allura" before the find named all four). */
var GB_RECALL_LINES=["Hmm, let me think back.","Let me look back.","One moment, I'm thinking back.","Let me remember."];
function gbRecallLine(say){
  var s=String(say||"").replace(/\s+/g," ").trim();
  if(s&&s.length<=60&&/\b(think|thinking|remember|recall|look back|looking back|let me)\b/i.test(s)
    &&!/\b(not|no|never|but|don'?t|can'?t|cannot|forgot|found|is|are|was|were|it'?s)\b/i.test(s)) return s;
  return GB_RECALL_LINES[Math.floor(Math.random()*GB_RECALL_LINES.length)];
}
/* With recall first in the reply, a search is known before say streams: say is not voiced as it
   arrives, and the reply handler speaks the thinking-back line in its place. */
function gbRecallStreaming(full){
  var m=/"recall"\s*:\s*"([^"]*)/.exec(String(full||""));
  return !!(m&&m[1].trim()&&!/^omit\b/i.test(m[1].trim()));
}
"""

RECALL_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/prompts.js",
        '/* 1005: the "always log" ask',
        _GB_RECALL_PROMPTS_JS + '/* 1005: the "always log" ask',
        1,
    ),
    (
        "v3/prompts.js",
        "    '\"log\":\"one diary-worthy line, OMIT for routine moments\",' +",
        "    '\"log\":\"one diary-worthy line, OMIT for routine moments\",' +\n"
        "    '\"fact\":\"OMIT unless your person just told you something worth keeping about"
        " their life, people, plans or likes: one plain line for your fact book (lunch today:"
        " a chalupa and a taco)\",' +",
        1,
    ),
    # recall comes before say, so the streamed voice learns of a search before it speaks the line
    # (field, 2026-11-13: "I found Lydian and Allura" and "not the place itself" were spoken
    # while the say was streaming, the recall key arriving after them).
    (
        "v3/prompts.js",
        "    '\"say\":\"' + (gbTalkGrown()",
        "    '\"recall\":\"OMIT almost always. when they ask about something from before that"
        " you cannot see above, a few search words (taco bell order) to look through your fact"
        " book, diary and this conversation. it comes first: you have not looked yet, so your say"
        " is then only a short line that you are thinking back, never an answer and never that"
        " you do not know; you answer with what you find next thought\",' +\n"
        "    '\"say\":\"' + (gbTalkGrown()",
        1,
    ),
    (
        "v3/next.html",
        "function traceMoment(u){",
        _GB_RECALL_PAGE_JS + "function traceMoment(u){",
        1,
    ),
    (
        "v3/next.html",
        "else _live.scratch.traces.push(_tr);",
        "else _live.scratch.traces.push(_tr);\n      gbSessAdd(_tr.u,_tr.a);",
        1,
    ),
    (
        "v3/next.html",
        "    var _dl=0; if(o.log&&String(o.log)",
        "    try{ if(typeof o.fact===\"string\"&&gbFactAdd(_live,o.fact))"
        " logEv(\"fact\",{n:_live.facts.length}); if(typeof o.recall===\"string\"&&"
        "gbRecallAsk(o.recall,kind,extra)) o.say=gbRecallLine(o.say); }catch(_fe){}\n"
        "    var _dl=0; if(o.log&&String(o.log)",
        1,
    ),
    (
        "v3/next.html",
        'if(_hreqAtIssue&&typeof MEMLOG_ASK==="string") _gbLive+=MEMLOG_ASK;',
        'if(_gbRecallCarry){ _gbLive+="\\n\\n== WHAT YOU JUST LOOKED UP ==\\n"+_gbRecallCarry;'
        ' _gbRecallCarry=""; } else if(_hreqAtIssue) _gbLive+=gbRecallKept();'
        ' if(_hreqAtIssue&&typeof MEMLOG_ASK==="string")'
        " _gbLive+=MEMLOG_ASK;",
        1,
    ),
    (
        "v3/soul.js",
        '  s.log = unionByTime(newer.log, s.log, "txt");',
        '  if (Array.isArray(newer.facts)) s.facts = unionByTime(newer.facts,'
        ' Array.isArray(s.facts) ? s.facts : [], "txt");\n'
        '  s.log = unionByTime(newer.log, s.log, "txt");',
        1,
    ),
    # A file the phone exports or imports carries the fact book with the diary; a template
    # (a character to be born fresh) has neither.
    (
        "v3/soul.js",
        "    if (Array.isArray(logIn)) s.log = inTimeOrder(logIn);",
        "    if (Array.isArray(raw.facts)) s.facts = inTimeOrder(raw.facts);\n"
        "    if (Array.isArray(logIn)) s.log = inTimeOrder(logIn);",
        1,
    ),
    (
        "v3/soul.js",
        "soul.goals = rt.goals; soul.scratch = rt.scratch; soul.log = rt.log; soul.bornAt = 1; }",
        "soul.goals = rt.goals; soul.scratch = rt.scratch; soul.log = rt.log; soul.bornAt = 1;"
        " if (Array.isArray(rt.facts)) soul.facts = rt.facts; }",
        1,
    ),
]
PATCHES += RECALL_PATCHES
PATCHES += [
    (
        "v3/next.html",
        "  var s=extractSayPartial(full); if(!s) return; var v=s.value;",
        "  if(gbRecallStreaming(full)) return;\n"
        "  var s=extractSayPartial(full); if(!s) return; var v=s.value;",
        1,
    ),
]

# A persona's name (owner, 2026-11-12: Dave greeted him with "I'm Sprout"). Upstream's seed says
# "I am Sprout" and its own name (goals.selfName) is set only when its person confirms one, so a
# persona named in JBrain woke up as Sprout. A box creature born fresh (no goals yet: a new, reset
# or seeded persona) now starts out with its persona's name; one already awake keeps its own,
# which the owner edits under Soul as "Calls itself".
NAME_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/soul.js",
        'if (!s.goals || typeof s.goals !== "object") s.goals = f.goals;',
        'if (!s.goals || typeof s.goals !== "object") { s.goals = f.goals;'
        " try { if (window.__gbBoxSoul && window.__gbBoxSoul.persona && s.meta"
        ' && typeof s.meta.name === "string") s.goals.selfName = s.meta.name; } catch (e) {} }',
        1,
    ),
]
PATCHES += NAME_PATCHES

# Sleep (owner, 2026-11-12: "dream in between sessions or if a session goes long ... the
# creature can say it's tired and wants to sleep. And then if I give it permission to sleep or I
# exit the session it'll have the dream"). Upstream dreamed every 12 replies, mid-conversation.
# Now a dream comes when the person lets it sleep (a `sleep` reply field), after 5 quiet minutes
# (the visit has ended with the page open), or at the first lull of a new visit when the last one
# was never dreamed (a closed page cannot dream). A long visit makes it sleepy: after 60 replies,
# or 20 and half an hour awake, its turn says so and it asks, once, at a natural pause. A dream
# that follows a conversation sees that conversation and writes what mattered to the diary and
# its facts to the fact book, so the dream is the visit's long-term memory.
_GB_SLEEP_PAGE_JS = r"""var _gbSleepOK=false, _gbSleepAt=0, _gbDreamOnOpen=false, _gbSleepWhy="", _gbBootT=Date.now(),
  GB_SLEEP_IDLE_MS=300000, GB_OPEN_GAP_MS=600000, GB_TIRED_REPLIES=60, GB_TIRED_MIN=20,
  GB_TIRED_MS=1800000, GB_DREAM_SESS=80, GB_SLEEP_HOLD_MS=180000;
/* Permission to sleep lasts a few minutes: a dream that cannot start by then (energy, a busy
   body) must not leave the person's 30 s guard off and a goodnight hanging over a live chat. */
function gbSleepAllowed(){
  if(_gbSleepOK&&Date.now()-_gbSleepAt>GB_SLEEP_HOLD_MS){ _gbSleepOK=false; try{ logEv("sleep_expired",{}); }catch(e){} }
  return _gbSleepOK;
}
/* Sleep ends whatever it was doing: a seek or mission still under way holds every dream back
   ("activity") until the leave lapses. maybeDream asks first, so a seek beat that slipped in is
   ended on the next try; but only while no newer request of its person's is queued, sent, or
   answered on the page (a game started by words) since the leave: what they asked for after the
   goodnight outranks it. */
var _gbSleepReq=0;
function gbSleepGrant(){ _gbSleepOK=true; _gbSleepAt=Date.now(); logEv("sleep_ok",{csd:callsSinceDream});
  try{ _gbSleepReq=typeof _humanReqN==="number"?_humanReqN:0; }catch(e){} }
function gbSleepEnds(){ try{
  if(gbSleepAllowed()&&!pendingUser&&!(_humanT>_gbSleepAt)&&(typeof _humanReqN!=="number"||_humanReqN===_gbSleepReq)&&(seekGoal||actOn||mimicOn)) endActivity("sleep");
}catch(e){} }
/* The visit's lines not yet dreamed ride the soul (scratch.visit), so a dream after a reload or
   on the next day's open still sees the conversation it should keep; a landed dream clears them,
   so no visit is summarised twice. */
function gbVisitKeep(x){ try{
  var sc=_live&&_live.scratch; if(!sc||!x) return;
  var v=Array.isArray(sc.visit)?sc.visit:(sc.visit=[]);
  v.push({t:x.t,txt:x.txt}); if(v.length>GB_DREAM_SESS) v.splice(0,v.length-GB_DREAM_SESS);
}catch(e){} }
function gbVisit(){ var sc=_live&&_live.scratch; return (sc&&Array.isArray(sc.visit))?sc.visit:[]; }
function gbTiredLine(){
  if(gbSleepAllowed()) return "";
  var awake=Date.now()-Math.max(lastDreamT||0,_gbBootT);
  if(!(callsSinceDream>=GB_TIRED_REPLIES||(callsSinceDream>=GB_TIRED_MIN&&awake>=GB_TIRED_MS))) return "";
  return "\n\n== GETTING SLEEPY ==\nyou have been awake and talking a long time since you last slept, and today wants settling into memory. at a natural pause (never in the middle of answering something), tell your person you are getting sleepy and ask if you may sleep. ask once; if they say not yet, carry on and ask again only much later. when they agree or tell you to sleep, say goodnight and set sleep to true.";
}
function gbDreamSession(){
  var v=gbVisit(); if(!v.length) return "";
  return "\n\nIts conversation with its person since it last slept, oldest first:\n"+v.map(function(x){ return "- "+String(x&&x.txt||""); }).join("\n")+
    "\n\nAlso put in your JSON: \"session_summary\":\"1-3 plain sentences for its diary: what mattered in that conversation (people, plans, things its person told it, what they did together)\",\"facts\":[\"up to 5 short plain facts its person told it that are worth keeping\"]";
}
var _GB_KEEP_JUNK=/^(omit|none|nothing|n\/a|null|1-3 plain|up to 5 short)\b/i;
function gbDreamKeep(o){ try{
  var sum=typeof o.session_summary==="string"?o.session_summary.replace(/\s+/g," ").trim():"", n=0, k=0;
  if(_GB_KEEP_JUNK.test(sum)) sum="";
  // soulLog keeps 140 characters a line, so a summary is filed a sentence at a time.
  if(sum) sum.split(/(?<=[.!?])\s+/).filter(Boolean).slice(0,3).forEach(function(x){ soulLog(_live,"📖 "+x); k++; });
  (Array.isArray(o.facts)?o.facts:[]).slice(0,5).forEach(function(f){ if(typeof f==="string"&&!_GB_KEEP_JUNK.test(f.trim())&&gbFactAdd(_live,f)) n++; });
  if(_live&&_live.scratch) _live.scratch.visit=[];
  _gbSleepWhy="";
  logEv("dream_keep",{s:k,f:n});
}catch(e){} }
"""

SLEEP_PATCHES: list[tuple[str, str, str, int]] = [
    ("v3/next.html", "var _humanT=0;", _GB_SLEEP_PAGE_JS + "var _humanT=0;", 1),
    (
        "v3/next.html",
        "  _gbSess.push({t:Date.now(),txt:got});",
        "  _gbSess.push({t:Date.now(),txt:got}); gbVisitKeep(_gbSess[_gbSess.length-1]);",
        1,
    ),
    (
        "v3/next.html",
        "var idle=Date.now()-lastActT, due=callsSinceDream>=12 || (idle>120000 &&"
        " callsSinceDream>=3);",
        "var idle=Date.now()-lastActT, due=gbSleepAllowed() || _gbDreamOnOpen"
        " || (idle>GB_SLEEP_IDLE_MS && callsSinceDream>=3);",
        1,
    ),
    # Its person just let it sleep: their last words are the permission, not a turn to wait out.
    (
        "v3/next.html",
        'Date.now()-_humanT<30000?"human":""',
        'Date.now()-_humanT<30000&&!gbSleepAllowed()?"human":""',
        1,
    ),
    # Permission outranks the cooldown and a failed dream's backoff; dream() keeps every other
    # gate (energy, ownership, pause), and maybeDream has already waited out speech and motion.
    ("v3/next.html", "  if(due) dream(); }", "  if(due) dream(gbSleepAllowed()); }", 1),
    # The goodnight is still being spoken when the reply handler asks, and a new visit's first
    # replies are mostly spoken too; the cadence asks again once the mouth is quiet.
    (
        "v3/next.html",
        "    if(actQuiet) return;",
        "    if((gbSleepAllowed()||_gbDreamOnOpen)&&!_speechBusy()&&!pendingUser){ maybeDream();"
        " if(inFlight||dreaming) return; }\n    if(actQuiet) return;",
        1,
    ),
    ("v3/next.html", "IDLE_DREAM_MS=120000", "IDLE_DREAM_MS=300000", 1),
    ("v3/next.html", "function maybeDream(){ var _dw0=", "function maybeDream(){ try{ gbSleepEnds(); }catch(e){} var _dw0=", 1),
    # "Go to sleep" and "go to bed" are the plainest ways to let it sleep, and upstream's seek
    # words read them as a place to find, so the sleep it was given became a hunt for "sleep".
    (
        "v3/next.html",
        "if(gm&&gm.index<=24) goal=gm[1];",
        "if(gm&&gm.index<=24&&!/^(?:sleep|bed|nap)\\b/i.test(gm[1])) goal=gm[1];",
        1,
    ),
    (
        "v3/next.html",
        "_dreamT0=Date.now();",
        "_dreamT0=Date.now(); if(_gbSleepOK||_gbDreamOnOpen) _gbSleepWhy=_gbSleepOK?\"its person"
        " said it could sleep, after a long time together\":\"a new visit began and it had not"
        " slept since the last one\"; _gbSleepOK=false; _gbDreamOnOpen=false;",
        1,
    ),
    (
        "v3/next.html",
        '"\\n\\nWhy it sleeps now: a quiet stillness came over it";',
        '"\\n\\nWhy it sleeps now: "+(_gbSleepWhy||"a quiet stillness came over it")'
        "+gbDreamSession();",
        1,
    ),
    # The dream's own summaries are not lived evidence for the next dream's identity work.
    (
        "v3/next.html",
        'return _t.indexOf("last night I dreamed of")!==0 && _t.indexOf("🌙 dreamed")!==0;',
        'return _t.indexOf("last night I dreamed of")!==0 && _t.indexOf("🌙 dreamed")!==0'
        ' && _t.indexOf("📖")!==0;',
        1,
    ),
    (
        "v3/next.html",
        "var o=jparse(raw); if(o){ applyDream(o,lived); }",
        "var o=jparse(raw); if(o){ applyDream(o,lived); gbDreamKeep(o); }",
        1,
    ),
    # The summary and facts need room beside the identity work.
    (
        "v3/next.html",
        "brainOverride(orCfg.dream,true),700,90000)",
        "brainOverride(orCfg.dream,true),1000,90000)",
        1,
    ),
    ("v3/next.html", "if(_gbRecallCarry){", "_gbLive+=gbTiredLine(); if(_gbRecallCarry){", 1),
    # A new visit dreams the last one only when that one left conversation undreamed.
    (
        "v3/next.html",
        "if(_csd0>0&&isFinite(_csd0)) callsSinceDream=Math.min(Math.floor(_csd0),60);",
        "if(_csd0>0&&isFinite(_csd0)) callsSinceDream=Math.min(Math.floor(_csd0),60);"
        " var _gbLastV=gbVisit().slice(-1)[0];"
        " if(_gbLastV&&Date.now()-(+_gbLastV.t||0)>GB_OPEN_GAP_MS) _gbDreamOnOpen=true;",
        1,
    ),
    (
        "v3/next.html",
        "o.say=gbRecallLine(o.say); }catch(_fe){}",
        "o.say=gbRecallLine(o.say); if((o.sleep===true||o.sleep===\"true\")&&"
        "kind===\"event\") gbSleepGrant(); }catch(_fe){}",
        1,
    ),
    (
        "v3/prompts.js",
        " a chalupa and a taco)\",' +",
        " a chalupa and a taco)\",' +\n"
        "    '\"sleep\":\"OMIT unless your person just said you may sleep or told you to go to"
        " sleep: then true, with a goodnight in say\",' +",
        1,
    ),
]
PATCHES += SLEEP_PATCHES

# What the quiet beat says, from upstream's live page (Brit, 2026-11-11 "QUIET MOMENT TEXT",
# after GitHub's last snapshot). The old text framed quiet as company kept with the person, so
# a creature left alone sat still; the new one hands it the time to chase its wants with its
# body. Each persona chooses, on by default (owner, 2026-11-12: a persona setting, not
# upstream's ?quiet URL switch, because the phone is only a display): growbot-box.js sets
# __gbBoxExplore from the box and only false brings the old words back. Only the words change:
# the beat's gates, its spacing and our 90 s wait above stay, and the text rides the turn's own
# message, so the cached system prefix is untouched. Upstream's QUIET SETTLE (beats thin out
# the longer nobody acts) is left out: it saves cloud credits, a local beat costs none, and it
# would hold back exactly the exploring a creature left alone is meant to do.
GB_EXPLORE_TXT = (
    "QUIET — nothing has happened for a while. This time is yours: you may reach for one of"
    " your wants with your own body."
)
QUIET_TEXT_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        "var GB_QUIET_AFTER_MS=90000;",
        f'var GB_QUIET_AFTER_MS=90000;\nvar GB_EXPLORE_TXT="{GB_EXPLORE_TXT}";',
        1,
    ),
    (
        "v3/next.html",
        'think("QUIET COMPANY — you are awake together.',
        'think(window.__gbBoxExplore!==false?GB_EXPLORE_TXT:"QUIET COMPANY — you are awake'
        ' together.',
        1,
    ),
]
PATCHES += QUIET_TEXT_PATCHES

# Upstream leashed a cloud transcriber at 8 s and its backstop at 12 s, and handed a failed
# clip to the other cloud. Both lanes are the box's one whisper here, so a leash or a hand-off
# only re-sends the same clip to the queue it is waiting in (whisper-server finishes an
# abandoned job before the next); the box's own limit (serve.py STT_TIMEOUT_S) and its reason
# (gbBoxEars) decide instead.
STT_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        "var _pri=!!STT_GROQ, _lease=_pri?8000:12000;   // 0827: SHORT leash on the primary,"
        " LONG on the backstop.",
        "var _pri=!!STT_GROQ, _lease=65000;   // the box's whisper; it times itself out at 60 s."
        " (0827 upstream: SHORT leash on the primary, LONG on the backstop.)",
        1,
    ),
    (
        "v3/next.html",
        "var _pri=!STT_GROQ, _lease=_pri?8000:12000;   // 0827: same role-based rule as the"
        " groq lane above",
        "var _pri=!STT_GROQ, _lease=65000;   // the box's whisper, as the groq lane above",
        1,
    ),
    (
        "v3/next.html",
        "if(STT_GROQ){ diTranscribe(blob,type,meta); return; }",
        "",
        1,
    ),
    (
        "v3/next.html",
        "if(!STT_GROQ){ groqTranscribe(blob,type,meta); return; }",
        "",
        1,
    ),
]
PATCHES += STT_PATCHES

# The brain panel's settings, scrubbed of what a box-run creature has no use for (owner,
# 2026-11-11: "some of the settings no longer are applicable to us because we run only on the
# local model"). The box picks the model, there are no credits or keys, the soul lives on the box
# (personas, its own backups), and the logs link and the old full panel are cloud journaling and
# the cloud dials. Hidden behind window.__gbBox (growbot-box.js), so the page without the shim
# keeps upstream's panel; the rows that stay are the body, face following, eye, voice and low
# power.
_BOX = "if(!window.__gbBox)"
SCRUB_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        'h+=wRow("dial","brain","","the brain dial"',
        _BOX + 'h+=wRow("dial","brain","","the brain dial"',
        1,
    ),
    (
        "v3/next.html",
        'h+=wRow("soul","soul","#9A86FF","soul"',
        _BOX + 'h+=wRow("soul","soul","#9A86FF","soul"',
        1,
    ),
    (
        "v3/next.html",
        'h+=wRow("byok","brain","#FFB24D","your own key"',
        _BOX + 'h+=wRow("byok","brain","#FFB24D","your own key"',
        1,
    ),
    ("v3/next.html", 'h+=hatch("mind");', _BOX + 'h+=hatch("mind");', 1),
    (
        "v3/next.html",
        "function brainLogsSection(){",
        'function brainLogsSection(){ if(window.__gbBox) return "";',
        1,
    ),
    (
        "v3/next.html",
        "function moveMeRow(){",
        'function moveMeRow(){ if(window.__gbBox) return "";',
        1,
    ),
    # Advanced held only upstream's placeholders ("the real timings land here with the
    # update") and the cloud logs link.
    (
        "v3/next.html",
        "  h+='<button type=\"button\" id=\"gbkAdvTog\"",
        "  " + _BOX + "h+='<button type=\"button\" id=\"gbkAdvTog\"",
        1,
    ),
    (
        "v3/next.html",
        "if(W.adv){ h+='<div id=\"gbkAdv\"",
        "if(W.adv&&!window.__gbBox){ h+='<div id=\"gbkAdv\"",
        1,
    ),
    # The credits tab: two tabs, not three.
    (
        "v3/next.html",
        'gap:6px"></button>',
        'gap:6px" data-gb-cloud="1"></button>',
        1,
    ),
    # A creature last left on the credits tab would reopen on the hidden room.
    (
        "v3/next.html",
        'if(_t==="s"||_t==="c"||_t==="t") bTab=_t;',
        'if(_t==="s"||(_t==="c"&&!window.__gbBox)||_t==="t") bTab=_t;',
        1,
    ),
]
# More of the same (owner, 2026-11-12: "the cell phone should just be a display", and "remove
# all the stuff that doesn't apply to our implementation"): My thoughts' soul download (the box
# keeps the soul, and JBrain exports it) and its visual memories "on this device". The full
# panel's MIND rows are hidden by growbot-box.js's style.
SCRUB_PATCHES += [
    (
        "v3/next.html",
        '  h+=bCard(bHead("soul file")+',
        "  " + _BOX + 'h+=bCard(bHead("soul file")+',
        1,
    ),
    (
        "v3/next.html",
        "Visual memories · on this device</button>');",
        "'+(window.__gbBox?\"Visual memories\":\"Visual memories · on this device\")+'</button>');",
        1,
    ),
]
PATCHES += SCRUB_PATCHES

# The creature's chrome, fitted to a box-run creature (owner, 2026-11-12).
CHROME_PATCHES: list[tuple[str, str, str, int]] = [
    # "I want to keep the bottom bar visible all the time": upstream fades the bar (brain,
    # pause, mute and the type box) 9 s after the last touch. Its clock still runs and still
    # re-wakes the bar; it only never fades it.
    ("v3/next.html", "setFade(true); },9000); }", "},9000); }", 1),
    # The keyboard: Android Chrome by default lays it over the page and pans the page up to the
    # focused type box, which scrolls the status strip and the top of the face off screen.
    # resizes-content shrinks the layout viewport instead, so every fixed layer (the face's
    # canvas, the strip, the bar) fits the space above the keyboard and the face redraws
    # smaller. iOS ignores the key.
    (
        "v3/next.html",
        "user-scalable=no, viewport-fit=cover\"/>",
        "user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content\"/>",
        1,
    ),
    # The wake gate's card under the wake button is upstream's store: "adopted, i'm yours",
    # its members area and a soul download for adopters, or "adopt me · $20". The box
    # answers /api/device with a pass, so it always drew the adopted card; here the creature
    # is the household's, its soul lives on the box, and there is no store. The card stays
    # hidden, and the storage guard above it still runs.
    (
        "v3/next.html",
        "var a=$(\"wgAdopt\"); if(!a||!window._wakeGateUp) return;",
        "var a=$(\"wgAdopt\"); if(!a||!window._wakeGateUp||window.__gbBox) return;",
        1,
    ),
]
PATCHES += CHROME_PATCHES

# Which camera is the creature's eye, front or back (owner, 2026-11-12). Upstream keeps the
# choice in localStorage gb_eye, set by the brain panel's eye button; the box keeps gb_eye in
# the persona's settings record, so the owner can change it from the PWA and the creature
# switches where it stands. Upstream killed a free-roaming `look` verb because the model
# flipped lenses constantly and lost track of what it saw, so the creature's own `eye` is
# heeded only on a turn that answers something its person said, and a lens it already has
# does nothing.
EYE_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/prompts.js",
        "    '\"coat\":\"OMIT usually",
        "    '\"eye\":\"OMIT almost always — front or back, ONLY when your person asks you to"
        " look with your other camera: front is the screen side, facing them; back looks out"
        " the back of your head at the world. It stays until they ask again\",' +\n"
        "    '\"coat\":\"OMIT usually",
        1,
    ),
    (
        "v3/next.html",
        "function flipEye(target){",
        "function gbEyeTo(to,by){ if((to!==\"user\"&&to!==\"environment\")||to===camFacing)"
        " return false; flipEye(to);"
        " try{ var _be=$(\"bpmEye\"); if(_be) _be.textContent=\"👁 eye: \"+(camFacing===\"user\""
        "?\"front (screen side)\":\"back\"); }catch(e){}"
        " try{ logEv(\"eye_set\",{to:camFacing,by:by}); }catch(e){} return true; }\n"
        "window.addEventListener(\"gb-box-prefs\",function(e){"
        " var c=e&&e.detail&&e.detail.changed;"
        " if(c&&Object.prototype.hasOwnProperty.call(c,\"gb_eye\"))"
        " gbEyeTo(c.gb_eye===\"environment\"?\"environment\":\"user\",\"owner\"); });\n"
        "function flipEye(target){",
        1,
    ),
    (
        "v3/next.html",
        '_hueOf(o.coat))) logEv("coat",{c:window.gbFace.colour()});',
        '_hueOf(o.coat))) logEv("coat",{c:window.gbFace.colour()});'
        ' if(_hreqAtIssue&&!dreaming&&(o.eye==="front"||o.eye==="back"))'
        ' gbEyeTo(o.eye==="back"?"environment":"user","self");',
        1,
    ),
    # A phone that cached the old schema loads the one that offers `eye`.
    (
        "v3/next.html",
        '<script src="/v3/prompts.js?v=1008-motion-box-lang"',
        '<script src="/v3/prompts.js?v=1008-motion-box-lang-eye"',
        1,
    ),
]
PATCHES += EYE_PATCHES

# A phone that cached the old prompts loads the one with the talk style (TALK_PATCHES).
PATCHES += [
    (
        "v3/next.html",
        '<script src="/v3/prompts.js?v=1008-motion-box-lang-eye"',
        '<script src="/v3/prompts.js?v=1008-motion-box-lang-eye-talk-recall-sleep-recall3"',
        1,
    ),
    # And the soul code that names a fresh persona (NAME_PATCHES).
    (
        "v3/next.html",
        '<script src="/v3/soul.js?v=0624c"',
        '<script src="/v3/soul.js?v=0624c-name"',
        1,
    ),
]

# The picture is read while the person is still talking (owner, 2026-11-13). A spoken turn's
# prefill (the history, this turn's state and its camera frames, ~2,400 tokens) used to start
# only once the transcript landed. Now, once the person has been talking for 0.6 s, the page
# sends the box that turn minus their words, ending in a placeholder message and asking for one
# token, so the engine reads it while they speak; the turn then sends the same messages byte for
# byte with their words as its last message, and the engine resumes from where the warm-up
# stopped. Measured on the box with logged turns: 7.8 s of prefill fell to 1.9 s. The engine
# keeps a reusable checkpoint only at the start of a request's last message, which is why the
# words ride in a message of their own and the warm-up ends in "." rather than stopping short.
# A warm-up is used once, by the next spoken turn, under 12 s old, and only when nothing it
# was built from has moved: the history, the system text, the eye, the state block (the
# scratchpad, goal and language lines; its self-stats compared without their numbers) and the
# picture-memory line. A warm-up's newest frame must still be under 10 s old, the page's rule
# for any frame, and the turn sees and remembers those same frames. What the warm-up could not
# know (a recall, an imitation guide) rides with the words. Anything else runs as before.
_GB_WARM_JS = r"""var _gbWokeT=0, GB_WOKE_HOLD_MS=30000;
function gbOpenDreamDue(){ return _gbDreamOnOpen&&!window._wakeGateUp&&Date.now()-_gbWokeT>=GB_WOKE_HOLD_MS; }
var _gbWarm=null, _gbWarmBusy=false, _gbWarmT=0, _gbWarmMiss=0, _gbWarmOff=false, GB_WARM_MISS=3, GB_WARM_MAX_MS=12000, GB_WARM_GAP_MS=5000, GB_WARM_VOICE_MS=600, GB_WARM_FUSE_MS=30000;
function gbTraceReply(t){ var a=String(t.a||""), note=a==="(stayed quiet)"||a.indexOf("(brain hiccup")===0, r={say:note?"":a}; if(t.e) r.emotion=t.e; return JSON.stringify(r)+(note?"\n"+a:"")+(t.d?"\n(did: "+t.d+")":""); }
function gbHist(now){ var tr=gbTalkTraces(_live.scratch.traces), msgs=[], i;
  for (i=0;i<tr.length;i++){ msgs.push({role:"user",content:(tr[i].t?("["+Math.max(0,Math.round((now-tr[i].t)/1000))+"s ago] "):"")+traceMoment(tr[i].u)}); msgs.push({role:"assistant",content:gbTraceReply(tr[i])}); }
  return msgs; }
function gbWarmKey(){ var t=(_live&&_live.scratch&&_live.scratch.traces)||[], l=t[t.length-1]; return t.length+":"+(l?(l.t||0)+":"+gbTraceReply(l):"")+":"+(camOn?1:0)+":"+camFacing; }
function gbSoulView(){ var v=(MODE==="phone")?Object.assign({},_live,{body:null}):_live;
  if(MODE!=="phone"&&_wheels&&v.body&&!((v.body.walk&&v.body.walk.present===false)||/wheel/i.test(String(v.body.id||"")+" "+String((v.body.geometry&&v.body.geometry.drive)||""))))
    v=Object.assign({},v,{body:Object.assign({},v.body,{walk:Object.assign({},v.body.walk||{},{present:false})})});
  return v; }
function gbWarmUp(){ var now=Date.now();
  if(window._wakeGateUp||_gbWarmOff||_gbWarmBusy||now-_gbWarmT<GB_WARM_GAP_MS||typeof window.gbBoxLive!=="function") return;
  if(_gbWarm&&_gbWarm.utt!==vadStartT){ _gbWarm=null; _gbWarmMiss++; }
  if(_gbWarmMiss>=GB_WARM_MISS) return;
  if(inFlight||!orCfg||!_live||now<coolUntil||paused||document.hidden||dreaming||energyState==="asleep"||_gbRecallCarry) return;
  if(!soulOwned()||(typeof vmBusy==="function"&&vmBusy())||llmCfg()) return;
  _gbWarmT=now;
  try{
    var nf=(frameDiff>3||_rideOn||now<selfMoveUntil)?3:1, cut=now-10000, frames=camOn?eyeBuf.filter(function(f){ return f.t>=cut; }).slice(-nf):[], parts=[];
    if(camOn&&!frames.length) return;
    var eyeWord=(camFacing==="environment")?"your rear eye, the back of your head":"your forward eye, the screen side";
    frames.forEach(function(f){ var ago=Math.max(0,Math.round((now-f.t)/1000));
      parts.push({type:"text",text:"[camera ("+eyeWord+") — "+(ago<=1?"now]":ago+"s ago]")});
      parts.push({type:"image_url",image_url:{url:"data:image/jpeg;base64,"+f.j}}); });
    var vmc=(typeof vmTurnContext==="function"&&typeof vmStreamSpeech==="function"&&typeof vmReplyGuard==="function")?{user:"·",asked:false}:null;
    var sys=buildSystem(gbSoulView(),{language:_convLang})+"\n\nSPEECH STYLE: never use an em dash in what you say, write the pause as a comma or a period."+moveGuideText();
    var vm=(typeof vmPrompt==="function"?vmPrompt(vmc):""), ss=selfStats(), bl=buildSystem.live||"", mg=moveGuideText.live||"", live=bl+mg+"\n\n"+ss+vm, tired=gbTiredLine(), memlog=(typeof MEMLOG_ASK==="string")?MEMLOG_ASK:"";
    live+=tired+memlog;
    sys+=(typeof gaitList==="function"&&gaitList.fixed||"")+(typeof selfStats==="function"&&selfStats.fixed||"")+(typeof vmPrompt==="function"&&vmPrompt.fixed||"");
    var pre={role:"user",content:parts.length?parts:""};
    if(!window.gbBoxLive([pre],live)) return;
    var hist=gbHist(now), body=_mkBody(brainOverride(orCfg.active||orCfg.fast,false),1,[{role:"system",content:sys}].concat(hist,[pre,{role:"user",content:"."}]));
    var hdr={"content-type":"application/json","x-gb-warm":"1"}; if(pbDevTok) hdr["x-pb-device"]=pbDevTok;
    var ctl=window.AbortController?new AbortController():null, fuse=setTimeout(function(){ try{ if(ctl) ctl.abort(); }catch(e){} },GB_WARM_FUSE_MS);
    var w=_gbWarm={t:now,utt:vadStartT,key:gbWarmKey(),sys:sys,hist:hist,pre:pre,frames:frames,nf:frames.length,ft:frames.length?frames[frames.length-1].t:0,bl:bl,mg:mg,ss:gbShape(ss),vm:vm,tired:tired,memlog:memlog}; _gbWarmBusy=true;
    try{ logEv("warm",{nf:frames.length}); }catch(e){}
    function drop(why){ w.bad=true; if(_gbWarm===w) _gbWarm=null; try{ logEv("warm_fail",{w:why}); }catch(e){} }
    fetch("/api/llm?target=openrouter",{method:"POST",headers:hdr,body:JSON.stringify(body),signal:ctl?ctl.signal:undefined})
      .then(function(r){ if(!r.ok){ drop("http "+r.status); if(r.status===409) _gbWarmOff=true; } return r.text(); })
      .then(function(tx){ var j=null; try{ j=JSON.parse(tx); }catch(e){} if(!w.bad&&(!j||j.error)) drop("body"); })
      .catch(function(){ if(!w.bad) drop("net"); })
      .then(function(){ clearTimeout(fuse); _gbWarmBusy=false; });
  }catch(e){ _gbWarm=null; _gbWarmBusy=false; }
}
function gbWarmFresh(){ return !!(_gbWarm&&_gbWarm.utt===vadStartT&&Date.now()-_gbWarm.t<GB_WARM_MAX_MS); }
function gbShape(s){ return String(s||"").replace(/\d+(?:\.\d+)?/g,"#"); }
function gbWarmSkip(w,why){ try{ logEv("warm_skip",{w:why,age:Date.now()-w.t}); }catch(e){} return null; }
function gbWarmTake(kind,extra,frames){
  if(kind!=="event"||!(extra&&extra.humanReq)) return null;
  var w=_gbWarm, now=Date.now(); _gbWarm=null; _gbWarmMiss=0;
  if(!w) return null;
  if(now-w.t>GB_WARM_MAX_MS) return gbWarmSkip(w,"old");
  if(w.key!==gbWarmKey()) return gbWarmSkip(w,"moved");
  if(w.nf<frames.length||(w.nf>0)!==(frames.length>0)) return gbWarmSkip(w,"frames");
  if(w.nf&&now-w.ft>10000) return gbWarmSkip(w,"stale");
  return w; }
function gbWarmFits(w,c){
  var why=w.bad?"failed":c.sys!==w.sys?"sys":c.bl!==w.bl?"state":c.mg!==w.mg?"goal":gbShape(c.ss)!==w.ss?"stats":c.vm!==w.vm?"vm":c.blind?"blind":"";
  if(why){ gbWarmSkip(w,why); return false; } return true; }
function gbWarmMsgs(w,live,liveN,userMsg){
  var late=live.slice(liveN).replace(w.tired,"").replace(w.memlog,"").replace(/^\s+/,"");
  return w.hist.concat([w.pre,{role:"user",content:(late?late+"\n\n== THIS MOMENT ==\n":"")+userMsg}]); }
"""
WARM_PATCHES: list[tuple[str, str, str, int]] = [
    ("v3/next.html", "function think(event,kind,extra){", _GB_WARM_JS + "function think(event,kind,extra){", 1),
    # An autonomous thought sent while the person talks would overwrite the engine's warm-up.
    # Only while the utterance that started the warm-up is open, and warm-ups stop after three
    # utterances in a row that never became a turn: a television can start them too, and must
    # not silence the creature.
    (
        "v3/next.html",
        '  var now=Date.now();\n  if(energyState==="asleep"){ if(kind!=="event"',
        '  if(kind!=="event"&&vadOn&&gbWarmFresh()) return;\n'
        '  var now=Date.now();\n  if(energyState==="asleep"){ if(kind!=="event"',
        1,
    ),
    (
        "v3/next.html",
        '  function gbTraceReply(t){ var a=String(t.a||""), note=a==="(stayed quiet)"||'
        'a.indexOf("(brain hiccup")===0, r={say:note?"":a}; if(t.e) r.emotion=t.e; return'
        ' JSON.stringify(r)+(note?"\\n"+a:"")+(t.d?"\\n(did: "+t.d+")":""); }\n',
        "",
        1,
    ),
    (
        "v3/next.html",
        "  for(i=0;i<tr.length;i++){ msgs.push({role:\"user\",content:(tr[i].t?(\"[\"+Math.max(0,"
        "Math.round((now-tr[i].t)/1000))+\"s ago] \"):\"\")+traceMoment(tr[i].u)});"
        " msgs.push({role:\"assistant\",content:gbTraceReply(tr[i])}); }",
        "  msgs=gbHist(now);",
        1,
    ),
    (
        "v3/next.html",
        '_gbLive=(buildSystem.live||"")+(moveGuideText.live||"")+"\\n\\n"+selfStats()+'
        '(typeof vmPrompt==="function"?vmPrompt(_vmTurn):"");',
        '_gbVm=(typeof vmPrompt==="function"?vmPrompt(_vmTurn):""), _gbSS=selfStats(),'
        ' _gbLive=(buildSystem.live||"")+(moveGuideText.live||"")+"\\n\\n"+_gbSS+_gbVm,'
        ' _gbLiveN=_gbLive.length;',
        1,
    ),
    (
        "v3/next.html",
        ' if(typeof window.gbBoxLive!=="function"||!window.gbBoxLive(msgs,_gbLive)) sys+=_gbLive;',
        " if(_gw&&gbWarmFits(_gw,{sys:sys,bl:buildSystem.live||\"\",mg:moveGuideText.live||\"\","
        "ss:_gbSS,vm:_gbVm,blind:parts.length-1!==frames.length*2})){"
        " msgs=gbWarmMsgs(_gw,_gbLive,_gbLiveN,userMsg);"
        ' try{ logEv("warm_use",{age:Date.now()-_gw.t,nf:_gw.nf}); }catch(_we){} }\n'
        '  else if(typeof window.gbBoxLive!=="function"||!window.gbBoxLive(msgs,_gbLive)) sys+=_gbLive;',
        1,
    ),
    # The turn sees the frames the warm-up sent, so what it remembers is what the model saw.
    (
        "v3/next.html",
        "  var _lumAt=(luma>=0?Math.round(luma):-1),",
        "  var _gw=gbWarmTake(kind,extra,frames); if(_gw) frames=_gw.frames;\n"
        "  var _lumAt=(luma>=0?Math.round(luma):-1),",
        1,
    ),
    (
        "v3/next.html",
        "  if(now-vadVoiceT > (_pcmUtt&&_pcmUtt.gbId?",
        "  if(voice&&now-vadStartT>=GB_WARM_VOICE_MS) gbWarmUp();\n"
        "  if(now-vadVoiceT > (_pcmUtt&&_pcmUtt.gbId?",
        1,
    ),
]
PATCHES += WARM_PATCHES

# Nothing reaches the model while the sleep screen is up (owner, 2026-11-13: "the local llm
# shouldn't process anything until I say wake up"). The dream a new visit owes (SLEEP_PATCHES)
# ran on that screen, before the wake tap; now it, and any thought, waits for the tap, and the
# owed dream waits a further 30 s after it, so the creature does not say hello and fall asleep.
WAKE_GATE_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        'window._wakeGateUp=false; _wg.style.display="none";',
        'window._wakeGateUp=false; _gbWokeT=Date.now(); _wg.style.display="none";',
        1,
    ),
    (
        "v3/next.html",
        "due=gbSleepAllowed() || _gbDreamOnOpen",
        "due=gbSleepAllowed() || gbOpenDreamDue()",
        1,
    ),
    (
        "v3/next.html",
        "if((gbSleepAllowed()||_gbDreamOnOpen)&&",
        "if((gbSleepAllowed()||gbOpenDreamDue())&&",
        1,
    ),
    (
        "v3/next.html",
        'function _askWaitWhy(q){\n  if(paused) return "paused";',
        'function _askWaitWhy(q){\n  if(window._wakeGateUp) return "gate";\n  if(paused) return "paused";',
        1,
    ),
    (
        "v3/next.html",
        'function think(event,kind,extra){\n  if(typeof vmBusy==="function"&&vmBusy()) return;',
        'function think(event,kind,extra){\n  if(window._wakeGateUp) return;\n'
        '  if(typeof vmBusy==="function"&&vmBusy()) return;',
        1,
    ),
    (
        "v3/next.html",
        'function dream(force){\n  if(typeof vmBusy==="function"&&vmBusy()) return;',
        'function dream(force){\n  if(window._wakeGateUp) return;\n'
        '  if(typeof vmBusy==="function"&&vmBusy()) return;',
        1,
    ),
]
PATCHES += WAKE_GATE_PATCHES

# A streamed utterance's last piece is often only the breath after the last word, and the speech
# model hears nothing in it. That piece used to show "didn't catch that" while the words before
# it reached the creature whole (owner, 2026-11-13: "showing an error message that it couldn't
# understand it"). An empty piece is now silent; the utterance says so only when every piece
# came back empty (_gbFlush above).
PIECE_PATCHES: list[tuple[str, str, str, int]] = [
    # A piece the speech model answered with nothing usable marks its utterance; the transcript
    # filters' own messages stay quiet for a piece (every one of heardText's toasts is a drop).
    (
        "v3/next.html",
        "function _heardDrop(why,meta){ if(meta&&meta.gbPiece) _gbPiece(meta.gbPiece,\"\",meta);",
        "function _heardDrop(why,meta){ if(meta&&meta.gbPiece){ var _gbG=_gbUtts[meta.gbPiece.id];"
        " if(_gbG&&why!==\"echo\"&&(!_gbG.why||_gbG.why===\"empty\")) _gbG.why=why;"
        " _gbPiece(meta.gbPiece,\"\",meta); }",
        1,
    ),
    (
        "v3/next.html",
        "function heardText(txt,meta,detectedLang){",
        "function heardText(txt,meta,detectedLang){"
        " var toast=(meta&&meta.gbPiece)?function(){}:window.toast;",
        1,
    ),
]
PATCHES += PIECE_PATCHES

# The speaker and the mic, as two cells on the creature's bottom rail (owner, 2026-11-13: "two
# mute buttons, one for the audio from the creature, the other for the microphone"). Speaker is
# upstream's sound mute, named for what it silences. The mic cell steps through three modes,
# kept per device in localStorage gb_mic_mode: open (upstream: it hears whenever the ear is
# on), push (only while a finger holds the mic button or the screen) and muted (the ear stays
# on, so the mic is not given up, but nothing it hears opens an utterance, starts a warm-up,
# fires a reflex or interrupts the creature). Muting is not turning the ear off: that is still
# a tap on the mic button, and the slashed red chip. A held clip is upstream's own manual clip
# (talkStart/talkStop), so push to talk sends exactly what the old hold did.
_GB_MIC_JS = r"""function gbMicSet(m){ if(m!=="push"&&m!=="muted") m="open"; if(m===GB_MIC) return;
  GB_MIC=m; try{ localStorage.gb_mic_mode=m; }catch(e){}
  _gbPtt=false; _gbPttStop=false;
  if(m!=="open"&&vadOn) vadCancel();
  if(m==="muted"&&recording) talkAbort();
  try{ logEv("mic_mode",{m:m}); }catch(e){}
  toast(m==="open"?"open mic. just talk":m==="push"?"push to talk. hold the screen or the mic button while you speak":"mic muted. I can't hear you, but you can type"); }
var _gbPtt=false, _gbPttStop=false, _gbPttUntil=0, _gbPttId=null, _gbPttTry=0, _gbPttWait=false;
function gbPttDown(e){ if(GB_MIC!=="push"||_gbPtt||window._wakeGateUp||paused) return;
  _gbPtt=true; _gbPttStop=false; _gbPttId=e&&e.pointerId!=null?e.pointerId:null; _gbPttTry=Date.now()+12000;
  if(!recording){ _gbPttWait=!(micStream&&micHealthy()); talkStart(); if(_gbPttWait) setTimeout(gbPttRetry,150); } }
// A press that lands while another mic ask is in flight (the host's own recovery ask on the same
// tap) has its talkStart dropped; once that stream is there, the hold still becomes a clip. One
// retry only: a talkStart that had a stream and still did not record has said why.
function gbPttRetry(){ if(!_gbPtt||recording||!_gbPttWait||Date.now()>_gbPttTry) return;
  if(micStream&&!_micInFlight&&micHealthy()){ _gbPttWait=false; talkStart(); return; } setTimeout(gbPttRetry,150); }
// Hidden mid-press with the mic still being asked for: the ask is made stale, so a stream that
// lands on the way back starts no clip (upstream's hide handler only stops a clip under way).
function gbPttHide(){ if((_gbPtt||_gbPttStop)&&!recording&&_micInFlight){ _micGen++; _micInFlight=false; }
  _gbPtt=false; _gbPttStop=false; }
function gbPttUp(e){ if(!_gbPtt) return; if(e&&_gbPttId!=null&&e.pointerId!==_gbPttId) return; _gbPtt=false;
  if(recording){ talkStop(); return; }
  _gbPttStop=true; _gbPttUntil=Date.now()+13000; setTimeout(gbPttSettle,40); }
function gbPttSettle(){ if(!_gbPttStop||_gbPtt) return;
  if(recording){ _gbPttStop=false; talkStop(); return; }
  if(Date.now()<_gbPttUntil) setTimeout(gbPttSettle,40); else _gbPttStop=false; }
document.addEventListener("pointerup",gbPttUp);
document.addEventListener("pointercancel",gbPttUp);
document.addEventListener("visibilitychange",function(){ if(document.hidden) gbPttHide(); });
window.addEventListener("pagehide",gbPttHide);
document.addEventListener("contextmenu",function(e){ if(GB_MIC==="push"&&e.target&&e.target.closest&&(e.target.closest("#gbkMic")||!e.target.closest(GB_PTT_NOT))) e.preventDefault(); });
var GB_PTT_NOT="input,textarea,select,button,#editPanels,#feed,#modebar,#brainPanel,#sgNudge,#gbk,#gbkBrainP,#gbkBack";
"""
_GB_MIC_ICONS = r"""function gbSpk(on){ return '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9h4l5-4v14l-5-4H4z"></path>'+(on?'<path d="M16.5 8.5a5 5 0 0 1 0 7"></path><path d="M19 6a8.5 8.5 0 0 1 0 12"></path>':'<path d="M17 9l5 6"></path><path d="M22 9l-5 6"></path>')+'</svg>'; }
function gbMicIcon(m){ var mic='<rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"></path><path d="M12 18v3"></path>';
 var b=m==="push"?'<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V12"></path><path d="M11 11.5V4a1.5 1.5 0 0 1 3 0v7.5"></path><path d="M14 11.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-.5a6 6 0 0 1-5-2.7L3.6 14a1.5 1.5 0 0 1 2.5-1.6L8 15"></path>':mic+(m==="muted"?'<line x1="3" y1="3" x2="21" y2="21" stroke-width="2.4"></line>':'');
 return '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+b+'</svg>'; }
var GB_MIC_MODES=["open","push","muted"], GB_MIC_LABEL={open:"open mic",push:"push talk",muted:"mic muted"};
"""
MIC_PATCHES: list[tuple[str, str, str, int]] = [
    (
        "v3/next.html",
        'var MUTED=(function(){ try{ return localStorage.gb_mute==="1"; }catch(e){ return false; } })();',
        'var MUTED=(function(){ try{ return localStorage.gb_mute==="1"; }catch(e){ return false; } })();\n'
        'var GB_MIC=(function(){ try{ var v=localStorage.gb_mic_mode; return v==="push"||v==="muted"?v:"open"; }'
        'catch(e){ return "open"; } })();',
        1,
    ),
    ("v3/next.html", "function vadCancel(){ vadOn=false;", _GB_MIC_JS + "function vadCancel(){ vadOn=false;", 1),
    # Outside open mode the ear still measures the room, so its floor is right the moment the
    # mode comes back, but nothing it hears goes further. A held clip is the one way in.
    (
        "v3/next.html",
        "  if(vadOn||recording){ if(micRms>_clipMax) _clipMax=micRms;",
        '  if(GB_MIC!=="open"&&!recording){ noiseFloor+=(micRms-noiseFloor)*(micRms<noiseFloor?0.2:0.005);'
        " micRms=0; hum.pitch=-1; _ambOn=false; _ambT=0; _ambLowT=0; _ambDuty=0; _ambDuty2=0;"
        " if(vadOn) vadCancel(); setTimeout(earTick,120); return; }\n"
        "  if(vadOn||recording){ if(micRms>_clipMax) _clipMax=micRms;",
        1,
    ),
    # In push mode a press anywhere on the creature is a press of the talk button, not a pet.
    (
        "v3/next.html",
        "tapping the CREATURE is (audit: brain-panel touches were petting)\n",
        "tapping the CREATURE is (audit: brain-panel touches were petting)\n"
        '    if(GB_MIC==="push"&&!window._wakeGateUp&&!paused&&e.isPrimary!==false){ gbPttDown(e); return; }\n',
        1,
    ),
    (
        "v3/next.html",
        'toast("tap, speak, then tap again to send")',
        'toast(GB_MIC==="push"?"hold while you speak, then let go":"tap, speak, then tap again to send")',
        1,
    ),
    # The rail: four cells, the speaker and the mic's mode beside brain and pause.
    (
        "v3/next.html",
        "#gbkCells{display:grid;grid-template-columns:repeat(3,1fr);",
        "#gbkCells{display:grid;grid-template-columns:repeat(4,1fr);",
        1,
    ),
    (
        "v3/next.html",
        "body:not(.editing) #brainTabBtn{display:none!important}\n</style>",
        "body:not(.editing) #brainTabBtn{display:none!important}\n"
        ".gbkPips{display:flex;gap:3px;margin-top:-3px}.gbkPips i{display:block;width:9px;height:3px;"
        "background:rgba(232,246,255,.25)}.gbkPips i.on{background:currentColor}\n"
        "#gbkMic.gbkPtt{width:auto;padding:0 12px;gap:8px;display:flex;align-items:center;"
        "font:400 11px 'Departure Mono',ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;"
        "background:#16243A;color:#8FB8FF;border:1px solid #8FB8FF}\n"
        "#gbkMic.gbkPtt.hold{background:#FFB24D;color:#2E1A00;border-color:#FFB24D}\n"
        "#gbkMic.gbkMuted{color:#FFB24D}\n"
        "body.gbkPushMode #face{touch-action:none}\n"
        "body.gbkPushMode #liveCard,body.gbkPushMode #face{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}\n"
        "#gbkMic *{pointer-events:none}\n"
        "</style>",
        1,
    ),
    (
        "v3/next.html",
        '<button type="button" class="gbkCell gbkWarn" id="gbkSound"></button>',
        '<button type="button" class="gbkCell gbkWarn" id="gbkSound"></button>\n'
        '    <button type="button" class="gbkCell" id="gbkMicMode"></button>',
        1,
    ),
    ("v3/next.html", "function g(id){return document.getElementById(id);}", _GB_MIC_ICONS + "function g(id){return document.getElementById(id);}", 1),
    (
        "v3/next.html",
        ' setH(mc,hasTxt?snd():((earOff&&typeof micOff==="function")?micOff():mic()));',
        ' var MM=window.GB_MIC||"open", MQ=MM!=="open"&&!hasTxt&&E!=="blocked"&&E!=="pending";\n'
        ' if(MQ&&MM==="push"){ setH(mc,mic()+"<span>"+(R?"talking":"hold")+"</span>"); setC(mc,"gbkChip gbkPtt"+(R?" hold":""));'
        ' setA(mc,"aria-label",R?"recording. let go to send":"push to talk. hold here or on the screen while you speak"); }\n'
        ' else if(MQ&&MM==="muted"&&!earOff){ setH(mc,gbMicIcon("muted")); setC(mc,"gbkChip gbkMuted");'
        ' setA(mc,"aria-label","microphone muted. nothing is heard. tap to turn the mic off"); }\n'
        ' else { setH(mc,hasTxt?snd():((earOff&&typeof micOff==="function")?micOff():mic()));',
        1,
    ),
    (
        "v3/next.html",
        '"microphone off. tap to enable, hold to say one thing"))))));',
        '"microphone off. tap to enable, hold to say one thing")))))); }\n'
        ' if(document.body.classList.contains("gbkPushMode")!==(MM==="push")) document.body.classList.toggle("gbkPushMode",MM==="push");',
        1,
    ),
    (
        "v3/next.html",
        ' var mt=g("gbkMeter"); setC(mt,((earOn||R)&&!P)?',
        ' var mt=g("gbkMeter"); setC(mt,((earOn||R)&&!P&&(R||MM==="open"))?',
        1,
    ),
    (
        "v3/next.html",
        ' var tx=g("gbkTxt"), ph=P?"napping":(R?"holding. speak.":(E==="pending"?',
        ' var tx=g("gbkTxt"), ph=P?"napping":(R?"holding. speak.":(MQ&&MM==="muted"?"mic muted. type to it":'
        '(MQ&&MM==="push"?"hold to talk, or type":(E==="pending"?',
        1,
    ),
    (
        "v3/next.html",
        '(earOn?"listening. talk or type":"type to it"))));',
        '(earOn?"listening. talk or type":"type to it"))))));',
        1,
    ),
    (
        "v3/next.html",
        ' var sb=g("gbkSound"); setH(sb,px(M?"mute":"sound",3)+\'<span>\'+(M?"unmute":"mute")+\'</span>\');',
        ' var sb=g("gbkSound"); setH(sb,gbSpk(!M)+\'<span>\'+(M?"speaker off":"speaker")+\'</span>\');'
        ' setA(sb,"aria-label",M?"speaker off. tap to hear him again":"speaker on. tap to silence him");\n'
        ' var mm=g("gbkMicMode"); if(mm){ var mi=GB_MIC_MODES.indexOf(MM);'
        ' setH(mm,gbMicIcon(MM)+"<span>"+GB_MIC_LABEL[MM]+"</span><span class=\\"gbkPips\\">"+GB_MIC_MODES.map(function(x,i){ return "<i"+(i===mi?" class=\\"on\\"":"")+"></i>"; }).join("")+"</span>");'
        ' setC(mm,"gbkCell"+(MM==="muted"?" gbkWarn on":""));'
        ' setA(mm,"aria-label","microphone mode: "+GB_MIC_LABEL[MM]+". tap for "+GB_MIC_LABEL[GB_MIC_MODES[(mi+1)%3]]); }',
        1,
    ),
    (
        "v3/next.html",
        "function mustShow(E){ return !!(window.MUTED||window.paused||",
        'function mustShow(E){ return !!(window.MUTED||window.GB_MIC==="muted"||window.paused||',
        1,
    ),
    # The mic button in push mode is the talk button: pressing it starts the clip at once.
    (
        "v3/next.html",
        ' if(g("gbkTxt").value.replace(/^\\s+|\\s+$/g,"")) return;\n clearTimeout(holdT); held=false; swallow=false;',
        ' if(g("gbkTxt").value.replace(/^\\s+|\\s+$/g,"")) return;\n'
        ' if(window.GB_MIC==="muted") return;\n'
        ' if(window.GB_MIC==="push"){ clearTimeout(holdT); held=false; if(window.gbPttDown) window.gbPttDown(e); paint(); return; }\n'
        ' clearTimeout(holdT); held=false; swallow=false;',
        1,
    ),
    (
        "v3/next.html",
        ' kick("listenBtn"); setTimeout(paint,80); });',
        ' if(window.GB_MIC==="push") return;\n kick("listenBtn"); setTimeout(paint,80); });',
        1,
    ),
    (
        "v3/next.html",
        'g("gbkSound").addEventListener("click",function(){ kick("muteBtn"); setTimeout(paint,80); });',
        'g("gbkSound").addEventListener("click",function(){ kick("muteBtn"); setTimeout(paint,80); });\n'
        'g("gbkMicMode").addEventListener("click",function(){ var i=GB_MIC_MODES.indexOf(window.GB_MIC||"open");'
        ' if(typeof window.gbMicSet==="function") window.gbMicSet(GB_MIC_MODES[(i+1)%3]); setTimeout(paint,80); });',
        1,
    ),
]
PATCHES += MIC_PATCHES

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
    shutil.copyfile(HERE / "growbot-status.js", site / "growbot-status.js")
    shutil.copyfile(HERE / "growbot-eyes.js", site / "growbot-eyes.js")
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
