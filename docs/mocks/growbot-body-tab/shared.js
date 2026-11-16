// The simulated robot and every part the three variants share, so they differ only in structure.
// The dashed bar above the phone is mock controls (theme, robot on, library size), not design.
window.B = (function () {
  const S = {
    theme: "dark", on: true, many: true, walk: "master", mode: "tap", secs: 5,
    angle: 0, len: 0.5, back: false, heading: 0, speed: 1, pitch: 1, driving: false, running: null, said: null,
    sheet: null, toast: "", follow: null, remote: false, pane: "drive",
  };
  // Library names as the box holds them; the part before ":" is the short name the pad shows.
  const WALKS_ALL = [
    { id: "master", name: "Master v3: turn, walk and back up, calmer backing", kind: "camera end first, with speed" },
    { id: "nose_s", name: "Nose walk, smoothed v1: back away, less leg chatter", kind: "nose first, with speed" },
    { id: "lift5", name: "Lift and fall v5: walk tall, on the robot as built", kind: "camera end first, heading only" },
    { id: "look10", name: "Look v10: turn on the spot to face a target", kind: "camera end first, heading only" },
    { id: "lift2", name: "Lift and fall, second slot v1: walk tall toward a heading", kind: "camera end first, heading only" },
    { id: "nose3", name: "Nose walk v3: back away at a commanded speed", kind: "nose first, with speed" },
  ];
  const walks = () => (S.many ? WALKS_ALL : WALKS_ALL.slice(0, 2));
  const short = (n) => n.split(":")[0];
  const gist = (n) => (n.includes(":") ? n.split(":").slice(1).join(":").trim() : "");
  const cur = () => walks().find((w) => w.id === S.walk) || null;
  const TRICKS = ["Wiggle", "Bow", "Sway", "Stand tall", "Get up", "Escape spin left", "Escape spin right", "Look v10"];
  const RUNS = [
    { n: 40, w: "10 h ago · 04:48 · 3 failed, 6 to look at" },
    { n: 36, w: "11 h ago · 00:07 · stopped at result" },
    { n: 26, w: "11 h ago · 00:19 · stopped at result" },
    { n: 1, w: "11 h ago · 00:50 · stopped at result" },
  ];
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const I = {
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 15l6-6 6 6"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>',
    ccw: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 12a8 8 0 1 0 2.5-5.8"/><path d="M4 4v4h4"/></svg>',
    cw: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 12a8 8 0 1 1-2.5-5.8"/><path d="M20 4v4h-4"/></svg>',
    stop: '<svg viewBox="0 0 24 24" width="18" height="18"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>',
  };
  const blocked = () => (S.on ? null : "Turn Fish on to move it.");

  // The pad's words follow the walk: Ahead / Left / Right / Back with a trained one.
  function keys() {
    const w = cur();
    const back = w && !w.kind.includes("speed") ? "Turn" : "Back";
    return w
      ? [["n", "forward", "Ahead", I.up], ["w", "left", "Left", I.ccw], ["e", "right", "Right", I.cw], ["s", "back", back, I.down]]
      : [["n", "forward", "Forward", I.up], ["w", "left", "Spin", I.ccw], ["e", "right", "Spin", I.cw], ["s", "back", "Back", I.down]];
  }
  const DOING = { forward: "Walking ahead", left: "Turning left", right: "Turning right", back: "Backing away" };

  function run(move, label) {
    if (blocked()) return;
    S.driving = false;
    S.running = { move, n: Date.now() };
    const w = cur();
    S.said = { text: `${label || DOING[move]}${w && !label ? ` with ${short(w.name)}` : ""} for ${S.secs} s.`, bad: false };
    const n = S.running.n;
    setTimeout(() => { if (S.running && S.running.n === n) { S.running = null; B.render(); } }, S.secs * 1000);
  }
  function stop() {
    S.running = null; S.driving = false; S.follow = null;
    S.said = { text: "Stopped. The legs are limp.", bad: false };
  }

  function pad() {
    const off = !!blocked();
    return `<div class="pad">${keys().map(([at, id, lbl, ic]) =>
      `<button class="k ${at}${S.running && S.running.move === id ? " on" : ""}" data-act="move" data-id="${id}" ${off ? "disabled" : ""} aria-label="${lbl} ${S.secs} s">${ic}<span>${lbl}</span></button>`).join("")}
      <button class="stop" data-act="stop" ${S.on ? "" : "disabled"}>${I.stop}<span>Stop</span></button></div>
      <div class="bar">${S.running ? `<i style="animation-duration:${S.secs}s" data-n="${S.running.n}"></i>` : ""}</div>`;
  }
  const said = () => `<p class="said${S.said && S.said.bad ? " bad" : ""}" aria-live="polite">${S.said ? esc(S.said.text) : "&nbsp;"}</p>`;
  const secs = (label) => `${label ? `<div class="sub">${label}</div>` : ""}<div class="segs" role="group" aria-label="How long">${[2, 5, 10, 15].map((s) =>
    `<button class="${S.secs === s ? "on" : ""}" data-act="secs" data-id="${s}">${s} s</button>`).join("")}</div>`;
  const headingWords = (t) => (t === 0 ? "straight" : t === 180 ? "180° right" : `${Math.abs(t)}° ${t < 0 ? "left" : "right"}`);
  // Steer live's dial: the robot at the centre, the arrow's angle is the heading and its length
  // the speed (owner, 2026-11-16: "a kind of half circle ... that shows origin and has a draggable
  // vector", then "can't we steer beyond 180°": the box takes any heading, so a whole circle).
  // Forward | Back flips the speed of a walk that takes one.
  const DS = 240, DR = 104, DO = 120;
  const dialAt = (ang, r) => { const t = (ang * Math.PI) / 180; return [DO + r * Math.sin(t), DO - r * Math.cos(t)]; };
  const wrap = (d) => { const x = ((((d + 180) % 360) + 360) % 360) - 180; return x === -180 ? 180 : x; };
  const speedOn = () => { const w = cur(); return !w || w.kind.includes("speed"); };
  function dialWords() {
    const hw = headingWords(S.angle);
    return speedOn() ? `${hw}, ${S.len === 0 ? "standing" : `speed ${((S.back ? -1 : 1) * S.len).toFixed(1)}`}` : hw;
  }
  function dialSvg() {
    const [tx, ty] = dialAt(S.angle, Math.max(S.len, 0.04) * DR);
    const ticks = [0, 45, 90, 135, 180, -135, -90, -45].map((t) => { const [x1, y1] = dialAt(t, DR - (t === 0 ? 14 : 8)), [x2, y2] = dialAt(t, DR); return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${t === 0 ? "var(--text)" : "var(--text-3)"}" stroke-width="${t === 0 ? 2.5 : 1.5}"/>`; }).join("");
    return `<div id="dial" role="slider" tabindex="0" aria-label="Heading and speed" aria-valuetext="${dialWords()}" style="display:block;width:100%;max-width:260px;margin:12px auto 0;touch-action:none;cursor:grab"><svg viewBox="0 0 ${DS} ${DS}" style="display:block;width:100%;height:auto" aria-hidden="true">
      <circle cx="${DO}" cy="${DO}" r="${DR}" fill="var(--surface-2)" stroke="var(--border)"/>
      ${speedOn() ? `<circle cx="${DO}" cy="${DO}" r="${DR / 2}" fill="none" stroke="var(--border)" stroke-dasharray="3 4"/>` : ""}${ticks}
      <line id="dial-vec" x1="${DO}" y1="${DO}" x2="${tx}" y2="${ty}" stroke="var(--steel)" stroke-width="4" stroke-linecap="round"/>
      <circle cx="${DO}" cy="${DO}" r="6" fill="var(--text-2)"/>
      <circle id="dial-tip" cx="${tx}" cy="${ty}" r="13" fill="var(--steel-tint)" stroke="var(--steel)" stroke-width="2"/></svg></div>
      <p class="said" id="dial-said" style="color:var(--text);font-family:var(--font-mono)">${dialWords()}</p>`;
  }
  function dialMove(e) {
    const el = document.getElementById("dial"); if (!el) return;
    const box = el.getBoundingClientRect();
    const x = ((e.clientX - box.left) / box.width) * DS - DO, y = DO - ((e.clientY - box.top) / box.height) * DS;
    S.angle = wrap(Math.round((Math.atan2(x, y) * 180) / Math.PI / 5) * 5);
    S.len = speedOn() ? Math.max(0, Math.min(1, Math.round((Math.hypot(x, y) / DR) * 10) / 10)) : 1;
    const [tx, ty] = dialAt(S.angle, Math.max(S.len, 0.04) * DR);
    const v = document.getElementById("dial-vec"), t = document.getElementById("dial-tip");
    v.setAttribute("x2", tx); v.setAttribute("y2", ty); t.setAttribute("cx", tx); t.setAttribute("cy", ty);
    el.setAttribute("aria-valuetext", dialWords());
    document.getElementById("dial-said").textContent = dialWords();
  }
  function sliders() {
    return `${dialSvg()}
      ${speedOn() ? `<div class="segs" role="group" aria-label="Which way" style="margin-bottom:8px"><button class="${S.back ? "" : "on"}" data-act="way" data-id="fwd">Forward</button><button class="${S.back ? "on" : ""}" data-act="way" data-id="back">Back</button></div>` : ""}
      <div class="sl"><label for="pi">Camera height</label><input id="pi" type="range" min="-1" max="1" step="0.1" value="${S.pitch}" data-sl="pitch"><output>${(+S.pitch).toFixed(1)}</output></div>
      <button class="btn block go${S.driving ? " on" : ""}" data-act="drive" ${S.on || S.driving ? "" : "disabled"}>${S.driving ? "Stop driving" : "Start driving"}</button>
      ${S.driving ? "" : `<button class="btn block danger" style="margin-top:8px" data-act="stop" ${S.on ? "" : "disabled"}>Stop</button>`}`;
  }
  const picker = () => {
    const w = cur();
    return `<button class="picker" data-act="sheet" data-id="walks"><span class="tx"><span class="k">Walking with</span><span class="v">${w ? esc(short(w.name)) : "Built-in walk"}</span></span><span class="chev">›</span></button>`;
  };
  const tricks = () => `<div class="strip">${TRICKS.map((t) =>
    `<button class="chip${S.running && S.running.move === t ? " lit" : ""}" data-act="trick" data-id="${esc(t)}" ${S.on ? "" : "disabled"}>${esc(t)}</button>`).join("")}</div>`;
  const tip = () => (blocked() ? `<p class="tip blocked">${blocked()}</p>` : "");
  const head = (title) => `<div class="card-head"><h3>${title}</h3><span class="state"><span class="dot ${S.on ? "ok" : ""}"></span>${S.on ? '<span class="ok-t">Ready</span>' : "Off"}</span></div>`;
  const robot = () => `<section class="card"><div class="who"><span class="av">S</span><span class="tx"><b>Sprout</b> is wearing Fish, on Pixel 6a.<br>${S.on ? "On · seen 4 s ago" : "Off · last seen 3 h ago"}</span></div><button class="row" style="padding:8px 0 0;margin-top:10px" data-act="sheet" data-id="robot"><span class="tx">Who wears Fish, and its key<em>Pairing code, New key, Revoke key</em></span><span class="chev">›</span></button></section>`;

  const follow = () => `<p class="hint">Fish locks onto the nearest face its front camera sees and keeps that distance. Say "stop stop" to end it.</p>
    <div class="acts" style="margin-top:0">
      <button class="btn${S.follow === "walk" ? " primary" : ""}" data-act="follow" data-id="walk" ${S.on ? "" : "disabled"}>${S.follow === "walk" ? "Stop following" : "Follow me"}</button>
      <button class="btn${S.follow === "dry" ? " primary" : ""}" data-act="follow" data-id="dry" ${S.on ? "" : "disabled"}>${S.follow === "dry" ? "Stop dry run" : "Dry run"}</button></div>`;
  const test = () => `<p class="hint">A guided trial on the robot's phone that saves every reading to the box. Last run: <b>#40</b>, 3 failed, 6 to look at.</p>
    <button class="btn block primary" data-act="toast" data-id="Starting the body test on Pixel 6a…" ${S.on ? "" : "disabled"}>Start a body test</button>`;
  const legs = () => `<p class="hint">Which leg, straighten (trim) and direction, the three-step leg check. Saved on the box.</p>
    <div class="sl"><label>Left trim</label><input type="range" min="-30" max="30" value="4"><output>+4°</output></div>
    <div class="sl"><label>Right trim</label><input type="range" min="-30" max="30" value="-2"><output>−2°</output></div>
    <button class="btn block" data-act="toast" data-id="This is the live Legs pane, unchanged.">Open the full leg check</button>`;
  const runs = () => `<ul class="runs">${RUNS.map((r) => `<li><span class="n">#${r.n}</span><span class="w">${r.w}</span><button class="linkbtn" data-act="toast" data-id="Saved bodytest-${r.n}.json">log</button></li>`).join("")}</ul>`;
  const dev = (open) => `<details class="dev"${open ? " open" : ""}><summary>For GrowBot's developer <span class="dim-t">Brain log · walks and spins</span><span class="chev">›</span></summary><div class="in">
      <button class="btn block" data-act="toast" data-id="(the brain log sheet, unchanged)">Share a brain log…</button>
      <div class="rows"><button class="row" data-act="toast" data-id="(the Walks and spins card, unchanged)"><span class="tx">Walks and spins<em>Same as the official GrowBot repo · checked 2 h ago</em></span><span class="state"><span class="dot ok"></span></span><span class="chev">›</span></button></div>
    </div></details>`;

  function sheet() {
    if (!S.sheet) return "";
    let t = "", b = "";
    if (S.sheet === "walks") {
      t = "Walk with";
      const opt = (id, nm, g, k) => `<button class="walk${S.walk === id ? " sel" : ""}" data-act="walk" data-id="${id}"><span class="r"></span><span class="tx"><span class="nm">${esc(nm)}</span>${g ? `<span class="gist">${esc(g)}</span>` : ""}<span class="kind">${k}</span></span></button>`;
      b = `<div class="walks">${opt("", "Built-in walk", "Fish's stock walk and spins", "no training needed")}${walks().map((w) => opt(w.id, short(w.name), gist(w.name), w.kind)).join("")}</div>
        <button class="btn block" style="margin-top:12px" data-act="toast" data-id="(the Trained walks library, unchanged)">Manage trained walks…</button>`;
    } else if (S.sheet === "follow") { t = "Follow me"; b = follow(); }
    else if (S.sheet === "test") { t = "Body test"; b = `${test()}<div class="sub">Past runs</div>${runs()}`; }
    else if (S.sheet === "legs") { t = "Legs"; b = legs(); }
    else if (S.sheet === "tricks") { t = "Tricks"; b = `<div class="acts" style="margin-top:0">${TRICKS.map((x) => `<button class="chip" data-act="trick" data-id="${esc(x)}" ${S.on ? "" : "disabled"}>${esc(x)}</button>`).join("")}</div>`; }
    else if (S.sheet === "robot") { t = "Fish"; b = `<dl class="facts" style="margin-top:0"><dt>Pairing code</dt><dd class="mono">gb-e3b6d8</dd><dt>Device key</dt><dd>made Nov 10, only on Fish</dd></dl><div class="sub">Who wears Fish</div><div class="choices"><label class="choice"><input type="radio" name="w"><span class="tx">Nobody<em>Fish stands still.</em></span></label><label class="choice sel"><input type="radio" name="w" checked><span class="tx">Sprout<em>on Pixel 6a</em></span></label><label class="choice"><input type="radio" name="w"><span class="tx">Captain Bolt<em>not open</em></span></label></div><div class="acts"><button class="btn">New key</button><button class="btn danger">Revoke key</button></div>`; }
    return `<div class="scrim" data-act="close"><div class="sheet" role="dialog" aria-label="${t}"><div class="handle"></div><div class="sh"><span class="t">${t}</span><button class="x" data-act="close" aria-label="Close">×</button></div><div class="sb">${b}</div></div></div>`;
  }
  const toast = () => (S.toast ? `<div class="toast">${esc(S.toast)}</div>` : "");
  const chrome = (inner) => `<div class="topbar"><button class="back">‹</button><span class="ttl">GrowBot</span></div>
    <div class="seg"><button>Personas <span class="n">2</span></button><button class="on">Body</button><button>Phones</button></div>
    <div class="body">${inner}</div>`;

  let paint = () => {};
  function mockbar(el, which, render) {
    paint = render;
    const link = (k, f, n) => `<a href="${f}" style="color:${k === which ? "#7fa7c9" : "#c9ccd1"}">${k} · ${n}</a>`;
    const draw = () => {
      el.innerHTML = `<div class="mb-title">Mock controls (not design) · GrowBot Body tab redesign · ${link("A", "a-remote-rows.html", "remote + rows")} · ${link("B", "b-tabs.html", "tabs")} · ${link("C", "c-cockpit.html", "cockpit")}</div>
        <div class="mb-row"><button data-mb="on" class="${S.on ? "on" : ""}">Fish ${S.on ? "on" : "off"}</button><button data-mb="many" class="${S.many ? "on" : ""}">${S.many ? "6 trained walks" : "2 trained walks"}</button><button data-mb="theme">${S.theme} theme</button></div>`;
    };
    el.addEventListener("click", (e) => {
      const b = e.target.closest("[data-mb]"); if (!b) return;
      const k = b.dataset.mb;
      if (k === "on") { S.on = !S.on; if (!S.on) { S.running = null; S.driving = false; S.follow = null; } }
      if (k === "many") { S.many = !S.many; if (!cur()) S.walk = ""; }
      if (k === "theme") { S.theme = S.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = S.theme; }
      draw(); render();
    });
    draw();
  }

  function wire(root) {
    root.addEventListener("click", (e) => {
      const el = e.target.closest("[data-act]"); if (!el || !root.contains(el)) return;
      if (el.dataset.act === "close" && e.target !== el && el.classList.contains("scrim")) return;
      const a = el.dataset.act, id = el.dataset.id;
      if (a === "move") run(id);
      else if (a === "stop") stop();
      else if (a === "secs") S.secs = +id;
      else if (a === "mode") { S.mode = id; S.running = null; if (id === "tap") S.driving = false; }
      else if (a === "pane") S.pane = id;
      else if (a === "way") S.back = id === "back";
      else if (a === "drive") { S.driving = !S.driving; S.running = null; S.said = S.driving ? { text: `Driving with ${cur() ? short(cur().name) : "the built-in walk"}. Drag the arrow as it goes.`, bad: false } : { text: "Stopped driving.", bad: false }; }
      else if (a === "trick") { run(id, id === "Look v10" ? "Holding Look v10" : `${id}`); S.sheet = null; }
      else if (a === "follow") { S.follow = S.follow === id ? null : id; S.said = S.follow ? { text: id === "walk" ? "Following the nearest face." : "Dry run: the arrow on Fish's screen shows the way.", bad: false } : null; }
      else if (a === "sheet") S.sheet = id;
      else if (a === "close") S.sheet = null;
      else if (a === "walk") { S.walk = id; S.sheet = null; S.driving = false; }
      else if (a === "remote") S.remote = id === "open";
      else if (a === "toast") { S.toast = id; setTimeout(() => { S.toast = ""; paint(); }, 2200); }
      paint();
    });
    let held = false;
    root.addEventListener("pointerdown", (e) => {
      if (!e.target.closest("#dial")) return;
      held = true; e.target.closest("#dial").setPointerCapture(e.pointerId); dialMove(e);
    });
    root.addEventListener("pointermove", (e) => { if (held) dialMove(e); });
    root.addEventListener("pointerup", () => { held = false; });
    root.addEventListener("pointercancel", () => { held = false; });
    root.addEventListener("input", (e) => {
      const k = e.target.dataset && e.target.dataset.sl; if (!k) return;
      S[k] = +e.target.value;
      const o = e.target.nextElementSibling;
      if (o) o.textContent = k === "heading" ? headingWords(S[k]) : S[k].toFixed(1);
    });
  }
  return { S, I, pad, said, secs, sliders, picker, tricks, tip, head, robot, follow, test, legs, runs, dev, sheet, toast, chrome, mockbar, wire, cur, short, blocked, headingWords, render: () => paint() };
})();
