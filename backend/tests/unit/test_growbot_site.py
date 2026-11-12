"""The committed GrowBot site (growbot/site/, built by growbot/patches/apply.py).

The box serves growbot/site/ straight from the bind mount, so these hold the plan's promises
on the bytes that ship (docs/plans/GROWBOT_PLAN.md §2, §2a): every speech path goes through
the box, nothing points the page at a third-party origin, the CSP hashes match the inline
code they allow, the vendored assets are the pinned ones, and the firmware dials the box.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_GROWBOT = Path(__file__).resolve().parents[3] / "growbot"
_SITE = _GROWBOT / "site"
_FIRMWARE = _GROWBOT / "firmware"


def _load_apply():
    path = _GROWBOT / "patches" / "apply.py"
    spec = importlib.util.spec_from_file_location("growbot_apply", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["growbot_apply"] = module
    spec.loader.exec_module(module)
    return module


apply = _load_apply()

_PAGES = sorted(_SITE.rglob("*.html"))
# Upstream's own code, which the vendored bundles are not: those carry their projects' default
# CDN strings for code paths the app never takes, and the CSP refuses them regardless.
_PIN = json.loads((_GROWBOT / "patches" / "upstream.json").read_text())
_VENDORED = {e["path"] for e in _PIN["vendor"]}
_CODE = [
    p
    for p in sorted(_SITE.rglob("*"))
    if p.suffix in {".html", ".js", ".mjs"} and p.relative_to(_SITE).as_posix() not in _VENDORED
]


def test_site_has_pages_and_the_creature() -> None:
    assert (_SITE / "v3" / "next.html").is_file()
    assert _PAGES


@pytest.mark.parametrize("page", _PAGES, ids=lambda p: p.relative_to(_SITE).as_posix())
def test_every_page_loads_the_box_voice_first(page: Path) -> None:
    first = re.search(r"<script[^>]*>", page.read_text(encoding="utf-8"))
    assert first is not None
    assert first.group(0) == '<script src="/growbot-box.js">'


def test_the_box_voice_replaces_speech_synthesis_and_posts_to_the_box() -> None:
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert 'Object.defineProperty(window, "speechSynthesis"' in shim
    assert 'fetch("/api/fish-tts"' in shim
    assert re.search(r"https?://", shim) is None


def test_the_creature_loads_the_face_right_after_the_box_voice() -> None:
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script[^>]*>", creature)
    assert tags[:2] == ['<script src="/growbot-box.js">', '<script src="/growbot-face.js">']
    assert (_SITE / "growbot-face.js").read_bytes() == (
        _GROWBOT / "patches" / "growbot-face.js"
    ).read_bytes()


def test_the_creature_shows_what_it_is_doing_top_left() -> None:
    """The status strip loads right after the face, before any page script, so its wrappers
    see every model call, photo, voice clip and leg frame the page makes."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script[^>]*>", creature)
    assert tags[:3] == [
        '<script src="/growbot-box.js">',
        '<script src="/growbot-face.js">',
        '<script src="/growbot-status.js">',
    ]
    strip = (_SITE / "growbot-status.js").read_text(encoding="utf-8")
    assert strip == (_GROWBOT / "patches" / "growbot-status.js").read_text(encoding="utf-8")
    assert re.search(r"https?://", strip) is None
    # Each hook is the page's own call site: the model and voice lanes, relay-transport's
    # frames, and the globals the VAD and every voice route keep.
    assert 'u.pathname === "/api/fish-tts"' in strip
    assert 'u.pathname !== "/api/llm" && u.pathname !== "/api/llm-stream"' in strip
    assert "body.indexOf('\"image_url\"')" in strip
    assert 'if (kind === "see") snap();' in strip
    assert "WebSocket.prototype.send = function" in strip
    assert "window.vadOn || window.recording" in strip
    assert "window.speakingUntil" in strip
    # The camera refills its frame ring every 2.5 s: an encode is not a look, so no hook on it.
    assert "toDataURL" not in strip
    assert '"/api/llm?target=openrouter"' in creature
    assert "var listenMode=false, vadRec=null, vadOn=false," in creature
    assert "var micStream=null, recording=false," in creature
    assert "var robotReady=false, speakingUntil=0," in creature
    transport = (_SITE / "relay-transport.js").read_text(encoding="utf-8")
    assert "JSON.stringify({ t: 'act', rid, steps, mode })" in transport


def test_the_strip_keeps_what_is_under_way_at_the_left_edge() -> None:
    """Running chips sort ahead of finished ones each render, and the strip drops below an
    overlay's logo bar instead of covering it."""
    strip = (_SITE / "growbot-status.js").read_text(encoding="utf-8")
    assert "if (ao !== bo) return ao ? -1 : 1;" in strip
    assert "return ao ? b.start - a.start : b.end - a.end;" in strip
    assert strip.index("order(t);") < strip.index(
        "for (var i = 0; i < entries.length; i++) {\n      var e"
    )
    assert 'document.querySelectorAll(".gb-entry-nav")' in strip
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<nav class="gb-entry-nav"' in creature


def test_the_bottom_bar_never_fades() -> None:
    """Brain, pause, mute and the type box stay on screen (owner, 2026-11-12)."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "setFade(true)" not in creature
    # The keyboard shrinks the page rather than panning the strip and the face off screen.
    assert "viewport-fit=cover, interactive-widget=resizes-content" in creature
    assert "function setFade(f){" in creature


def test_the_wake_gate_shows_no_store_card_on_the_box() -> None:
    """Upstream's adopted / members / adopt-me card is its store; the box has none."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "if(!a||!window._wakeGateUp||window.__gbBox) return;" in creature
    assert creature.index("gbStoreGuardFrom(j); }catch(e){}   // 0928") < creature.index(
        "||window.__gbBox) return;"
    )
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.__gbBox = true;" in shim


def test_the_pixel_eyes_give_way_only_while_the_new_face_is_on() -> None:
    """Upstream's eyes are skipped, not deleted: ?face=pixel or a throw brings them back."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "var _gbF=window.gbFace&&window.gbFace.on()?window.gbFace:null;" in creature
    assert "if(!_gbF) for(side=-1;side<=1;side+=2)" in creature
    assert "if(dreaming&&!_gbF){" in creature
    assert "if(_gbF) _gbF.draw(cx,W,H,{" in creature
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert re.search(r'var DEFAULT = "(ostrich|robot|lantern)";', face)
    assert 'face !== "pixel" && !broken' in face
    assert "broken = true;" in face


def test_a_persona_wears_its_own_face_lantern_by_default() -> None:
    """The box's face for the persona is read before the face loads, a poll's new one changes
    it where it stands, and only ?face= outranks it."""
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert 'var DEFAULT = "lantern";' in face
    assert (
        face.index("if (valid(asked)) return asked;")
        < face.index("if (valid(window.__gbBoxFace)) return window.__gbBoxFace;")
        < face.index("localStorage.gb_face ||")
    )
    assert 'window.addEventListener("gb-box-state", function (ev) {' in face
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert shim.index("window.__gbBoxFace = state.json.face;") < shim.index(
        "if (state.status === 204) {"
    )
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert page.index('<script src="/growbot-box.js">') < page.index(
        '<script src="/growbot-face.js">'
    )


def test_the_creature_can_recolour_itself_like_the_sky() -> None:
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    assert prompts.index('\'"coat":"OMIT usually') < prompts.index('\'"sky":"OMIT usually')
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "window.gbFace.setColour(o.coat,_hueOf(o.coat))" in creature
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert "localStorage.gb_face_color" in face


def test_mespeak_is_not_shipped_and_kokoro_is_the_only_engine() -> None:
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "/v2/mespeak.js" not in creature
    assert not (_SITE / "v2" / "mespeak.js").exists()
    assert 'var vtEngine="fish"' in creature
    assert '<option value="robot">' not in creature
    assert '<option value="device">' not in creature
    assert re.search(r'vtEngine=(?!=)[^;]*"(robot|device)"', creature) is None


@pytest.mark.parametrize("path", _CODE, ids=lambda p: p.relative_to(_SITE).as_posix())
def test_no_third_party_origin_in_code(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for origin in (
        "growbot-relay.growbot.workers.dev",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdn.jsdelivr.net",
        "storage.googleapis.com",
        "/feedback-widget.js",
    ):
        assert origin not in text, origin


def test_relay_dials_the_page_origin() -> None:
    relay = (_SITE / "relay-transport.js").read_text(encoding="utf-8")
    assert "RELAY_DEFAULT = (location.protocol === 'https:' ? 'wss://' : 'ws://')" in relay


def test_csp_hashes_match_the_inline_code_shipped() -> None:
    expected: set[str] = set()
    handlers = False
    for page in _PAGES:
        page_hashes, page_handlers = apply.csp_hashes(page.read_text(encoding="utf-8"))
        expected |= page_hashes
        handlers = handlers or page_handlers
    csp = json.loads((_SITE / "csp.json").read_text())
    assert set(csp["script_hashes"]) == expected
    assert csp["unsafe_hashes"] is handlers


@pytest.mark.parametrize("entry", _PIN["vendor"], ids=lambda e: e["path"])
def test_vendored_assets_are_the_pinned_bytes(entry: dict[str, str]) -> None:
    data = (_SITE / entry["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert entry["url"].startswith("https://")


def test_firmware_dials_the_box_with_its_key() -> None:
    chip = (_FIRMWARE / "relay_chip.py").read_text(encoding="utf-8")
    assert 'HOST = getattr(_box, "GROWBOT_HOST", None)' in chip
    assert "socket.getaddrinfo(HOST, PORT)" in chip
    assert "s = _wrap(raw)" in chip
    assert "ctx.verify_mode = ssl.CERT_REQUIRED" in chip
    assert 'raise RuntimeError("secrets.py has no GROWBOT_HOST' in chip
    assert '"key": ROBOT_KEY' in chip
    assert "% (PATH, SNI, key)" in chip
    compile(chip, "relay_chip.py", "exec")
    # The clock is set only from a reply that echoes this request's random stamp.
    assert "query[40:48] = nonce" in chip and "msg[24:32] != nonce" in chip


def test_csp_hashes_cover_tags_handlers_and_innerhtml_handlers() -> None:
    page = (
        '<p onclick="a(&quot;x&quot;)">t</p>'
        '<script src="/x.js" onerror="e()"></script>'
        "<script>h('<b onclick=\"f(\\'y\\')\">');</script>"
    )
    hashes, handlers = apply.csp_hashes(page)
    assert handlers is True
    assert apply._sha(b'a("x")') in hashes
    assert apply._sha(b"e()") in hashes
    assert apply._sha(b"f('y')") in hashes
    assert apply._sha(b"h('<b onclick=\"f(\\'y\\')\">');") in hashes
    assert len(hashes) == 4


def test_a_patch_that_no_longer_matches_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("one\n")
    with pytest.raises(apply.PatchError, match="expected 1"):
        apply._patch(tmp_path, [("a.js", "two", "three", 1)])
    apply._patch(tmp_path, [("a.js", "one", "uno", 1)])
    assert (tmp_path / "a.js").read_text() == "uno\n"


def test_a_vendored_asset_with_the_wrong_hash_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.bin").write_bytes(b"tampered")
    entry = {"path": "x.bin", "url": "https://example.invalid/x", "sha256": "0" * 64}
    with pytest.raises(apply.PatchError, match="sha256"):
        apply._fetch(entry, tmp_path)


# A runtime load of an absolute URL: fetch, import, a socket, a worker, or a src/href that the
# browser follows on its own. A link a person taps is not one of these.
_RUNTIME_LOAD = re.compile(
    r"""(?:fetch|import|WebSocket|EventSource|Worker|SharedWorker|importScripts)\s*\(\s*["'`]https?://"""
    r"""|<(?:script|img|audio|video|source|iframe|link)\b[^>]*\s(?:src|href)=["']https?://""",
    re.I,
)


@pytest.mark.parametrize("path", _CODE, ids=lambda p: p.relative_to(_SITE).as_posix())
def test_no_runtime_load_of_any_absolute_url(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert _RUNTIME_LOAD.search(text) is None


def test_handler_shapes_the_build_cannot_hash_are_refused() -> None:
    assert apply._unsupported_handlers("el.setAttribute('onclick', 'x()')")
    assert apply._unsupported_handlers('h("<b onclick=\\"x()\\">")')
    assert apply._unsupported_handlers('<b onclick="x()">') == []


def test_a_failed_build_leaves_the_committed_tree_untouched(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "app").mkdir(parents=True)
    (upstream / "app" / "relay-transport.js").write_text("changed upstream\n")
    out = tmp_path / "out"
    (out / "site").mkdir(parents=True)
    (out / "site" / "kept.txt").write_text("committed\n")
    with pytest.raises(apply.PatchError):
        apply.build(upstream, out, None)
    assert (out / "site" / "kept.txt").read_text() == "committed\n"
    assert sorted(p.name for p in out.iterdir()) == ["site"]


def test_the_creature_installs_as_an_app() -> None:
    """Android's Install app needs a manifest with a start page and 192/512 icons; the
    sidecar serves exactly these paths without a session (serve.py PUBLIC_PATHS)."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    manifest = json.loads((_SITE / "manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/start" and manifest["scope"] == "/"
    assert manifest["display"] in ("fullscreen", "standalone")
    sizes = set()
    for icon in manifest["icons"]:
        data = (_SITE / icon["src"].lstrip("/")).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        assert icon["sizes"] == f"{width}x{height}"
        sizes.add(width)
    assert {192, 512} <= sizes


def test_the_service_worker_is_registered_and_caches_nothing() -> None:
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert 'navigator.serviceWorker.register("/sw.js"' in shim
    worker = (_SITE / "sw.js").read_text(encoding="utf-8")
    assert 'event.request.mode !== "navigate"' in worker
    assert 'pathname === "/open") return;' in worker
    assert (_SITE.parent / "patches" / "pwa" / "sw.js").read_text(encoding="utf-8") == worker
    assert "caches." not in worker and "cache.put" not in worker


def test_the_setup_page_follows_only_this_boxs_ticket_link() -> None:
    """The second QR is the only thing the setup page opens, so a code from anywhere else
    (another site, another box) goes nowhere."""
    page = (_SITE / "setup.html").read_text(encoding="utf-8")
    assert 'href="/box-root.crt"' in page and '<script src="/setup.js">' in page
    scanner = (_SITE / "setup.js").read_text(encoding="utf-8")
    assert "url.origin !== location.origin" in scanner
    assert 'url.pathname !== "/open"' in scanner
    assert "location.replace(link)" in scanner


def test_the_creature_page_starts_on_the_boxs_soul() -> None:
    """G4b (plan §2c): a persona's soul is pulled into pb2_soul before soul.js reads it (the
    loader is the creature's first script, and the pull is synchronous), only on the creature
    page, and every write of the key goes back to the box."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert shim == (_GROWBOT / "patches" / "growbot-box.js").read_text(encoding="utf-8")
    sync = shim[shim.index('if (location.pathname !== "/v3/next.html") return;') :]
    assert "r.open(method, path, false);" in sync
    assert 'var KEY = "pb2_soul"' in sync and 'PATH = "/api/box/persona"' in sync
    assert "Storage.prototype.setItem = function" in sync
    # The pull lands before the hook, so the box's own copy is never pushed back as a write.
    assert sync.index("put(KEY, JSON.stringify(pulled.json.soul))") < sync.index(
        "Storage.prototype.setItem ="
    )
    # An unsent write goes only into the persona it came from: the page checks, and every push
    # names it for the box to check too.
    assert "m.persona === persona" in sync
    assert sync.count("JSON.stringify({ persona: ") == 2
    # The phone's own (G2) creature is set aside while a persona has the key.
    assert 'OWN = "gb_phone_soul"' in sync and "put(OWN, mine)" in sync


def test_the_creatures_own_pairing_box_opens_nothing() -> None:
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.prompt = function (message)" in shim
    assert "/pairing code/i.test(String(message))" in shim
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert 'window.prompt("Pairing code shown for your robot:"' in page


def test_visual_memories_follow_the_persona() -> None:
    """G4d (plan §2c): each persona's saved views are its own namespace, merged in from the box
    before the creature opens its store, and every write the creature makes to it is sent."""
    vm = (_SITE / "v3" / "visual-memory.js").read_text(encoding="utf-8")
    assert "function vmNs(){ try{ return window.__gbBoxVmNs||" in vm
    assert "w.then(vmOpenNow,vmOpenNow)" in vm and "function vmOpenNow(){" in vm
    assert 'if(mode==="readwrite"&&typeof window.gbBoxMemChanged==="function")' in vm
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "/v3/visual-memory.js?v=1005-mem-box" in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    mem = shim[shim.index('var PREFIX = "gb_box_vm/"') :]
    assert 'PATH = "/api/box/memories"' in mem
    assert "window.gbBoxMemReady = Promise.race(" in mem and "window.gbBoxMemChanged = " in mem
    # Pinned per tab, never through shared storage; a G2 session (no persona) pins nothing.
    assert "window.__gbBoxVmNs = NS;" in mem and "gb_box_vm_ns" not in shim
    # Moved, deleted or a G2 session: every persona's faces leave the phone.
    assert "var leaving = !!(soul.g2 || soul.away);" in mem and 'purge(d, "")' in mem
    assert "window.__gbBoxSoul = { away: true };" in shim
    # A record the merge could not fetch never reloads the page, nor does one version twice.
    assert "missing[id] !== fp(box.rows[id])" in mem and "m.mergedAt === box.version" in mem
    # Writes name the persona this tab runs as, for the sidecar to check.
    assert mem.count("{ persona: persona, ") == 2
    # Only a phone with unsent writes forgets on the box what it lacks.
    assert "known[id] !== undefined && m.dirty" in mem


def test_the_creature_says_why_the_box_failed_it() -> None:
    """A slow, stopped or refusing local model reads as itself, not as upstream's cloud lines
    or a bare abort message, and a dropped clip says which check dropped it."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.gbBoxEars = function (e)" in shim
    assert "window.gbBoxBrainWhy = function (e)" in shim
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert page.count("window.gbBoxEars?window.gbBoxEars(e)") == 2
    assert "window.gbBoxBrainWhy?window.gbBoxBrainWhy(e):e.message" in page
    # The box's words ride a property: the page's catch pattern-matches the message.
    assert "if(streamErr&&!L)_ee.boxSaid=streamErr;" in page
    assert 'if (e && typeof e.boxSaid === "string")' in shim
    # Spoken turns wait 90 s, ticks keep 30, and a timed-out stream is not retried.
    assert page.count("_decisionTokens,90000") == 2 and page.count("_decisionTokens,30000") == 1
    assert 'e.gateFail||(e&&e.name==="AbortError")||' in page
    assert 'toast("\u2026didn\'t catch that")' not in page
    assert "the speech model heard no words" in page
    # Both transcriber lanes are the box's whisper: no 8 s/12 s cloud leash re-sending the clip.
    assert page.count("_lease=65000;") == 2 and "_lease=_pri?8000:12000" not in page
    assert "diTranscribe(blob,type,meta); return;" not in page
    assert "groqTranscribe(blob,type,meta); return;" not in page


def test_the_system_prompt_holds_only_what_stays_put() -> None:
    """The box's hybrid model reuses a cached prompt only up to a checkpoint at a message
    boundary, so a turn is fast only when the system message is the same text as the last
    turn's. The state, clock, diary, self-stats and recall ride the turn's own message."""
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    start = prompts.index("function buildSystem(")
    body = prompts[start : prompts.index("\n}\n", start)]
    assert body.endswith("\n  return _gbStatic + buildOutputSchema(soul.body);")
    live = body[body.index("buildSystem.live = ") : body.index("  return _gbStatic")]
    moving = ("languageContext(", "== LONG-ARC", "== SCRATCHPAD", "clock", "== RECENT DIARY")
    for part in moving:
        assert part in live, part
    static = body[body.index("var _gbStatic = ") : body.index("buildSystem.live = ")]
    assert "SAFETY_FLOOR" in static and "== YOUR IDENTITY" in static and "clock" not in static
    # The automatic reply-language line rewrites itself once speech settles a language.
    assert "languageContext(" not in static

    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '_gbLive=(buildSystem.live||"")+(moveGuideText.live||"")' in page
    # The movement guide's live lines (latest move, seek beat, stance) leave it in every mode.
    guide = page[page.index("function moveGuideText(){") : page.index("/* ---- MIMIC")]
    assert guide.count("moveGuideText.live=") == 3
    for moving in (
        "goalBlock()",
        "gaitList()",
        "_recStatus()",
        "_namedStatus()",
        "_expressionGuide()",
    ):
        for line in guide.splitlines():
            if moving in line and "return " in line:
                assert line.index("moveGuideText.live=") < line.index(moving), moving
    assert "_gbLive+=MEMLOG_ASK; if(_recall&&_recall.text) _gbLive+=_recall.text;" in page
    fixed = (
        'sys+=(typeof gaitList==="function"&&gaitList.fixed||"")'
        '+(typeof selfStats==="function"&&selfStats.fixed||"")'
        '+(typeof vmPrompt==="function"&&vmPrompt.fixed||"");'
    )
    assert "sys+=" not in page[
        page.index("var sys=buildSystem(") : page.index("var slug=brainOverride(")
    ].replace("sys+=_gbLive;", "").replace(fixed, "")
    assert '<script src="/v3/prompts.js?v=1008-motion-box-lang"' in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.gbBoxLive = function (msgs, live)" in shim


def test_the_fixed_rules_ride_the_system_message_and_the_state_stays_live() -> None:
    """The gait rules, the picture-memory contract and the fixed self-description are the same
    every turn, so they join the cached system message; every sentence that depends on state
    stays in the turn's own message, or one change would make each turn re-read it all."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    stats = page[page.index("function selfStats(){") : page.index("/* ===== BRAIN TAB")]
    fixed = page[page.index('var GB_SELF_FIXED="') : page.index("function selfStats(){")]
    live = stats[stats.index('return "== WHAT YOU ARE RIGHT NOW') :]
    for moving in ("_bootT", "callsSinceBoot", "seekGoal", "camOn", "moving", "lastMimic"):
        assert moving in live and moving not in fixed, moving
    for rule in ("You think whenever", "Quiet does not put you to sleep", "don't deflect"):
        assert rule in fixed and rule not in live, rule
    assert 'selfStats.fixed=GB_SELF_FIXED.replace("{model}",model);' in stats

    spin = page[page.index("function normalSpinTruth(){") : page.index("function gaitList(){")]
    assert "normalSpinTruth.now=normal;" in spin
    assert '"+normal+"' not in spin
    gaits = page[page.index("function gaitList(){") : page.index("// one-mover-at-a-time")]
    rules = gaits[gaits.index("gaitList.fixed=") : gaits.index("  return spinFeedback")]
    now = gaits[gaits.index("  return spinFeedback") :]
    assert "== TRAINED WALKS" in rules and "normalSpinTruth()" in rules
    for moving in ("forwardWalkState", "lines.join", "normalSpinTruth.now", "spinMeasLine"):
        assert moving in now and moving not in rules, moving

    vm = (_SITE / "v3" / "visual-memory.js").read_text(encoding="utf-8")
    prompt = vm[vm.index("function vmPrompt(c){") : vm.index("function vmContract(){")]
    contract = vm[
        vm.index("function vmContract(){") : vm.index("\n}", vm.index("function vmContract"))
    ]
    assert 'vmPrompt.fixed=vmOn()?vmContract():"";' in prompt
    assert "VISUAL MEMORY overrides" in contract and "vmCatalogLine" not in contract
    assert "vmCatalogLine()" in prompt and "VM_PROMPT_OFF" in prompt
    assert '<script src="/v3/visual-memory.js?v=1005-mem-box-fixed"' in page


def test_the_brain_panel_keeps_only_what_a_box_creature_uses() -> None:
    """The box picks the model and holds the soul, and there are no credits or keys: those
    rows hide behind the shim's flag, and the page without it keeps upstream's panel."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    for row in (
        'h+=wRow("dial","brain","","the brain dial"',
        'h+=wRow("soul","soul","#9A86FF","soul"',
        'h+=wRow("byok","brain","#FFB24D","your own key"',
        'h+=hatch("mind");',
        'h+=\'<button type="button" id="gbkAdvTog"',
    ):
        assert page.count("if(!window.__gbBox)" + row) == 1, row
    assert 'function brainLogsSection(){ if(window.__gbBox) return "";' in page
    assert 'function moveMeRow(){ if(window.__gbBox) return "";' in page
    assert "if(W.adv&&!window.__gbBox){" in page
    assert page.count('data-gb-cloud="1"') == 1
    assert '(_t==="c"&&!window.__gbBox)' in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.__gbBox = true;" in shim
    assert "[data-gb-cloud]{display:none!important}" in shim
    assert "#giftBtn,#bpTabByok,#bpTabCredits{display:none!important}" in shim


def test_the_text_is_phone_sized_even_in_desktop_site_mode() -> None:
    """The spoken line and the brain panel are raised to phone sizes, and a phone that lays the
    page out ~980px wide (desktop site) has its controls zoomed back, never the face's canvas."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert '"#say{font-size:" + 15 * k + "px!important' in shim
    assert '"#gbkBBody{zoom:1.2}"' in shim
    assert '"body>*:not(#wrap){zoom:"' in shim
    assert "return k > 1.25 ? Math.min(k, 3) : 1;" in shim
    assert 'window.addEventListener("resize", paint);' in shim


def test_past_replies_go_back_to_the_model_in_its_own_json_shape() -> None:
    """Replayed as bare words, the creature's history taught the model to answer in clipped
    plain text; each past reply now rides as {"say", "emotion"}, a page note as an empty say."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert 'msgs.push({role:"assistant",content:gbTraceReply(tr[i])});' in page
    assert 'content:tr[i].d?tr[i].a+"\\n(did: "' not in page
    assert ',e:(o&&typeof o.emotion==="string"?o.emotion.slice(0,12):undefined)};' in page
    start = page.index("function gbTraceReply(t){")
    fn = page[start : page.index("\n", start)]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = (
        "[gbTraceReply({a:'Hi \"Jeff\"',e:'happy'}),"
        "gbTraceReply({a:'(stayed quiet)',d:'spin 2 s'}),"
        "gbTraceReply({a:'(brain hiccup \u2014 out of energy, no answer)'}),"
        "gbTraceReply({a:'(purrs) hi'}),gbTraceReply({})]"
    )
    script = f"{fn}\nconsole.log(JSON.stringify({cases}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    said, quiet, hiccup, spoken, empty = json.loads(out.stdout)
    assert json.loads(said) == {"say": 'Hi "Jeff"', "emotion": "happy"}
    assert quiet == '{"say":""}\n(stayed quiet)\n(did: spin 2 s)'
    assert hiccup.startswith('{"say":""}\n(brain hiccup')
    # A say the model wrote in brackets is still words it spoke.
    assert json.loads(spoken) == {"say": "(purrs) hi"}
    assert json.loads(empty) == {"say": ""}


def test_a_spoken_turn_sends_one_frame_unless_something_is_moving() -> None:
    """Each frame is ~300 tokens of prefill on the box; three only when there is change to see."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    line = page[page.index("var _nf=(kind===") :]
    line = line[: line.index(";")]
    assert line.endswith(
        ":((frameDiff>3||_rideOn||Date.now()<selfMoveUntil||(extra&&extra.beat&&seekGoal)"
        '||(extra&&typeof extra.visualUser==="string"&&_GB_LOOK_WORDS.test(extra.visualUser)))'
        "?3:1)"
    )
    words = page[page.index("var _GB_LOOK_WORDS=/") :]
    words = words[len("var _GB_LOOK_WORDS=/") : words.index("/i;")]
    look = re.compile(words.replace("\\b", r"\b"), re.IGNORECASE)
    for said in ("watch this", "what am I holding?", "What's this", "can you see me"):
        assert look.search(said), said
    for said in ("how are you", "spin to your left", "tell me a story"):
        assert not look.search(said), said
    assert "each thought you see the freshest one, or the 3 freshest" in page


def _stream_js(page: str) -> str:
    start = page.index("var _gbPieceNext=null")
    return page[start : page.index("function _gbPcmUttStop0(")]


_STREAM_HARNESS = r"""
var _pcmRate=1000, _pcmLive=true, _pcmUtt=null, vadVoiceT=0, _vadHold=1500, now=0;
Date.now=function(){ return now; };
var timers=[]; setTimeout=function(f,ms){ timers.push({f:f,at:now+(ms||0)}); };
function run(){ timers.sort(function(a,b){ return a.at-b.at; });
  while(timers.length&&timers[0].at<=now){ timers.shift().f(); } }
var sent=[], shown=[], stt=[], old=[];
function encodeWav(f,r,d,c){ return {len:f.length}; }
var _vadEndT=0, _imitationCtl=null; var document={getElementById:function(){ return null; }};
function Blob(parts){ this.len=parts[0].len; }
function transcribe(b,t,ms){ stt.push({p:_gbPieceNext,len:b.len}); _gbPieceNext=null; }
function showYouSaid(t){ shown.push(t); } function sendTxtFrom(t,m){ sent.push(t); }
function logEv(){} function _convLangSaw(){}
function _gbPcmUttStop0(s,t){ old.push(s); }
function utt(){ _pcmUtt={buf:new Float32Array(13000),n:0,pre:0,t0:now,nf:0}; _pcmLive=true; }
function feed(ms,voice){ for(var i=0;i<ms;i+=100){ var d=new Float32Array(100), u=_pcmUtt;
  var take=Math.min(u.buf.length-u.n,100); u.buf.set(d.subarray(0,take),u.n); u.n+=take;
  if(voice) vadVoiceT=now; _gbRollCheck(d,take); now+=100; run(); } }
function answer(i,txt){ var p=stt[i].p; _gbPiece(p,txt,{lang:"en"},"en"); }
"""


def _run_stream(page: str, scenario: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        _stream_js(page)
        + _STREAM_HARNESS
        + scenario
        + "\nconsole.log(JSON.stringify({sent:sent,shown:shown,stt:stt.length,old:old}));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_speech_is_transcribed_in_pieces_and_reaches_the_creature_as_one_turn() -> None:
    """A long message goes to the transcriber at its pauses and at the buffer's end while the
    person keeps talking, nothing past the old 12 s cap is lost, and the creature gets one turn
    with every piece in order."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    long_talk = """
    utt(); feed(5000,true); feed(600,false);            // 5 s then a pause: piece 0
    feed(13000,true);                                   // a full buffer: piece 1, no audio lost
    feed(2000,true); pcmUttStop(true,300); run();       // the end: piece 2, the final
    answer(1,"second part"); answer(0,"first part,");
    answer(2,"and the end.");
    """
    got = _run_stream(page, long_talk)
    assert got["stt"] == 3
    assert got["sent"] == ["first part, second part and the end."]
    assert got["old"] == []
    assert "first part, second part …" in got["shown"]

    # A short message never splits and goes the old way.
    short = "utt(); feed(3000,true); pcmUttStop(true,300); run();"
    got = _run_stream(page, short)
    assert got["stt"] == 0 and got["old"] == [True] and got["sent"] == []


def test_a_lost_or_silent_piece_never_holds_the_turn() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    # Piece 0 never answers; the end is silence, so no final piece is sent: the turn goes after
    # the 12 s give-up with what did come back.
    lost = """
    utt(); feed(5000,true); feed(600,false); feed(4500,true); feed(600,false);
    feed(1200,false); pcmUttStop(true,300); run();
    answer(1,"the words that came back");
    now+=25000; run();
    answer(0,"late words");
    """
    got = _run_stream(page, lost)
    # What came back goes after the give-up; a piece landing later still reaches the creature.
    assert got["stt"] == 2 and got["sent"] == ["the words that came back", "late words"]

    # A cancelled utterance (its own voice, pause, a hidden tab) sends nothing, as upstream.
    cancelled = """
    utt(); feed(5000,true); feed(600,false); feed(1000,true); pcmUttStop(false,0); run();
    answer(0,"hello there");
    """
    got = _run_stream(page, cancelled)
    assert got["sent"] == []


def test_a_streamed_utterance_outlives_the_old_twelve_second_cap() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "else if(now-vadStartT > (_pcmLive&&_pcmUtt?GB_UTT_MAX_MS:12000)){ vadStop();" in page
    assert "_gbFinal(_gbU,true,0)" in page[page.index("function pcmTapDown(){") :][:300]
    # Its own beats hold while a streamed message is live or its pieces are landing.
    assert (
        "if((vadOn&&(now-vadStartT<6000||(_pcmLive&&_pcmUtt&&_pcmUtt.gbId)))||now<vadHoldUntil"
        "||Object.keys(_gbUtts).length){ lastActT=now; return; }"
    ) in page
    # 30 s of talk with no pause: the full buffer rolls twice and nothing is dropped.
    nonstop = """
    utt(); feed(30000,true); pcmUttStop(true,300); run();
    for(var i=0;i<stt.length;i++) answer(i,"part"+i);
    """
    got = _run_stream(page, nonstop)
    assert got["stt"] == 3 and got["sent"] == ["part0 part1 part2"]


def test_an_ordinary_quiet_beat_waits_ninety_seconds_after_the_last_activity() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "var GB_QUIET_AFTER_MS=90000;" in page
    assert "if(idle>=GB_QUIET_AFTER_MS && now-lastBeatT>240000){" in page
    assert "idle>=8000 && now-lastBeatT" not in page
    assert page.index("var GB_QUIET_AFTER_MS") < page.index("if(idle>=GB_QUIET_AFTER_MS")
    assert "90 seconds after anything last happened" in page
