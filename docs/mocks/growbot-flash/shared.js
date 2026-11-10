// Simulated state and the pieces every variant shares: the flash (picotool, then mpremote),
// the Wi-Fi checklist, the pairing code, the device key, "Open GrowBot" and its certificate
// note, and the Ops services list. No network: nothing here fetches, and "Open GrowBot" shows
// a pretend tab instead of navigating, so the mock can be opened from a file anywhere.
(function () {
  const G = (window.G = {});
  const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;

  G.esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  G.ago = (ts) => {
    const s = (Date.now() - ts) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  };
  G.day = (ts) => new Date(ts).toLocaleDateString("en-GB", { day: "numeric", month: "short" });

  // New codes come from a fixed list so a test run is repeatable.
  const CODES = ["gb-7c20be", "gb-e41d09", "gb-5ab3f2", "gb-0d9c61"];

  function fresh() {
    return {
      screen: null,
      usb: "blank", // what is on the box's USB: blank | mp | none
      outcome: "ok", // the next flash: ok | fail-load | fail-wifi
      svc: "up",
      trusted: false, // does this phone trust the box's certificate
      certNoted: false, // the first-time note has been shown on this phone
      boxIp: "192.168.1.20",
      networks: [
        { ssid: "Home-2G", on: true, saved: true },
        { ssid: "Workshop", on: false, saved: true },
      ],
      robots: [
        { id: "r1", name: "Quorra", code: "gb-3fa91c", key: "active", minted: Date.now() - 7 * DAY, online: true, seen: Date.now() - 20e3 },
        { id: "r2", name: "Tamsin", code: "gb-91d0e4", key: "active", minted: Date.now() - 2 * DAY, online: false, seen: Date.now() - 3 * HOUR },
      ],
      codeIx: 0,
      form: { target: "new", name: "", reinstall: false, adding: false, newSsid: "", newPass: "", remember: true },
      flash: { state: "idle", phases: {}, log: [], fail: null, robotId: null, rotate: false },
      armed: {},
      sheet: null,
      toast: null,
      open: {},
    };
  }
  G.S = fresh();

  // ---- render loop with focus and scroll kept across re-renders ----
  let renderFn = () => {};
  G.render = () => {
    const a = document.activeElement;
    const focusId = a && a.id ? a.id : null;
    const sel = a && "selectionStart" in a ? [a.selectionStart, a.selectionEnd] : null;
    const scrolls = [...document.querySelectorAll("[data-scroll]")].map((el) => [el.dataset.scroll, el.scrollTop]);
    renderFn();
    G.renderMockbar();
    for (const [k, top] of scrolls) {
      const el = document.querySelector(`[data-scroll="${k}"]`);
      if (el) el.scrollTop = top;
    }
    document.querySelectorAll(".log").forEach((el) => (el.scrollTop = el.scrollHeight));
    if (focusId) {
      const el = document.getElementById(focusId);
      if (el) {
        el.focus();
        if (sel && el.setSelectionRange) try { el.setSelectionRange(sel[0], sel[1]); } catch (_) {}
      }
    }
  };

  G.actions = {};
  G.mount = ({ screens, render, actions }) => {
    G.screens = screens;
    G.S.screen = screens[0][0];
    renderFn = render;
    Object.assign(G.actions, actions || {});
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-act]");
      if (!t) return;
      const fn = G.actions[t.dataset.act];
      if (fn) { e.preventDefault(); fn(t.dataset.arg, t); }
    });
    document.addEventListener("input", (e) => {
      const t = e.target;
      if (t.dataset.bind) { G.S.form[t.dataset.bind] = t.type === "checkbox" ? t.checked : t.value; G.render(); }
    });
    document.addEventListener("change", (e) => {
      const t = e.target;
      if (t.dataset.mock) { G.S[t.dataset.mock] = t.type === "checkbox" ? t.checked : t.value; G.render(); }
    });
    setInterval(() => { if (!(document.activeElement && document.activeElement.tagName === "INPUT")) G.render(); }, 15000);
    G.render();
  };

  // ---- mock controls, outside the phone ----
  G.renderMockbar = () => {
    const S = G.S, el = document.getElementById("mockbar");
    if (!el) return;
    const theme = document.documentElement.dataset.theme;
    const opt = (v, l, cur) => `<option value="${v}"${v === cur ? " selected" : ""}>${l}</option>`;
    el.innerHTML = `
      <div class="mb-title">Mock controls — not part of the design</div>
      <div class="mb-row">Screen:
        ${G.screens.map(([id, l]) => `<button data-act="screen" data-arg="${id}" class="${S.screen === id ? "on" : ""}">${l}</button>`).join("")}
        <button data-act="theme" data-arg="${theme === "dark" ? "light" : "dark"}">Theme: ${theme}</button>
        <button data-act="reset">Reset</button>
      </div>
      <div class="mb-row">
        <label>On the box's USB <select data-mock="usb">${opt("blank", "a new Pico", S.usb)}${opt("mp", "a Pico with MicroPython", S.usb)}${opt("none", "nothing", S.usb)}</select></label>
        <label>Next flash <select data-mock="outcome">${opt("ok", "succeeds", S.outcome)}${opt("fail-load", "fails loading MicroPython", S.outcome)}${opt("fail-wifi", "fails joining Wi-Fi", S.outcome)}</select></label>
        <label>growbot service <select data-mock="svc">${opt("up", "up", S.svc)}${opt("down", "down", S.svc)}</select></label>
        <label><input type="checkbox" data-mock="trusted"${S.trusted ? " checked" : ""}> phone trusts the box</label>
      </div>`;
  };

  Object.assign(G.actions, {
    screen: (id) => { G.S.screen = id; G.S.sheet = null; G.render(); },
    theme: (t) => { document.documentElement.dataset.theme = t; G.render(); },
    reset: () => { const scr = G.S.screen; G.S = fresh(); G.S.screen = scr; G.render(); },
    toggle: (k) => { G.S.open[k] = !G.S.open[k]; G.render(); },
    rescan: () => G.toast(G.S.usb === "none" ? "Nothing on the box's USB." : "Found it again."),
    net: (i) => { const n = G.S.networks[+i]; n.on = !n.on; G.render(); },
    "net-add": () => { G.S.form.adding = true; G.render(); },
    "net-save": () => {
      const f = G.S.form;
      if (!f.newSsid.trim()) return;
      G.S.networks.push({ ssid: f.newSsid.trim(), on: true, saved: f.remember });
      Object.assign(f, { adding: false, newSsid: "", newPass: "" });
      G.render();
    },
    target: (v) => { G.S.form.target = v; G.render(); },
    reinstall: () => { G.S.form.reinstall = !G.S.form.reinstall; G.render(); },
    flash: () => G.startFlash({}),
    "flash-again": () => { G.S.flash = { state: "idle", phases: {}, log: [], fail: null }; G.render(); },
    copy: (code) => G.toast(`Copied ${code}.`),
    open: (id) => G.openRobot(id),
    "cert-download": () => G.toast("Saved the box's certificate to Downloads. Open it there to install it — once, for every page on the box."),
    "cert-go": (id) => { G.S.certNoted = true; G.openRobot(id, true); },
    "sheet-close": () => {
      const S = G.S;
      if (S.flash.rotate && S.flash.state !== "running") S.flash = { state: "idle", phases: {}, log: [], fail: null };
      S.sheet = null;
      G.render();
    },
    rotate: (id) => G.rotate(id),
    revoke: (id) => G.revoke(id),
    restart: () => G.toast("Restarting growbot. Robots reconnect on their own in a few seconds."),
  });

  G.toast = (msg) => {
    G.S.toast = msg;
    G.render();
    clearTimeout(G._t);
    G._t = setTimeout(() => { G.S.toast = null; G.render(); }, 3500);
  };
  G.toastHTML = () => (G.S.toast ? `<div class="toast" role="status">${G.esc(G.S.toast)}</div>` : "");

  // ---- the 3-second arming confirm, as on the panels ----
  G.arm = (key) => {
    const S = G.S;
    if (S.armed[key] && S.armed[key] > Date.now()) { delete S.armed[key]; return true; }
    S.armed[key] = Date.now() + 3000;
    setTimeout(() => { if (S.armed[key] && S.armed[key] <= Date.now()) { delete S.armed[key]; G.render(); } }, 3050);
    G.render();
    return false;
  };
  G.isArmed = (key) => G.S.armed[key] && G.S.armed[key] > Date.now();

  G.robot = (id) => G.S.robots.find((r) => r.id === id);

  // ---- words for a robot's state; colour is never the only signal ----
  G.robotState = (r) => {
    if (r.key === "revoked") return { cls: "bad", text: "key revoked — can't connect" };
    if (G.S.svc === "down") return { cls: "bad", text: "can't connect — growbot is down" };
    if (r.online) return { cls: "ok", text: `connected · ${G.ago(r.seen)}` };
    return { cls: "warn", text: `not connected · last seen ${G.ago(r.seen)}` };
  };
  G.stateHTML = (r) => {
    const s = G.robotState(r);
    return `<span class="state"><span class="dot ${s.cls}"></span><span class="${s.cls}-t">${s.text}</span></span>`;
  };
  G.keyLine = (r) => r.key === "revoked"
    ? `<span class="bad-t">Key revoked.</span> It can't reach the box until it is reflashed over USB, which gives it a new key.`
    : `Device key made ${G.day(r.minted)} at flash time. Only this robot has it.`;

  // ---- what is on the box's USB ----
  G.usbHTML = (radio) => {
    const S = G.S;
    if (S.usb === "none") return `<p class="msg warn">Nothing on the box's USB. Plug the robot's Pico into the box with a data cable, not a charge-only one, then rescan.</p>`;
    const dev = S.usb === "blank"
      ? { path: "RP2350 bootloader", em: "A new Pico — MicroPython goes on first." }
      : { path: "/dev/ttyACM0", em: "A Pico already running MicroPython." };
    return `<div class="choices"><label class="choice sel">${radio ? `<input type="radio" checked name="port">` : ""}
      <span class="tx"><span class="mono">${dev.path}</span><em>${dev.em}</em></span><span class="tag">Pico 2 W</span></label></div>`;
  };

  // ---- the networks it should join (the box's remembered list, more than one allowed) ----
  G.networksHTML = () => {
    const S = G.S, f = S.form;
    const rows = S.networks.map((n, i) => `<label class="choice${n.on ? " sel" : ""}"><input type="checkbox" data-act="net" data-arg="${i}"${n.on ? " checked" : ""}>
      <span class="tx">${G.esc(n.ssid)}<em>${n.saved ? "Remembered on the box" : "This flash only — not remembered"}</em></span></label>`).join("");
    const add = f.adding
      ? `<div class="card" style="padding:12px;margin:0 0 12px">
          <label class="field">Network name<input id="f-ssid" data-bind="newSsid" value="${G.esc(f.newSsid)}" autocapitalize="none"></label>
          <label class="field">Password<input id="f-pass" type="password" data-bind="newPass" value="${G.esc(f.newPass)}"></label>
          <label class="choice${f.remember ? " sel" : ""}"><input type="checkbox" id="f-rem" data-bind="remember"${f.remember ? " checked" : ""}><span class="tx">Remember it on the box<em>So the next robot or panel can use it without retyping.</em></span></label>
          <div class="acts"><button class="btn primary" data-act="net-save"${f.newSsid.trim() ? "" : " disabled"}>Add network</button></div></div>`
      : `<button class="btn" data-act="net-add" style="margin:0 0 12px">Add another network</button>`;
    const none = S.networks.every((n) => !n.on) ? `<p class="msg warn">Pick at least one — a robot with no network can't reach the box.</p>` : "";
    return `<fieldset class="choices"><legend>Networks it can join — it tries them in this order</legend>${rows}</fieldset>${add}${none}
      <p class="hint dim">2.4 GHz only — the Pico has no 5 GHz radio. It reaches the box at <span class="mono">${S.boxIp}</span>; if the box's address ever changes, the robot needs reflashing over USB.</p>`;
  };

  // ---- new robot, or the same robot again ----
  G.targetHTML = () => {
    const S = G.S, f = S.form;
    const opts = [["new", "A new robot", "It gets its own device key and pairing code."]]
      .concat(S.robots.map((r) => [r.id, `Reflash ${r.name}`, "Same pairing code, new device key — the old key stops working."]));
    return `<fieldset class="choices"><legend>Which robot is this</legend>${opts.map(([v, l, em]) =>
      `<label class="choice${f.target === v ? " sel" : ""}"><input type="radio" name="tgt" data-act="target" data-arg="${v}"${f.target === v ? " checked" : ""}><span class="tx">${G.esc(l)}<em>${em}</em></span></label>`).join("")}</fieldset>
      ${f.target === "new" ? `<label class="field">Its name<input id="f-name" data-bind="name" value="${G.esc(f.name)}" placeholder="e.g. Quorra"></label>` : ""}`;
  };

  G.flashReady = () => {
    const S = G.S, f = S.form;
    return S.usb !== "none" && S.networks.some((n) => n.on) && (f.target !== "new" || f.name.trim()) && S.flash.state !== "running";
  };
  G.whyNot = () => {
    const S = G.S, f = S.form;
    if (S.usb === "none") return "Plug the robot into the box first.";
    if (!S.networks.some((n) => n.on)) return "Pick a network for it to join.";
    if (f.target === "new" && !f.name.trim()) return "Give it a name — it's how you tell robots apart.";
    return "";
  };
  G.reinstallHTML = () => G.S.usb === "mp"
    ? `<label class="choice${G.S.form.reinstall ? " sel" : ""}"><input type="checkbox" data-act="reinstall"${G.S.form.reinstall ? " checked" : ""}><span class="tx">Reinstall MicroPython too<em>Only for a robot that won't start. Otherwise just its own files are replaced.</em></span></label>`
    : "";

  // ---- the flash itself ----
  const PHASES = [["load", "Load MicroPython"], ["copy", "Copy its firmware and Wi-Fi"], ["join", "Join Wi-Fi and reach the box"]];
  const WORD = { wait: "waiting", run: "running", done: "done", fail: "failed", skip: "already on it" };
  G.phasesHTML = () => {
    const F = G.S.flash;
    return `<ul class="phases">${PHASES.filter(([id]) => !(F.rotate && id === "load")).map(([id, l]) => {
      const st = F.phases[id] || "wait";
      return `<li class="${st}"><span class="d"></span><span class="lbl">${l}</span><span class="w">${WORD[st]}</span></li>`;
    }).join("")}</ul>`;
  };
  G.logHTML = () => {
    const F = G.S.flash;
    if (!F.log.length) return "";
    return `<pre class="log" aria-live="polite">${F.log.map((l) => {
      const c = /^(ERROR|FAILED)/.test(l) ? "e" : /^(OK|PAIRING CODE)/.test(l) ? "k" : "";
      return c ? `<span class="${c}">${G.esc(l)}</span>` : G.esc(l);
    }).join("\n")}</pre>`;
  };
  G.failHTML = () => {
    const F = G.S.flash;
    if (F.state !== "fail") return "";
    return F.fail === "fail-load"
      ? `<p class="msg bad"><strong>MicroPython did not load</strong> — the Pico isn't in its USB loader. Unplug it, hold the white BOOTSEL button while plugging it back into the box, then flash again. Nothing on it was changed.</p>`
      : `<p class="msg bad"><strong>Copied, but it couldn't join Wi-Fi</strong> — none of its networks answered. Check they are 2.4 GHz and in range of the robot, then flash again; MicroPython is already on it, so the next try is quicker.</p>`;
  };

  function bar(p) { const n = Math.round(p * 0.3); return "=".repeat(n) + " ".repeat(30 - n); }

  function script(opts) {
    const S = G.S, out = [], on = S.networks.filter((n) => n.on);
    const L = (ms, line, ph, st, end) => out.push({ ms, line, ph, st, end });
    if (opts.rotate) {
      L(200, `New device key for ${opts.name}, written over USB on /dev/ttyACM0`);
      L(300, "mpremote: copying the new key", "copy", "run");
      L(700, "cp secrets.py :secrets.py   (new device key — the old one stops working now)");
      L(400, "Restarting the robot", "copy", "done");
    } else {
      L(150, `GrowBot (Raspberry Pi Pico 2 W) on ${S.usb === "blank" ? "the RP2350 bootloader" : "/dev/ttyACM0"}`);
      if (S.usb === "blank" || S.form.reinstall) {
        L(300, "picotool: loading MicroPython 1.28.0 (RPI_PICO2_W-20260406-v1.28.0.uf2)", "load", "run");
        if (S.usb === "mp") L(500, "Restarting the robot into its USB loader");
        if (S.outcome === "fail-load") {
          L(900, "ERROR: No accessible RP2350 devices in BOOTSEL mode were found.", "load", "fail");
          L(100, "FAILED: MicroPython did not load.", null, null, "fail-load");
          return out;
        }
        for (const p of [25, 60, 100]) L(380, `Loading into Flash: [${bar(p)}] ${p}%`);
        L(350, "Verifying Flash:    [==============================] 100%");
        L(300, "The device was rebooted to start the application.");
        L(700, "Waiting for the robot's serial port … /dev/ttyACM0", "load", "done");
      } else {
        L(100, "MicroPython 1.28.0 is already on it — skipping picotool", "load", "skip");
      }
      L(300, "mpremote: copying the robot's firmware", "copy", "run");
      for (const f of ["boot.py", "main.py", "servos.py", "gaits.py"]) L(220, `cp ${f} :${f}`);
      L(300, `cp secrets.py :secrets.py   (${on.length} network${on.length === 1 ? "" : "s"}, box ${S.boxIp}:8444 as jbrain.local, new device key)`);
      L(400, "Restarting the robot", "copy", "done");
    }
    L(200, "Waiting for it to join Wi-Fi …", "join", "run");
    if (S.outcome === "fail-wifi") {
      for (const n of on) L(1000, `Tried "${n.ssid}" — no answer`);
      L(200, "FAILED: it could not join any of its networks.", "join", "fail", "fail-wifi");
      return out;
    }
    L(1000, `Joined "${on[0].ssid}"`);
    L(500, `Reached the box at ${S.boxIp}:8444 with its device key`);
    if (!opts.rotate) L(300, `PAIRING CODE: ${opts.code}`);
    L(150, "OK", "join", "done", "ok");
    return out;
  }

  G.startFlash = (opts) => {
    const S = G.S, f = S.form;
    if (!opts.rotate && !G.flashReady()) return;
    const existing = opts.rotate ? G.robot(opts.robotId) : f.target === "new" ? null : G.robot(f.target);
    const code = existing ? existing.code : CODES[S.codeIx % CODES.length];
    const name = existing ? existing.name : f.name.trim();
    const steps = script({ rotate: opts.rotate, name, code });
    S.flash = { state: "running", phases: {}, log: [], fail: null, robotId: existing ? existing.id : null, rotate: !!opts.rotate };
    const F = S.flash;
    G.render();
    let i = 0;
    const tick = () => {
      if (S !== G.S || F !== S.flash) return; // reset mid-run
      const s = steps[i++];
      F.log.push(s.line);
      if (s.ph) F.phases[s.ph] = s.st;
      if (s.end) {
        if (s.end === "ok") {
          if (existing) {
            Object.assign(existing, { key: "active", minted: Date.now(), online: true, seen: Date.now() });
          } else {
            S.codeIx++;
            const r = { id: "n" + S.codeIx, name, code, key: "active", minted: Date.now(), online: true, seen: Date.now(), fresh: true };
            S.robots.push(r);
            F.robotId = r.id;
            f.name = "";
            f.target = "new";
          }
          S.usb = "mp";
          F.state = "ok";
        } else {
          F.state = "fail";
          F.fail = s.end;
          if (s.end === "fail-wifi") S.usb = "mp";
        }
        f.reinstall = false;
        G.render();
        if (G.onFlashEnd) G.onFlashEnd(F);
        return;
      }
      G.render();
      setTimeout(tick, steps[i].ms);
    };
    setTimeout(tick, steps[0].ms);
  };

  G.pairHTML = (r) => `<div class="pair"><span class="code"><span class="lbl">Pairing code</span>${r.code}</span>
      <button class="btn" data-act="copy" data-arg="${r.code}">Copy</button></div>
    <p class="hint dim">It's printed on the robot's page too. Open GrowBot below and it's already filled in.</p>`;

  // ---- rotate and revoke ----
  G.rotate = (id) => {
    const r = G.robot(id);
    if (G.S.usb === "none") { G.toast(`Plug ${r.name} into the box's USB first — the new key is written over the cable.`); return; }
    if (!G.arm("rot:" + id)) return;
    G.S.sheet = { kind: "rotate", id };
    G.startFlash({ rotate: true, robotId: id });
  };
  G.revoke = (id) => {
    const r = G.robot(id);
    if (!G.arm("rev:" + id)) return;
    r.key = "revoked";
    r.online = false;
    G.toast(`${r.name}'s key is revoked. It disconnects now and can't come back until it is reflashed.`);
  };
  G.rotateLabel = (id) => (G.isArmed("rot:" + id) ? "Tap again for a new key" : "New key");
  G.revokeLabel = (id) => (G.isArmed("rev:" + id) ? "Tap again to revoke" : "Revoke key");
  G.keyActs = (r) => r.key === "revoked" ? "" :
    `<button class="btn" data-act="rotate" data-arg="${r.id}">${G.rotateLabel(r.id)}</button>
     <button class="btn danger${G.isArmed("rev:" + r.id) ? " armed" : ""}" data-act="revoke" data-arg="${r.id}">${G.revokeLabel(r.id)}</button>`;

  // ---- Open GrowBot: a one-time link, then the box's own page on this phone ----
  G.openRobot = (id, fromNote) => {
    const S = G.S;
    if (!S.trusted && !S.certNoted && !fromNote) { S.sheet = { kind: "cert", id }; G.render(); return; }
    S.sheet = { kind: "opening", id, stage: 0 };
    G.render();
    setTimeout(() => { if (S.sheet && S.sheet.kind === "opening") { S.sheet.stage = 1; G.render(); } }, 700);
  };
  G.openLabel = (r) => r.key === "revoked" ? "Open GrowBot — legs off" : "Open GrowBot";

  G.sheetShell = (title, body, foot, progress) => `<div class="scrim" data-act-scrim><div class="sheet" role="dialog" aria-label="${G.esc(title)}">
      <div class="handle"></div><div class="sh"><span class="t">${G.esc(title)}</span><button class="x" data-act="sheet-close" aria-label="Close">&times;</button></div>
      ${progress ? `<div class="progress">${progress}</div>` : ""}
      <div class="sb" data-scroll="sheet">${body}</div>${foot ? `<div class="sf">${foot}</div>` : ""}</div></div>`;

  G.sharedSheet = () => {
    const S = G.S, sh = S.sheet;
    if (!sh) return "";
    const r = G.robot(sh.id);
    if (sh.kind === "cert") {
      return G.sheetShell("First time on this phone",
        `<p class="hint">GrowBot opens on the box's own address, <span class="mono">jbrain.local:8444</span>, with the box's certificate. A phone that hasn't been told to trust the box shows a warning page first.</p>
         <p class="hint">Install the box's certificate once and the warning is gone — for GrowBot and every other page on the box. The phone needs it for the camera and microphone.</p>
         <p class="hint dim">After downloading: on iPhone, open Settings → Profile Downloaded, then General → About → Certificate Trust Settings. On Android, Settings → Security → Install a certificate → CA certificate.</p>`,
        `<button class="btn block" data-act="cert-download">Download the box's certificate</button>
         <button class="btn primary block" data-act="cert-go" data-arg="${r.id}">Open GrowBot</button>`);
    }
    if (sh.kind === "opening") {
      const warn = !S.trusted ? `<p class="msg warn">If the phone shows a certificate warning, that is the box's own certificate — continue past it, or install it from the note on first open.</p>` : "";
      const legs = r.key === "revoked" ? `<p class="msg bad">${G.esc(r.name)}'s key is revoked, so GrowBot opens without its legs until it is reflashed.</p>`
        : !r.online || S.svc === "down" ? `<p class="msg warn">${G.esc(r.name)} isn't connected right now. GrowBot still opens; its legs move once the robot is on and reaches the box.</p>` : "";
      return G.sheetShell(`Open GrowBot — ${r.name}`,
        sh.stage === 0
          ? `<p class="hint">Making a one-time link for this phone…</p>`
          : `<div class="tab"><div class="url">https://jbrain.local:8444/?code=${r.code}</div><div class="pg">GrowBot · ${G.esc(r.name)}<br><span class="dim-t">(opened in a new tab)</span></div></div>
             <p class="hint">The link worked once and is spent. Open it again from here whenever you need it — on this phone or another.</p>${legs}${warn}`,
        `<button class="btn block" data-act="sheet-close">Done</button>`);
    }
    if (sh.kind === "rotate") {
      const F = S.flash;
      return G.sheetShell(`New key — ${r.name}`,
        `<p class="hint">The box makes a new key and writes it into ${G.esc(r.name)} over USB. The old key stops working the moment this finishes.</p>${G.phasesHTML()}${G.failHTML()}${G.logHTML()}
         ${F.state === "ok" ? `<p class="msg ok">Done — ${G.esc(r.name)} is back with its new key.</p>` : ""}`,
        F.state === "running" ? `<button class="btn block" disabled>Writing the new key…</button>` : `<button class="btn block" data-act="sheet-close">Done</button>`);
    }
    return "";
  };

  // ---- Ops → Services: the growbot row sits in Devices, next to its neighbours ----
  G.opsHTML = ({ growbotBody, growbotGroup }) => {
    const S = G.S, up = S.svc === "up";
    const connected = S.robots.filter((r) => r.online && r.key === "active" && up).length;
    const group = growbotGroup || "Devices";
    const row = (nm, what, ok, body, key, two) => {
      const open = !!S.open[key];
      return `<div class="srow"><button class="shead" data-act="toggle" data-arg="${key}" aria-expanded="${open}">
        <span class="sdot ${ok ? "ok" : "bad"}"></span><span class="sinfo"><span class="sline"><span class="snm">${nm}</span>
        <span class="badge ${ok ? "ok" : "bad"}">${ok ? "running" : "exited"}</span>${ok ? `<span class="badge ok">healthy</span>` : ""}</span>
        <span class="smeta">${what}</span>${two ? `<span class="smeta two">${two}</span>` : ""}</span><span class="scaret">&#8250;</span></button>
        ${open ? `<div class="sbody">${body}</div>` : ""}</div>`;
    };
    const plain = `<div class="acts" style="margin-top:0"><button class="btn" data-act="restart">Restart</button><button class="btn">Show log</button></div>`;
    const gbTwo = up
      ? `${connected} of ${S.robots.length} robot${S.robots.length === 1 ? "" : "s"} connected now`
      : `<span class="bad-t">Stopped 12 min ago — no robot can connect until it is back.</span>`;
    const gb = row("growbot", up ? "desk robots · since 10 Nov, 09:14" : "desk robots", up, growbotBody(), "svc-growbot", gbTwo);
    const groups = {
      Devices: [row("wall", "wall display · since 10 Nov, 09:14", true, plain, "svc-wall"),
        row("endpoint", "panel and robot flasher · since 10 Nov, 09:14", true, plain, "svc-endpoint")],
      Apps: [row("minecraft", "Bedrock server · since 9 Nov, 22:40", true, plain, "svc-mc"), row("jlaunch", "long compute jobs · since 10 Nov, 09:14", true, plain, "svc-jl")],
    };
    groups[group].push(gb);
    const card = (label) => {
      const bad = groups[label].some((h) => h.includes("sdot bad"));
      return `<section class="ops-card"><div class="ops-ch"><span class="t">${label}</span><span class="c">${groups[label].length}</span>
        <span class="state"><span class="dot ${bad ? "bad" : "ok"}"></span><span class="${bad ? "bad" : "ok"}-t">${bad ? "1 down" : "all up"}</span></span></div>${groups[label].join("")}</section>`;
    };
    return `<div class="topbar"><button class="back" data-act="screen" data-arg="${G.screens[0][0]}">Back</button><span class="ttl">Ops — Services</span></div>
      <div class="body" data-scroll="ops">
        <section class="ops-card"><div class="ops-ch"><span class="t">Core</span><span class="c">6</span><span class="state"><span class="dot ok"></span><span class="ok-t">all up</span></span></div></section>
        <section class="ops-card"><div class="ops-ch"><span class="t">Models</span><span class="c">5</span><span class="state"><span class="dot ok"></span><span class="ok-t">all up</span></span></div></section>
        ${card("Devices")}${card("Apps")}
      </div>`;
  };
  // The robots-connected-now list inside the growbot row, shared by every variant.
  G.connectedHTML = () => `<ul class="rlist">${G.S.robots.map((r) => `<li><span class="dot ${G.robotState(r).cls}"></span><span class="nm">${G.esc(r.name)}</span><span class="when">${G.robotState(r).text}</span></li>`).join("")}</ul>`;
})();
