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
import sys
from pathlib import Path

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
    for moving in ("== LONG-ARC", "== SCRATCHPAD", "clock", "== RECENT DIARY", "diary"):
        assert moving in live
    static = body[body.index("var _gbStatic = ") : body.index("buildSystem.live = ")]
    assert "SAFETY_FLOOR" in static and "== YOUR IDENTITY" in static and "clock" not in static

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
    assert "sys+=" not in page[
        page.index("var sys=buildSystem(") : page.index("var slug=brainOverride(")
    ].replace("sys+=_gbLive;", "")
    assert '<script src="/v3/prompts.js?v=1008-motion-box"' in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.gbBoxLive = function (msgs, live)" in shim
