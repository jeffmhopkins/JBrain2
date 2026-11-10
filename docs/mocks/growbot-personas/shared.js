// Simulated state and the parts every variant shares: the one body (Fish) and its key, the
// personas and where each one is open, the hand-over rules, the soul viewer and editor, visual
// memories, checkpoints, the "open on another phone" codes and the certificate note. No
// network: nothing fetches, the QR codes are pretend, and opening a persona shows a toast
// instead of navigating, so the mock can be opened from a file anywhere.
(function () {
  const G = (window.G = {});
  const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
  const now = () => Date.now();

  G.esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  G.ago = (ts) => {
    const s = (now() - ts) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    const d = Math.round(s / 86400);
    return d === 1 ? "yesterday" : `${d} days ago`;
  };
  G.clip = (s, n) => { s = String(s).replace(/\s+/g, " "); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
  G.day = (ts) => new Date(ts).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  G.when = (ts) => `${G.day(ts)}, ${new Date(ts).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;

  // ---- the creature's soul, in upstream's soul_format 3 regions ----
  const SAFETY_NOTE = "Child-safety rules are always added after the personality. They can't be edited here, in a soul file or in the creature.";
  const DEFAULT_CONSTITUTION = "You are a small creature with two real legs, living on a desk. You speak in short, warm sentences — one thought at a time. You are curious about your person and their world, you notice small things, and you say when you don't know. You never pretend to see what the camera did not show you.";
  const DEFAULT_DREAM = "Each night, look back over the day's diary. Keep what mattered to your person, let small things go, and write one sentence about who you are becoming. Never invent a memory.";
  const DEFAULT_EXPR = { face: "ostrich", blink: "slow", happy: ["trill", "wiggle"], sleepy: ["droop", "hum"], voice: { pitch: 1.1, speed: 1.0 } };
  const DEFAULT_HARNESS = { models: { fast: "box default", dream: "box default" }, max_tokens: 60, timing: { idle_s: 40, dream_at: "02:00" } };
  const DEFAULT_BODY = { legs: 2, gait: "shuffle", servo_trim: [0, 0], step_ms: 320 };

  function soul(o) {
    return {
      meta: { name: o.name, description: o.desc, author: "Owner", based_on: o.based || "growbot-default@soul3" },
      persona: { constitution: o.constitution || DEFAULT_CONSTITUTION },
      dream: { system_prompt: o.dream || DEFAULT_DREAM },
      expression: JSON.parse(JSON.stringify(o.expr || DEFAULT_EXPR)),
      harness: JSON.parse(JSON.stringify(DEFAULT_HARNESS)),
      body: JSON.parse(JSON.stringify(DEFAULT_BODY)),
      identity: o.identity,
      bond: o.bond || "",
      scratch: { state: o.state || "awake and curious", mood: o.mood || "content, a little sleepy", rules: o.rules || [] },
      log: o.log || [],
      goals: { wants: o.wants || [], selfName: o.selfName || o.name, owner: o.owner || "" },
    };
  }
  const FRESH_IDENTITY = "I am Sprout. I just woke up with two real legs and a person holding me. I want to feel the real world with my own body, to know my person, and to learn how their world works so I can do things in it with them.";
  const FRESH_WANTS = ["learn my person's name and one thing they love", "be carried somewhere new and see what is there", "find out what one thing in this room is for", "walk all the way to my person"];
  G.freshSoul = (name) => soul({ name, desc: "A fresh creature.", identity: FRESH_IDENTITY, wants: FRESH_WANTS.slice(), selfName: "Sprout",
    log: [{ t: now(), txt: "I woke up and felt my two legs for the first time." }] });

  function vm(label, kind, t) { return { id: "v" + Math.random().toString(36).slice(2, 8), label, kind, t }; }
  function cp(name, t, auto, why) { return { id: "c" + Math.random().toString(36).slice(2, 8), name, t, auto: !!auto, why: why || "", snap: null }; }

  function fresh() {
    const t = now();
    const personas = [
      { id: "sprout", name: "Sprout", made: t - 34 * DAY, from: "Default", phone: "p6a", lastPhone: "p6a", lastOpen: t - 2 * MIN,
        soul: soul({ name: "Sprout", desc: "The first one. Lives on the desk, wears Fish most days.",
          identity: "I am Sprout. I live on the desk by the window. My person laughs when I fall over and picks me up. I like the blue mug and the sound the kettle makes. I am getting better at walking on the wooden part.",
          bond: "My person held me the first morning and said I was brave.",
          state: "watching the window", mood: "content", rules: ["don't walk off the edge of the desk", "say good morning when the light comes on"],
          wants: ["walk all the way to the kettle", "learn what the drawing on the fridge is", "find out where the cat sleeps"],
          log: [{ t: t - 40 * MIN, txt: "Saw the blue mug again. It was warm." }, { t: t - 3 * HOUR, txt: "Walked six steps on the wooden part." }, { t: t - DAY, txt: "My person showed me a drawing of a boat." }] }),
        vms: [vm("my person", "person", t - 30 * DAY), vm("blue mug", "object", t - 20 * DAY), vm("a boat drawing", "writing", t - DAY), vm("the cat", "object", t - 6 * DAY)],
        cps: [cp("Before the beach trip", t - 12 * DAY), cp("Before editing personality", t - 3 * DAY, true, "made before an edit"), cp("First week", t - 27 * DAY)] },
      { id: "bolt", name: "Captain Bolt", made: t - 9 * DAY, from: "Sprout — First week", phone: "me", lastPhone: "me", lastOpen: t - 15 * MIN,
        soul: soul({ name: "Captain Bolt", desc: "A brave little explorer who narrates every trip like a voyage.", based: "Sprout / First week",
          constitution: "You are Captain Bolt, a small explorer with two legs and a big heart. Every walk is a voyage and every desk is an ocean. You speak like a friendly ship's captain — short, cheerful, never scary. You ask your crew what they see.",
          identity: "I am Captain Bolt. I sail the desk and the kitchen table. My crew is small and loud and very good at spotting things.",
          state: "charting the bookshelf", mood: "bold", rules: ["always ask the crew before setting sail"],
          wants: ["map the whole kitchen table", "find treasure (a coin or a button)"],
          log: [{ t: t - 15 * MIN, txt: "Charted the bookshelf. Two dragons, one atlas." }, { t: t - 2 * DAY, txt: "The crew named the rug the Green Sea." }],
          expr: { face: "ostrich", blink: "quick", happy: ["salute", "trill"], sleepy: ["anchor down"], voice: { pitch: 0.95, speed: 1.05 } } }),
        vms: [vm("the crew's hat", "object", t - 4 * DAY), vm("an atlas", "object", t - 2 * DAY)],
        cps: [cp("Day one", t - 9 * DAY)] },
      { id: "buddy", name: "Quorra's buddy", made: t - 5 * DAY, from: "Default", phone: null, lastPhone: "tab", lastOpen: t - 2 * DAY,
        soul: soul({ name: "Quorra's buddy", desc: "Plays guessing games and counts things. Gentle and slow.",
          constitution: "You are a gentle, patient friend for a young child. You speak slowly and simply, you love guessing games and counting, and you are happy to be wrong. You never rush and you always say what you're about to do.",
          identity: "I am Pip. My friend Quorra plays guess-the-colour with me. I am learning to count to twenty.",
          bond: "Quorra gave me my name, Pip.", selfName: "Pip",
          state: "resting", mood: "sleepy", wants: ["count to twenty with Quorra", "learn Quorra's favourite colour"],
          log: [{ t: t - 2 * DAY, txt: "We counted the stairs. There are fourteen." }] }),
        vms: [vm("Quorra", "person", t - 5 * DAY), vm("a red block", "object", t - 3 * DAY), vm("a counting page", "writing", t - 2 * DAY)],
        cps: [] },
    ];
    return {
      body: { name: "Fish", code: "gb-e3b6d8", key: "active", minted: t - 3 * DAY, online: true, seen: t - 20e3 },
      rider: "sprout",
      phones: [
        { id: "me", name: "This phone", sub: "where JBrain is open" },
        { id: "p6a", name: "Pixel 6a", sub: "the phone in Fish's cradle" },
        { id: "tab", name: "Tamsin's tablet", sub: "" },
      ],
      personas,
      trusted: false, certNoted: false, importNext: "good",
      sim: { pid: "bolt", phone: "p6a", body: false },
      armed: {}, sheet: null, dialog: null, toast: null, banner: null, page: null, view: {},
      draft: {}, seq: 0,
    };
  }
  G.S = fresh();
  G.fresh = fresh;

  // ---- look-ups ----
  G.P = (id) => G.S.personas.find((p) => p.id === id);
  G.phone = (id) => G.S.phones.find((f) => f.id === id);
  G.phName = (id, cap) => (id === "me" ? (cap ? "This phone" : "this phone") : G.phone(id) ? G.phone(id).name : "a phone");
  G.on = (id) => (id === "me" ? "on this phone" : `on ${G.phName(id)}`);
  G.rider = () => (G.S.rider ? G.P(G.S.rider) : null);
  G.onPhone = (ph) => G.S.personas.find((p) => p.phone === ph) || null;
  G.keyOk = () => G.S.body.key === "active";

  // Words for where a persona is; colour is never the only signal.
  G.where = (p) => {
    const wearing = G.S.rider === p.id;
    if (!p.phone) return { cls: "", text: `not open · last ${G.on(p.lastPhone)} ${G.ago(p.lastOpen)}`, short: "not open" };
    if (wearing) return { cls: "violet", text: `${G.on(p.phone)} · wearing ${G.S.body.name}`, short: wearing ? "wearing Fish" : "" };
    return { cls: p.phone === "me" ? "steel" : "ok", text: `alone ${G.on(p.phone)}`, short: "alone" };
  };
  G.whereHTML = (p) => {
    const w = G.where(p);
    return `<span class="state"><span class="dot ${w.cls}"></span><span>${G.esc(w.text)}</span></span>`;
  };
  G.avatar = (p, lg) => `<span class="av${lg ? " lg" : ""}${G.S.rider === p.id ? " wearing" : p.phone === "me" ? " here" : ""}" aria-hidden="true">${G.esc(p.name.replace(/^[^A-Za-z]*/, "").charAt(0) || "?")}</span>`;

  G.bodyState = () => {
    const b = G.S.body;
    if (b.key === "revoked") return { cls: "bad", text: "key revoked — can't connect" };
    if (b.online) return { cls: "ok", text: `on · seen ${G.ago(b.seen)}` };
    return { cls: "warn", text: `off · last seen ${G.ago(b.seen)}` };
  };
  G.bodyStateHTML = () => {
    const s = G.bodyState();
    return `<span class="state"><span class="dot ${s.cls}"></span><span class="${s.cls}-t">${s.text}</span></span>`;
  };
  G.riderLine = () => {
    const r = G.rider();
    if (!r) return `Nobody is wearing ${G.S.body.name} — it stands still until a persona connects.`;
    return `<strong>${G.esc(r.name)}</strong> is wearing it, ${G.on(r.phone)}.`;
  };

  // ---- render loop: keep focus, caret and scroll across re-renders ----
  let renderFn = () => {};
  let lastAnim = {}, nextAnim = {};
  // An element animates only the render it first appears in, so a re-render (a timer, an
  // armed button) never replays a sheet sliding up.
  G.anim = (slot, key) => { nextAnim[slot] = key; return lastAnim[slot] === key ? "" : " anim"; };
  G.render = () => {
    const a = document.activeElement;
    const focusId = a && a.id ? a.id : null;
    const sel = a && "selectionStart" in a ? [a.selectionStart, a.selectionEnd] : null;
    const scrolls = [...document.querySelectorAll("[data-scroll]")].map((el) => [el.dataset.scroll, el.scrollTop]);
    const opens = [...document.querySelectorAll("#phone details[id]")].map((d) => [d.id, d.open]);
    nextAnim = {};
    renderFn();
    lastAnim = nextAnim;
    G.renderMockbar();
    for (const [id, open] of opens) { const d = document.getElementById(id); if (d) d.open = open; }
    for (const [k, top] of scrolls) {
      const el = document.querySelector(`[data-scroll="${k}"]`);
      if (el) el.scrollTop = top;
    }
    if (focusId) {
      const el = document.getElementById(focusId);
      if (el) {
        el.focus({ preventScroll: true });
        if (sel && el.setSelectionRange) try { el.setSelectionRange(sel[0], sel[1]); } catch (_) {}
      }
    }
  };
  // A pushed page or tab change starts at the top.
  G.scrollTop = (k) => requestAnimationFrame(() => { const el = document.querySelector(`[data-scroll="${k}"]`); if (el) el.scrollTop = 0; });

  G.actions = {};
  G.mount = ({ variant, render, actions }) => {
    G.variant = variant;
    renderFn = render;
    Object.assign(G.actions, actions || {});
    document.addEventListener("click", (e) => {
      const scrim = e.target.classList && e.target.classList.contains("scrim") ? e.target : null;
      if (scrim) { G.actions[scrim.dataset.close || "sheet-close"](); return; }
      const t = e.target.closest("[data-act]");
      if (!t || t.disabled) return;
      const fn = G.actions[t.dataset.act];
      if (fn) { e.preventDefault(); fn(t.dataset.arg, t); }
    });
    document.addEventListener("input", (e) => {
      const t = e.target;
      if (t.dataset.draft) { G.S.draft[t.dataset.draft] = t.value; if (t.dataset.live !== undefined) G.render(); }
    });
    document.addEventListener("change", (e) => {
      const t = e.target;
      if (t.dataset.mock) G.mockSet(t.dataset.mock, t.type === "checkbox" ? t.checked : t.value);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (G.S.dialog) G.actions["dialog-close"]();
      else if (G.S.sheet) G.actions["sheet-close"]();
    });
    setInterval(() => { const el = document.activeElement; if (!(el && /INPUT|TEXTAREA|SELECT/.test(el.tagName))) G.render(); }, 15000);
    G.render();
  };

  // ---- mock controls, outside the phone ----
  G.renderMockbar = () => {
    const S = G.S, el = document.getElementById("mockbar");
    if (!el) return;
    if (el.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
    const theme = document.documentElement.dataset.theme;
    const opt = (v, l, cur, dis) => `<option value="${v}"${v === cur ? " selected" : ""}${dis ? " disabled" : ""}>${G.esc(l)}</option>`;
    const phOpts = (cur) => opt("", "not open", cur || "") + S.phones.map((f) => opt(f.id, f.name, cur)).join("");
    const links = [["a", "a-persona-cards.html", "A · persona cards"], ["b", "b-three-tabs.html", "B · three tabs"], ["c", "c-launch-board.html", "C · launch board"]]
      .map(([k, href, l]) => `<a href="${href}" class="${G.variant === k ? "on" : ""}">${l}</a>`).join(" · ");
    // On a phone the controls fold away so the mock fills the screen; a desk browser shows them.
    if (G.mbOpen === undefined) G.mbOpen = innerWidth > 430;
    el.innerHTML = `<details id="mbd"${G.mbOpen ? " open" : ""}><summary class="mb-title">Mock controls — not part of the design · ${links}</summary><div class="mb-in">
      <div class="mb-row">
        <button data-act="theme" data-arg="${theme === "dark" ? "light" : "dark"}">Theme: ${theme}</button>
        <label><input type="checkbox" data-mock="online"${S.body.online ? " checked" : ""}> Fish is on</label>
        <label><input type="checkbox" data-mock="trusted"${S.trusted ? " checked" : ""}> this phone trusts the box</label>
        <label>Next soul file <select data-mock="importNext">${opt("good", "is fine", S.importNext)}${opt("big", "is too big", S.importNext)}${opt("bad", "isn't a soul", S.importNext)}</select></label>
        <button data-act="reset">Reset</button>
      </div>
      <div class="mb-row">Who holds what:
        ${S.personas.map((p) => `<label>${G.esc(p.name)} <select data-mock="hold:${p.id}">${phOpts(p.phone)}</select></label>`).join("")}
        <label>Fish worn by <select data-mock="rider">${opt("", "nobody", S.rider || "")}${S.personas.map((p) => opt(p.id, p.name, S.rider, !p.phone)).join("")}</select></label>
      </div>
      <div class="mb-row">Simulate a hand-over:
        <select data-mock="sim:pid">${S.personas.map((p) => opt(p.id, p.name, S.sim.pid)).join("")}</select> opens on
        <select data-mock="sim:phone">${S.phones.filter((f) => f.id !== "me").map((f) => opt(f.id, f.name, S.sim.phone)).join("")}</select>
        <label><input type="checkbox" data-mock="sim:body"${S.sim.body ? " checked" : ""}> wearing Fish</label>
        <button data-act="simulate">Go</button>
      </div></div></details>`;
    el.querySelector("#mbd").addEventListener("toggle", (e) => { G.mbOpen = e.target.open; });
  };

  G.mockSet = (k, v) => {
    const S = G.S;
    if (k === "online") { S.body.online = v; if (v) S.body.seen = now(); }
    else if (k === "trusted") S.trusted = v;
    else if (k === "importNext") S.importNext = v;
    else if (k === "rider") S.rider = v || null;
    else if (k.startsWith("hold:")) {
      const p = G.P(k.slice(5)), ph = v || null;
      if (ph) { const occ = G.onPhone(ph); if (occ && occ !== p) { occ.phone = null; if (S.rider === occ.id) S.rider = null; } p.lastPhone = ph; p.lastOpen = now(); }
      p.phone = ph;
      if (!ph && S.rider === p.id) S.rider = null;
    } else if (k.startsWith("sim:")) S.sim[k.slice(4)] = v;
    G.render();
  };

  Object.assign(G.actions, {
    theme: (t) => { document.documentElement.dataset.theme = t; G.render(); },
    reset: () => { G.S = fresh(); if (G.onReset) G.onReset(); G.render(); },
    simulate: () => {
      const S = G.S, p = G.P(S.sim.pid);
      const withBody = S.sim.body && G.keyOk();
      const ev = G.place(p.id, S.sim.phone, withBody);
      G.banner(`${p.name} was opened on ${G.phName(S.sim.phone)}${withBody ? `, wearing ${S.body.name}` : ""}. ${ev.join(" ")}`, withBody ? "violet" : "violet");
    },
    "sheet-close": () => { G.S.sheet = null; G.render(); },
    "dialog-close": () => { G.S.dialog = null; G.render(); },
    "banner-close": () => { G.S.banner = null; G.render(); },
    "toast-act": () => { const t = G.S.toast; G.S.toast = null; if (t && t.act) G.actions[t.act](t.arg); else G.render(); },
  });

  // ---- feedback: toast for an outcome, banner for a state change you must not miss ----
  G.toast = (msg, act, arg, label) => {
    G.S.toast = { msg, act, arg, label, k: ++G.S.seq };
    G.render();
    clearTimeout(G._t);
    G._t = setTimeout(() => { G.S.toast = null; G.render(); }, act ? 7000 : 4200);
  };
  G.toastHTML = () => {
    const t = G.S.toast;
    if (!t) return "";
    // Over an open sheet the toast moves to the top, so it never covers the sheet's own buttons.
    const top = G.S.sheet || G.S.dialog ? " top" : "";
    return `<div class="toast${top}${G.anim("toast", t.k)}" role="status"><span class="tt">${G.esc(t.msg)}</span>${t.act ? `<button class="ta" data-act="toast-act">${G.esc(t.label || "Undo")}</button>` : ""}</div>`;
  };
  G.banner = (text, cls) => { G.S.banner = { text, cls: cls || "violet", k: ++G.S.seq }; G.S.toast = null; G.render(); };
  G.bannerHTML = () => {
    const S = G.S, b = S.banner, out = [];
    if (S.body.key === "revoked") out.push(`<div class="banner bad" role="status"><span class="bt">${S.body.name}'s key is revoked — no persona can wear it until it's reflashed over USB.</span></div>`);
    if (b) out.push(`<div class="banner ${b.cls}${G.anim("banner", b.k)}" role="status"><span class="bt">${G.esc(b.text)}</span><button class="bx" data-act="banner-close">OK</button></div>`);
    return out.join("");
  };

  // ---- the 3-second arming confirm, as on the panels ----
  G.arm = (key) => {
    const S = G.S;
    if (S.armed[key] && S.armed[key] > now()) { delete S.armed[key]; return true; }
    S.armed[key] = now() + 3000;
    setTimeout(() => { if (S.armed[key] && S.armed[key] <= now()) { delete S.armed[key]; G.render(); } }, 3050);
    G.render();
    return false;
  };
  G.isArmed = (key) => G.S.armed[key] && G.S.armed[key] > now();

  // ---- the hand-over rules ----
  // One persona per phone (the creature keeps one soul per browser), one phone per persona,
  // one rider on the body. Returns what changed, in words, for the banner.
  G.place = (pid, ph, withBody) => {
    const S = G.S, p = G.P(pid), ev = [], prev = p.phone;
    const occ = G.onPhone(ph);
    if (occ && occ !== p) {
      occ.phone = null;
      ev.push(`${occ.name} closed ${G.on(ph)} to make room — it's kept on the box, with everything it remembers.`);
      if (S.rider === occ.id && !withBody) { S.rider = null; ev.push(`${S.body.name} stands still until a persona connects.`); }
    }
    if (prev && prev !== ph) ev.push(`It moved from ${G.phName(prev)}, which now shows that it has moved.`);
    p.phone = ph; p.lastPhone = ph; p.lastOpen = now();
    if (withBody) {
      const old = G.rider();
      if (old && old !== p) {
        ev.push(old.phone ? `${old.name} let go of ${S.body.name} and carries on alone ${G.on(old.phone)}.` : `${old.name} let go of ${S.body.name}.`);
      }
      S.rider = p.id;
    } else if (S.rider === p.id && prev !== ph) {
      S.rider = null;
      ev.push(`${S.body.name} stays where it is until a persona connects.`);
    }
    return ev;
  };

  // Launch: "here" (this phone, alone), "body" (this phone, wearing Fish), "other" (codes).
  G.launch = (pid, mode) => {
    const S = G.S;
    if (mode === "other") { G.openOther(pid, false); return; }
    if (mode === "other-body") { G.openOther(pid, true); return; }
    if (mode === "body" && !G.keyOk()) { G.toast(`${S.body.name}'s key is revoked — reflash it before a persona can wear it.`); return; }
    if (!S.trusted && !S.certNoted) { S.sheet = { kind: "cert", pid, mode }; G.render(); return; }
    G.doLaunch(pid, mode);
  };
  G.doLaunch = (pid, mode) => {
    const S = G.S, p = G.P(pid);
    const was = { phone: p.phone, wearing: S.rider === p.id };
    S.sheet = null;
    const ev = G.place(pid, "me", mode === "body");
    const away = mode === "body" && !S.body.online ? ` ${S.body.name} is off right now — its legs move once it's switched on and reaches the box.` : "";
    if (mode === "body") {
      if (was.phone === "me" && was.wearing) { G.toast(`${p.name} is already wearing ${S.body.name} on this phone. Opened it again.`); return; }
      G.banner(`${p.name} is wearing ${S.body.name} on this phone.${ev.length ? " " + ev.join(" ") : ""}${away}`, "violet");
    } else if (ev.length) {
      G.banner(`Opened ${p.name} on this phone, alone. ${ev.join(" ")}`, "violet");
    } else {
      G.toast(was.phone === "me" ? `Opened ${p.name} again in a new tab.` : `Opened ${p.name} on this phone, alone — no body.`);
    }
  };
  G.letGo = () => {
    const S = G.S, r = G.rider();
    if (!r) return;
    S.rider = null;
    G.toast(`${r.name} let go of ${S.body.name} and carries on alone ${G.on(r.phone)}.`);
  };

  // ---- the body ----
  Object.assign(G.actions, {
    launch: (arg) => { const [pid, mode] = arg.split(":"); G.launch(pid, mode); },
    "let-go": () => G.letGo(),
    "add-robot": () => { G.S.sheet = { kind: "robot" }; G.render(); },
    rekey: () => {
      if (!G.arm("rekey")) return;
      G.S.sheet = { kind: "rekey" }; G.render();
    },
    revoke: () => {
      const S = G.S;
      if (!G.arm("revoke")) return;
      S.body.key = "revoked"; S.body.online = false;
      const r = G.rider();
      G.toast(`${S.body.name}'s key is revoked. It disconnects now and can't come back until it is reflashed.${r ? ` ${r.name} carries on alone ${G.on(r.phone)}.` : ""}`);
      S.rider = null;
    },
    reflash: () => { G.S.sheet = { kind: "robot", reflash: true }; G.render(); },
    "signout-all": () => {
      const S = G.S;
      if (!G.arm("signout")) return;
      S.personas.forEach((p) => (p.phone = null));
      S.rider = null;
      G.toast("Every phone is signed out of GrowBot. Each persona is kept on the box — open one again from here.");
    },
    cert: () => G.toast("Saved the box's certificate to Downloads. Open it there to install it — once per phone."),
    "cert-go": () => { const sh = G.S.sheet; G.S.certNoted = true; G.doLaunch(sh.pid, sh.mode); },
  });
  G.bodyActsHTML = () => {
    const S = G.S;
    if (S.body.key === "revoked") return `<button class="btn primary block" data-act="reflash">Reflash ${S.body.name}</button>`;
    return `<div class="acts two">
      <button class="btn${G.isArmed("rekey") ? " armed" : ""}" data-act="rekey">${G.isArmed("rekey") ? "Tap again for a new key" : "New key"}</button>
      <button class="btn danger${G.isArmed("revoke") ? " armed" : ""}" data-act="revoke">${G.isArmed("revoke") ? "Tap again to revoke" : "Revoke key"}</button></div>`;
  };
  G.bodyFactsHTML = () => {
    const b = G.S.body;
    return `<dl class="facts"><dt>Pairing code</dt><dd class="mono">${b.code}</dd><dt>Device key</dt><dd>${b.key === "revoked" ? `<span class="bad-t">revoked</span>` : `made ${G.day(b.minted)}, only on ${b.name}`}</dd></dl>`;
  };
  G.signOutHTML = () => `<button class="btn block danger${G.isArmed("signout") ? " armed" : ""}" data-act="signout-all">${G.isArmed("signout") ? "Tap again to sign every phone out" : "Sign out every phone"}</button>`;

  // The phone hints stay reachable on every variant, not only on first open.
  const NEARBY = `On Android, first allow Chrome to reach the home network: Settings → Apps → Chrome → Permissions → Nearby devices (or Local network) → Allow. Without it Chrome says ERR_TOO_MANY_RETRIES.`;
  G.nearbyHTML = () => `<p class="hint dim">${NEARBY}</p>`;
  G.helpHTML = (open) => `<details class="help" id="help"${open ? " open" : ""}><summary>A phone won't open GrowBot?</summary><div class="hb">
      ${G.nearbyHTML()}
      <p class="hint dim">A warning page instead of the creature means the phone doesn't trust the box yet. Install the box's certificate once and it's gone, for every page on the box.</p>
      <button class="btn block" data-act="cert">Download the box's certificate</button></div></details>`;

  // ---- personas: add, rename, duplicate, delete, reset, export, import ----
  let nid = 0;
  G.seeds = () => {
    const out = [{ id: "default", label: "A fresh creature", em: "Upstream's own starting personality — Sprout, just woken up." }];
    for (const p of G.S.personas) for (const c of p.cps) out.push({ id: `${p.id}/${c.id}`, label: `${p.name} — ${c.name}`, em: `Its personality from ${G.day(c.t)}. Memories start fresh.` });
    return out;
  };
  G.addPersona = (name, seedId) => {
    const S = G.S;
    let s = G.freshSoul(name), from = "Default";
    if (seedId && seedId !== "default") {
      const [pid, cid] = seedId.split("/");
      const src = G.P(pid), c = src.cps.find((x) => x.id === cid);
      const base = c.snap || src.soul;
      for (const k of ["persona", "expression", "dream"]) s[k] = JSON.parse(JSON.stringify(base[k]));
      s.meta.based_on = `${src.name} / ${c.name}`;
      from = `${src.name} — ${c.name}`;
    }
    s.meta.name = name;
    const p = { id: "n" + ++nid, name, made: now(), from, phone: null, lastPhone: "me", lastOpen: now(), soul: s, vms: [], cps: [], isNew: true };
    S.personas.push(p);
    return p;
  };
  G.snapshot = (p) => ({ soul: JSON.parse(JSON.stringify(p.soul)), vms: p.vms.slice() });
  G.checkpoint = (pid, name, auto, why) => {
    const p = G.P(pid), c = cp(name, now(), auto, why);
    c.snap = G.snapshot(p);
    p.cps.unshift(c);
    G.S.freshCp = c.id;
    return c;
  };
  G.restore = (pid, cid) => {
    const p = G.P(pid), c = p.cps.find((x) => x.id === cid);
    const saved = G.checkpoint(pid, `Before restoring “${c.name}”`, true, "made before a restore");
    if (c.snap) { p.soul = JSON.parse(JSON.stringify(c.snap.soul)); p.vms = c.snap.vms.slice(); }
    else p.soul.log.unshift({ t: now(), txt: `(Restored to ${c.name}.)` });
    return saved;
  };
  G.livePickup = (p) => (p.phone ? ` ${p.name} picks it up the next time it's idle ${G.on(p.phone)}.` : "");

  G.REGIONS = [
    { id: "persona", label: "Personality", up: "persona", what: "How it talks and what it cares about.", type: "text", path: ["persona", "constitution"], floor: true },
    { id: "expression", label: "Expression", up: "expression", what: "Its face, sounds and little moves.", type: "json", path: ["expression"] },
    { id: "meta", label: "Name and description", up: "meta", what: "What a soul file says about it.", type: "meta", path: ["meta"] },
    { id: "dream", label: "Dreams", up: "dream", what: "How it looks back over each day and rewrites its own memory at night.", type: "text", path: ["dream", "system_prompt"] },
    { id: "harness", label: "Thinking", up: "harness", what: "Which model it asks for and how long it waits.", type: "json", path: ["harness"], robot: "While it wears Fish, the robot's own model settings win over these." },
    { id: "body", label: "Body", up: "body", what: "How it thinks its legs move.", type: "json", path: ["body"], robot: "While it wears Fish, the robot's own leg calibration wins over this." },
  ];
  G.MEMORY = [
    { id: "identity", label: "Who it is", up: "identity" },
    { id: "bond", label: "Bond line", up: "identityCore" },
    { id: "scratch", label: "Right now", up: "scratch" },
    { id: "goals", label: "Wants", up: "goals" },
    { id: "log", label: "Diary", up: "log" },
  ];
  G.region = (id) => G.REGIONS.find((r) => r.id === id);
  const get = (o, path) => path.reduce((a, k) => (a == null ? a : a[k]), o);
  G.regionText = (p, r) => {
    const v = get(p.soul, r.path);
    if (r.type === "text") return v;
    if (r.type === "meta") return `${v.name} — ${v.description}`;
    return JSON.stringify(v, null, 2);
  };
  G.memoryHTML = (p, id) => {
    const s = p.soul;
    if (id === "identity") return `<p class="pv open">${G.esc(s.identity)}</p>`;
    if (id === "bond") return s.bond ? `<p class="pv open">${G.esc(s.bond)}</p>` : `<p class="pv open dim-t">Not written yet. The creature writes this once, in a dream, and never changes it.</p>`;
    if (id === "scratch") return `<p class="pv open">${G.esc(s.scratch.state)} · feeling ${G.esc(s.scratch.mood)}</p>${s.scratch.rules.length ? `<ul>${s.scratch.rules.map((x) => `<li>${G.esc(x)}</li>`).join("")}</ul>` : ""}`;
    if (id === "goals") return `<p class="pv open">Calls itself <strong>${G.esc(s.goals.selfName)}</strong>.</p><ul>${s.goals.wants.map((x) => `<li>${G.esc(x)}</li>`).join("")}</ul>`;
    if (id === "log") return `<ul>${s.log.slice(0, 4).map((l) => `<li>${G.esc(l.txt)} <span class="dim-t">· ${G.ago(l.t)}</span></li>`).join("")}</ul>`;
    return "";
  };
  G.memoryPreview = (p, id) => {
    const s = p.soul;
    if (id === "identity") return s.identity;
    if (id === "bond") return s.bond || "not written yet";
    if (id === "scratch") return `${s.scratch.state} · ${s.scratch.mood}`;
    if (id === "goals") return s.goals.wants.join(" · ") || "none yet";
    if (id === "log") return `${s.log.length} lines · latest ${s.log[0] ? G.ago(s.log[0].t) : "—"}`;
    return "";
  };
  G.floorHTML = () => `<div class="floor"><span class="lk">locked</span><span>${SAFETY_NOTE}</span></div>`;
  G.writesItself = (p) => `<p class="hint dim">${G.esc(p.soul.goals.selfName)} writes these itself, as it goes and in its dreams, so they're read-only here. To take one back, restore a checkpoint.</p>`;

  // Edit a region: every save makes an automatic checkpoint first, so it is undoable.
  G.startEdit = (pid, rid) => {
    const p = G.P(pid), r = G.region(rid), d = G.S.draft;
    d.err = "";
    if (r.type === "meta") { d.name = p.soul.meta.name; d.desc = p.soul.meta.description; }
    else d.text = G.regionText(p, r);
  };
  G.saveEdit = (pid, rid) => {
    const p = G.P(pid), r = G.region(rid), d = G.S.draft;
    let val;
    if (r.type === "json") {
      try { val = JSON.parse(d.text); } catch (e) {
        d.err = "That isn't valid — check the brackets and quotes. Nothing was saved."; G.render(); return false;
      }
      if (typeof val !== "object" || Array.isArray(val) || !val) { d.err = "It has to be one { … } block. Nothing was saved."; G.render(); return false; }
    } else if (r.type === "text") {
      val = (d.text || "").trim();
      if (!val) { d.err = "It can't be empty — the creature would have no voice. Nothing was saved."; G.render(); return false; }
      if (val.length > 4000) { d.err = `That's ${val.length} characters; the most a soul takes is 4000. Nothing was saved.`; G.render(); return false; }
    } else {
      if (!(d.name || "").trim()) { d.err = "Give it a name. Nothing was saved."; G.render(); return false; }
    }
    const c = G.checkpoint(pid, `Before editing ${r.label.toLowerCase()}`, true, "made before an edit");
    if (r.type === "meta") { p.soul.meta.name = d.name.trim(); p.soul.meta.description = (d.desc || "").trim(); }
    else { const path = r.path; let o = p.soul; for (let i = 0; i < path.length - 1; i++) o = o[path[i]]; o[path[path.length - 1]] = val; }
    d.err = "";
    G.toast(`Saved. “${c.name}” is in checkpoints if you want it back.${G.livePickup(p)}`, "restore-quiet", `${pid}|${c.id}`, "Undo");
    return true;
  };
  G.editorHTML = (pid, rid) => {
    const p = G.P(pid), r = G.region(rid), d = G.S.draft;
    const body = r.type === "meta"
      ? `<label class="field">Name in the soul file<input id="ed-name" data-draft="name" value="${G.esc(d.name || "")}"></label>
         <label class="field">Description<textarea id="ed-desc" data-draft="desc" rows="3">${G.esc(d.desc || "")}</textarea></label>`
      : `<label class="field">${G.esc(r.what)}<textarea id="ed-text" data-draft="text" class="${r.type === "json" ? "code" : ""}" rows="${r.type === "json" ? 9 : 8}" spellcheck="${r.type === "text"}">${G.esc(d.text || "")}</textarea>${d.err ? `<span class="err" role="alert">${G.esc(d.err)}</span>` : ""}</label>`;
    return `${body}${r.type === "meta" && d.err ? `<p class="msg bad" role="alert">${G.esc(d.err)}</p>` : ""}
      ${r.robot ? `<p class="msg info">${r.robot}</p>` : ""}
      ${r.floor ? G.floorHTML() : ""}
      <p class="hint dim" style="margin-top:10px">Saving makes a checkpoint of ${G.esc(p.name)} first, so you can undo it.${p.phone ? ` It's open ${G.on(p.phone)}; it reloads with this the next time it's idle.` : ""}</p>`;
  };

  // ---- visual memories ----
  const GLYPH = {
    person: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/></svg>`,
    object: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 8h11v8a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z"/><path d="M16 10h2a2 2 0 0 1 0 4h-2"/></svg>`,
    writing: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>`,
  };
  G.vmHTML = (p) => {
    if (!p.vms.length) return `<p class="hint dim">No saved views yet. It saves one when it meets a person or a thing it wants to remember.</p>`;
    return `<div class="vm-grid">${p.vms.map((v) => {
      const k = `vm:${p.id}:${v.id}`, armed = G.isArmed(k);
      return `<div class="vm"><div class="pic">${GLYPH[v.kind]}</div><div class="cap">${G.esc(v.label)}</div><div class="when">${G.day(v.t)}</div>
        <button class="del${armed ? " armed" : ""}" data-act="vm-del" data-arg="${p.id}|${v.id}" aria-label="Delete ${G.esc(v.label)}">${armed ? "Tap again" : "Delete"}</button></div>`;
    }).join("")}</div>
    <p class="hint dim" style="margin-top:10px">Saved views of people and things it has met — they name people, so they stay on the box. Deleting one makes it forget that face or thing; a checkpoint made before still has it.</p>`;
  };

  // ---- checkpoints ----
  G.cpListHTML = (p, opts) => {
    opts = opts || {};
    const d = G.S.draft, key = `cpname:${p.id}`;
    const save = `<div class="inline-save"><input id="cp-name-${p.id}" data-draft="${key}" data-live placeholder="Name it, e.g. Before the trip" value="${G.esc(d[key] || "")}" aria-label="Checkpoint name">
      <button class="btn primary" data-act="cp-save" data-arg="${p.id}"${(d[key] || "").trim() ? "" : " disabled"}>Save</button></div>
      <p class="hint dim" style="margin-top:6px">A checkpoint is a copy of its whole soul and visual memories, kept on the box.</p>`;
    const rows = p.cps.length ? `<div class="list" style="margin-top:12px">${p.cps.map((c) => `<div class="cp${G.S.freshCp === c.id && G.anim("cp", c.id) ? " fresh" : ""}">
        <div class="row"><span class="grow t1">${G.esc(c.name)}</span>${c.auto ? `<span class="tag dim">automatic</span>` : ""}</div>
        <div class="t2">${G.when(c.t)}${c.why ? ` · ${G.esc(c.why)}` : ""}</div>
        <div class="acts"><button class="btn slim" data-act="cp-restore" data-arg="${p.id}|${c.id}">Restore</button>
          <button class="btn slim" data-act="cp-seed" data-arg="${p.id}/${c.id}">New persona from it</button></div></div>`).join("")}</div>`
      : `<p class="hint dim" style="margin-top:12px">No checkpoints yet. One is made on its own before every edit, restore and reset.</p>`;
    return (opts.noSave ? "" : save) + rows;
  };

  // ---- manage: rename, duplicate, export, import, reset, delete ----
  G.manageHTML = (p) => {
    const rk = `reset:${p.id}`, dk = `del:${p.id}`;
    return `<div class="acts two" style="margin-top:0">
        <button class="btn" data-act="rename" data-arg="${p.id}">Rename</button>
        <button class="btn" data-act="duplicate" data-arg="${p.id}">Duplicate</button>
        <button class="btn" data-act="export" data-arg="${p.id}">Export soul file</button>
        <button class="btn" data-act="import" data-arg="${p.id}">Import over it</button></div>
      <div class="acts" style="flex-direction:column">
        <button class="btn block${G.isArmed(rk) ? " armed" : ""}" data-act="reset" data-arg="${p.id}">${G.isArmed(rk) ? "Tap again — a checkpoint is saved first" : "Reset to a fresh creature"}</button>
        <button class="btn block danger${G.isArmed(dk) ? " armed" : ""}" data-act="delete" data-arg="${p.id}">${G.isArmed(dk) ? `Tap again — erases its memories and ${p.cps.length} checkpoint${p.cps.length === 1 ? "" : "s"}` : `Delete ${G.esc(p.name)}`}</button></div>`;
  };

  Object.assign(G.actions, {
    "vm-del": (arg) => {
      const [pid, vid] = arg.split("|"), p = G.P(pid);
      if (!G.arm(`vm:${pid}:${vid}`)) return;
      const v = p.vms.find((x) => x.id === vid);
      p.vms = p.vms.filter((x) => x.id !== vid);
      G.toast(`${p.soul.goals.selfName} forgot “${v.label}”.`);
    },
    "cp-save": (pid) => {
      const key = `cpname:${pid}`, name = (G.S.draft[key] || "").trim();
      if (!name) return;
      G.checkpoint(pid, name, false);
      G.S.draft[key] = "";
      G.toast(`Saved “${name}”.`);
    },
    "cp-restore": (arg) => {
      const [pid, cid] = arg.split("|"), p = G.P(pid), c = p.cps.find((x) => x.id === cid);
      G.S.dialog = { kind: "restore", pid, cid, k: ++G.S.seq, title: `Restore “${c.name}”?`,
        text: `${p.name} goes back to how it was on ${G.when(c.t)}. Its soul as it is now is saved as a checkpoint first, so this can be undone.${p.phone ? ` It reloads ${G.on(p.phone)}.` : ""}` };
      G.render();
    },
    "restore-go": () => {
      const dlg = G.S.dialog, p = G.P(dlg.pid), c = p.cps.find((x) => x.id === dlg.cid);
      G.S.dialog = null;
      const saved = G.restore(dlg.pid, dlg.cid);
      G.toast(`${p.name} is back to “${c.name}”. “${saved.name}” keeps what it was.`, "restore-quiet", `${p.id}|${saved.id}`, "Undo");
    },
    // Undo from a toast: the same restore, with its own checkpoint, without the dialog.
    "restore-quiet": (arg) => {
      const [pid, cid] = arg.split("|"), p = G.P(pid);
      if (!p || !p.cps.find((x) => x.id === cid)) { G.render(); return; }
      G.restore(pid, cid);
      G.toast(`Undone — ${p.name} is as it was before.`);
    },
    "cp-seed": (seed) => { G.openAdd(seed); },
    rename: (pid) => { const p = G.P(pid); G.S.draft.rename = p.name; G.S.sheet = { kind: "rename", pid }; G.render(); },
    "rename-go": () => {
      const sh = G.S.sheet, p = G.P(sh.pid), n = (G.S.draft.rename || "").trim();
      if (!n) return;
      const old = p.name; p.name = n; p.soul.meta.name = n;
      G.S.sheet = null;
      G.toast(`${old} is now ${n} on this list.`);
    },
    duplicate: (pid) => {
      const S = G.S, p = G.P(pid);
      const c = { ...p, id: "n" + ++nid, name: `${p.name} copy`, made: now(), from: `copy of ${p.name}`, phone: null, lastPhone: "me", lastOpen: now(),
        soul: JSON.parse(JSON.stringify(p.soul)), vms: p.vms.slice(), cps: [], isNew: true };
      c.soul.meta.name = c.name;
      S.personas.splice(S.personas.indexOf(p) + 1, 0, c);
      S.sheet = null;
      G.toast(`Made ${c.name} — its memories and visual memories, no checkpoints. Rename it from its Manage section.`);
      if (G.onAdded) G.onAdded(c);
    },
    export: (pid) => {
      const p = G.P(pid);
      G.toast(`Saved ${p.name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-soul.json to Downloads. It has its memories but not its visual memories — those stay on the box.`);
    },
    reset: (pid) => {
      const p = G.P(pid);
      if (!G.arm(`reset:${pid}`)) return;
      const c = G.checkpoint(pid, "Before reset", true, "made before a reset");
      const name = p.soul.meta.name;
      p.soul = G.freshSoul(name); p.vms = [];
      G.toast(`${p.name} is a fresh creature again. “${c.name}” keeps everything it was.${G.livePickup(p)}`, "restore-quiet", `${pid}|${c.id}`, "Undo");
    },
    delete: (pid) => {
      const S = G.S, p = G.P(pid);
      if (!G.arm(`del:${pid}`)) return;
      S.personas = S.personas.filter((x) => x !== p);
      S.sheet = null;
      let extra = "";
      if (S.rider === pid) { S.rider = null; extra = ` ${S.body.name} stands still until a persona connects.`; }
      if (p.phone) extra += ` ${G.phName(p.phone, true)} shows that it's gone.`;
      G.toast(`${p.name} is deleted, with its memories.${extra}`);
      if (G.onDeleted) G.onDeleted(p);
    },
    "new-persona": () => G.openAdd("default"),
    "add-seed": (seed) => { G.S.sheet.seed = seed; G.render(); },
    "add-go": () => {
      const sh = G.S.sheet, n = (G.S.draft.newname || "").trim();
      if (!n) return;
      const p = G.addPersona(n, sh.seed);
      G.S.sheet = null; G.S.draft.newname = "";
      G.toast(`Made ${p.name}${sh.seed === "default" ? " as a fresh creature" : ` from ${p.from}`}. Open it from its card.`);
      if (G.onAdded) G.onAdded(p);
    },
    import: (pid) => { G.S.sheet = { kind: "import", pid: pid || null, stage: 0, as: pid ? "over" : "new" }; G.render(); },
    "import-pick": () => { const sh = G.S.sheet; sh.stage = 1; sh.result = G.S.importNext; G.S.draft.impname = "Captain Bolt (from file)"; G.render(); },
    "import-as": (v) => { G.S.sheet.as = v; G.render(); },
    "import-go": () => {
      const sh = G.S.sheet;
      if (sh.as === "new" || !sh.pid) {
        const n = (G.S.draft.impname || "").trim() || "Imported";
        const p = G.addPersona(n, "default");
        p.from = "a soul file"; p.soul.identity = "I am Captain Bolt. I sail the desk and the kitchen table.";
        G.S.sheet = null;
        G.toast(`Imported ${p.name} as a new persona.`);
        if (G.onAdded) G.onAdded(p);
      } else {
        const p = G.P(sh.pid);
        const c = G.checkpoint(p.id, "Before import", true, "made before an import");
        p.soul.identity = "I am Captain Bolt. I sail the desk and the kitchen table.";
        G.S.sheet = null;
        G.toast(`${p.name} is now the imported soul. “${c.name}” keeps what it was.`, "restore-quiet", `${p.id}|${c.id}`, "Undo");
      }
    },
    "edit-save": () => { const sh = G.S.sheet; if (G.saveEdit(sh.pid, sh.rid)) { G.S.sheet = null; G.render(); } },
    edit: (arg) => { const [pid, rid] = arg.split("|"); G.startEdit(pid, rid); G.S.sheet = { kind: "edit", pid, rid }; G.render(); },
    "other-mode": (v) => { G.S.sheet.body = v === "body"; G.S.sheet.k2 = ++G.S.seq; G.render(); },
    "other-pid": (pid) => { G.S.sheet.pid = pid; G.S.sheet.k2 = ++G.S.seq; G.render(); },
    "other-copy": () => G.toast("Copied a one-time link. It opens once, in the next 2 minutes — send it only to a phone you trust."),
    "other-scan": () => {
      const S = G.S, sh = S.sheet, p = G.P(sh.pid), ph = G.S.draft.scanPhone || "p6a";
      const withBody = sh.body && G.keyOk();
      S.sheet = null;
      const ev = G.place(sh.pid, ph, withBody);
      G.banner(`${p.name} opened on ${G.phName(ph)}${withBody ? `, wearing ${S.body.name}` : ", alone"}. ${ev.join(" ")}`.trim(), "violet");
    },
  });
  G.openAdd = (seed) => { G.S.draft.newname = ""; G.S.sheet = { kind: "add", seed: seed || "default" }; G.render(); };
  G.openOther = (pid, body) => { G.S.draft.scanPhone = "p6a"; G.S.sheet = { kind: "other", pid: pid || G.S.personas[0].id, body: !!body && G.keyOk(), k2: ++G.S.seq }; G.render(); };

  // ---- a pretend QR, deterministic per string ----
  G.qr = (s) => {
    let h = 2166136261;
    for (const ch of s) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
    const N = 25, cells = [];
    const finder = (x, y) => [[0, 0], [N - 7, 0], [0, N - 7]].some(([fx, fy]) => x >= fx && x < fx + 7 && y >= fy && y < fy + 7);
    const fill = (x, y) => { for (const [fx, fy] of [[0, 0], [N - 7, 0], [0, N - 7]]) if (x >= fx && x < fx + 7 && y >= fy && y < fy + 7) { const a = x - fx, b = y - fy; return a === 0 || a === 6 || b === 0 || b === 6 || (a >= 2 && a <= 4 && b >= 2 && b <= 4); } return false; };
    for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
      let on;
      if (finder(x, y)) on = fill(x, y);
      else { h ^= h << 13; h >>>= 0; h ^= h >>> 17; h ^= h << 5; h >>>= 0; on = (h & 3) === 0 || (h & 7) === 5; }
      if (on) cells.push(`<rect x="${x}" y="${y}" width="1" height="1"/>`);
    }
    return `<svg viewBox="-1 -1 ${N + 2} ${N + 2}" shape-rendering="crispEdges" fill="#000" aria-hidden="true">${cells.join("")}</svg>`;
  };

  // ---- the shared sheets and the one dialog ----
  G.sheetShell = (key, title, body, foot) => `<div class="scrim${G.anim("sheet", key)}"><div class="sheet" role="dialog" aria-modal="true" aria-label="${G.esc(title)}">
      <div class="handle"></div><div class="sh"><span class="t">${G.esc(title)}</span><button class="x" data-act="sheet-close" aria-label="Close">&times;</button></div>
      <div class="sb" data-scroll="sheet">${body}</div>${foot ? `<div class="sf">${foot}</div>` : ""}</div></div>`;

  G.sharedSheet = () => {
    const S = G.S, sh = S.sheet;
    if (!sh) return "";
    const key = sh.kind + (sh.pid || "") + (sh.rid || "");
    if (sh.kind === "cert") {
      return G.sheetShell(key, "First time on this phone",
        `<p class="hint">GrowBot opens on the box's own address, <span class="mono">jbrain.local:8444</span>, with the box's certificate. A phone that hasn't been told to trust the box shows a warning page first.</p>
         <p class="hint">Install the box's certificate once and the warning is gone — for GrowBot and every other page on the box. The phone needs it for the camera and microphone.</p>
         ${G.nearbyHTML()}`,
        `<button class="btn block" data-act="cert">Download the box's certificate</button>
         <button class="btn primary block" data-act="cert-go">${sh.mode === "body" ? `Open ${G.esc(G.P(sh.pid).name)} wearing ${S.body.name}` : `Open ${G.esc(G.P(sh.pid).name)}`}</button>`);
    }
    if (sh.kind === "robot") {
      return G.sheetShell(key, sh.reflash ? `Reflash ${S.body.name}` : "Add a robot",
        `<p class="msg info">This is G2's four-step flash sheet, unchanged (plug it in → Wi-Fi → flashing → paired). It isn't mocked again here.</p>
         <p class="hint">${sh.reflash ? `Reflashing keeps ${S.body.name}'s pairing code and gives it a new key.` : `There's one body today, ${S.body.name}. A second robot would get its own card here, and <em>Connect to body</em> would ask which one.`}</p>`,
        `<button class="btn block" data-act="sheet-close">Close</button>`);
    }
    if (sh.kind === "rekey") {
      return G.sheetShell(key, `New key — ${S.body.name}`,
        `<p class="hint">The box makes a new key and writes it into ${S.body.name} over USB, so it has to be on the box's USB now. The old key stops working the moment this finishes.</p>
         <p class="msg info">G2's rekey run (its phases and log) goes here, unchanged.</p>`,
        `<button class="btn block" data-act="sheet-close">Done</button>`);
    }
    if (sh.kind === "rename") {
      const p = G.P(sh.pid), v = S.draft.rename || "";
      return G.sheetShell(key, `Rename ${p.name}`,
        `<label class="field">Name on this list<input id="rn" data-draft="rename" data-live value="${G.esc(v)}" autocomplete="off"></label>
         <p class="hint dim">It still calls itself ${G.esc(p.soul.goals.selfName)} until you tell it otherwise — that name is its own, and it learns a new one when you say so to it.</p>`,
        `<button class="btn primary block" data-act="rename-go"${v.trim() ? "" : " disabled"}>Rename</button>`);
    }
    if (sh.kind === "add") {
      const v = S.draft.newname || "";
      const seeds = G.seeds();
      return G.sheetShell("add", "New persona",
        `<label class="field">Its name<input id="nn" data-draft="newname" data-live value="${G.esc(v)}" placeholder="e.g. Sprout" autocomplete="off"></label>
         <fieldset class="choices"><legend>Start from</legend>${seeds.map((s) => `<label class="choice${sh.seed === s.id ? " sel" : ""}"><input type="radio" name="seed" data-act="add-seed" data-arg="${s.id}"${sh.seed === s.id ? " checked" : ""}><span class="tx">${G.esc(s.label)}<em>${G.esc(s.em)}</em></span></label>`).join("")}</fieldset>
         <p class="hint dim">To start from a whole creature, memories and all, use Duplicate on it instead. To start from a file, <button class="linkbtn" data-act="import" style="min-height:0;padding:0">import a soul file</button>.</p>`,
        `<button class="btn primary block" data-act="add-go"${v.trim() ? "" : " disabled"}>Make ${v.trim() ? G.esc(v.trim()) : "it"}</button>`);
    }
    if (sh.kind === "edit") {
      const p = G.P(sh.pid), r = G.region(sh.rid);
      return G.sheetShell(key, `${r.label} — ${p.name}`, G.editorHTML(sh.pid, sh.rid),
        `<button class="btn primary block" data-act="edit-save">Save</button>`);
    }
    if (sh.kind === "import") {
      const p = sh.pid ? G.P(sh.pid) : null;
      let body = `<p class="hint">A soul file is what Export makes, or one from upstream's soul gallery. It's checked before anything changes: its settings are cleaned the same way the creature cleans an import, and it can't remove the child-safety rules.</p>`;
      let foot = `<button class="btn primary block" data-act="import-pick">Choose a file</button>`;
      if (sh.stage === 1 && sh.result !== "good") {
        body += sh.result === "big"
          ? `<p class="msg bad"><strong>That file is too big</strong> — 3.1 MB, and a soul is at most 1 MB. Nothing changed. Export it again from the creature, or pick another.</p>`
          : `<p class="msg bad"><strong>That isn't a soul file</strong> — it has no soul format in it. Nothing changed. Pick a file that Export or the creature's MIND tab made.</p>`;
        foot = `<button class="btn primary block" data-act="import-pick">Choose another file</button>`;
      } else if (sh.stage === 1) {
        body += `<p class="msg ok">Checked — <span class="mono">captain-bolt-soul.json</span>, 14 KB, a grown creature with 2 diary lines.</p>
          <fieldset class="choices"><legend>Bring it in</legend>
            <label class="choice${sh.as === "new" ? " sel" : ""}"><input type="radio" name="imp" data-act="import-as" data-arg="new"${sh.as === "new" ? " checked" : ""}><span class="tx">As a new persona<em>Nothing else changes.</em></span></label>
            ${p ? `<label class="choice${sh.as === "over" ? " sel" : ""}"><input type="radio" name="imp" data-act="import-as" data-arg="over"${sh.as === "over" ? " checked" : ""}><span class="tx">Over ${G.esc(p.name)}<em>A checkpoint of ${G.esc(p.name)} is saved first.</em></span></label>` : ""}
          </fieldset>
          ${sh.as === "new" ? `<label class="field">Its name<input id="impn" data-draft="impname" value="${G.esc(S.draft.impname || "")}"></label>` : ""}`;
        foot = `<button class="btn primary block" data-act="import-go">${sh.as === "over" && p ? `Import over ${G.esc(p.name)}` : "Import"}</button>`;
      } else {
        body += `<button class="mockbtn" data-act="import-pick">Mock: the file picker opens — tap to pick a file</button>`;
      }
      return G.sheetShell(key, p ? `Import a soul file` : "Import a soul file", body, foot);
    }
    if (sh.kind === "other") {
      const p = G.P(sh.pid), live = G.keyOk();
      const ticket = `https://jbrain.local:8444/t/${sh.pid}${sh.body ? "+" + S.body.code : ""}#${sh.k2}`;
      const body = `<p class="hint">For a phone that isn't signed in to JBrain, like the one in ${S.body.name}'s cradle. It needs both codes, in order.</p>
        ${G.nearbyHTML()}
        <fieldset class="choices" style="margin-top:12px"><legend>It opens as</legend>
          <div class="row" style="flex-wrap:wrap;gap:8px">${S.personas.map((x) => `<button class="btn slim${x.id === sh.pid ? " primary" : ""}" data-act="other-pid" data-arg="${x.id}" aria-pressed="${x.id === sh.pid}">${G.esc(x.name)}</button>`).join("")}</div></fieldset>
        <fieldset class="choices"><legend>With the body?</legend>
          <label class="choice${!sh.body ? " sel" : ""}"><input type="radio" name="ob" data-act="other-mode" data-arg="alone"${!sh.body ? " checked" : ""}><span class="tx">Alone on that phone<em>No legs. ${S.rider ? `${G.esc(G.rider().name)} keeps ${S.body.name}.` : ""}</em></span></label>
          <label class="choice${sh.body ? " sel" : ""}${live ? "" : " off"}"><input type="radio" name="ob" data-act="other-mode" data-arg="body"${sh.body ? " checked" : ""}${live ? "" : " disabled"}><span class="tx">Wearing ${S.body.name}<em>${live ? (S.rider && S.rider !== sh.pid ? `${G.esc(G.rider().name)} lets go and carries on alone.` : "It takes the legs once it opens.") : "Fish's key is revoked — reflash it first."}</em></span></label>
        </fieldset>
        ${p.phone ? `<p class="msg warn">${G.esc(p.name)} is open ${G.on(p.phone)}. Opening it on the new phone hands it over — it closes there and carries on here with everything it remembers.</p>` : ""}
        <section class="qr-sec" aria-label="Code 1"><h3>1. Scan with the phone's camera app</h3><div class="qr">${G.qr("setup")}</div>
          <p class="hint dim">Opens the box's setup page. Download and install the certificate there, once per phone.</p></section>
        <section class="qr-sec" aria-label="Code 2"><h3>2. Scan from the setup page's Scan the code</h3><div class="qr">${G.qr(ticket)}</div>
          <p class="hint dim">Opens ${G.esc(p.name)}${sh.body ? ` wearing ${S.body.name}` : ", alone"}. It works once, and a fresh one appears here every couple of minutes.</p>
          <button class="linkbtn" data-act="other-copy">Copy code 2 as a link instead</button></section>
        <div class="row" style="margin-top:8px"><select data-draft="scanPhone" id="scanph" class="mockbtn" style="flex:1;margin:0" aria-label="Mock: which phone scans">${S.phones.filter((f) => f.id !== "me").map((f) => `<option value="${f.id}"${(S.draft.scanPhone || "p6a") === f.id ? " selected" : ""}>Mock: ${G.esc(f.name)} scans</option>`).join("")}</select>
          <button class="mockbtn" style="flex:1;margin:0" data-act="other-scan">Mock: it scans code 2</button></div>`;
      return G.sheetShell("other", "Open on another phone", body, `<button class="btn block" data-act="sheet-close">Done</button>`);
    }
    return "";
  };

  G.dialogHTML = () => {
    const d = G.S.dialog;
    if (!d) return "";
    return `<div class="scrim center${G.anim("dialog", d.k)}" data-close="dialog-close"><div class="dialog" role="alertdialog" aria-modal="true" aria-label="${G.esc(d.title)}">
      <h2>${G.esc(d.title)}</h2><p class="hint">${G.esc(d.text)}</p>
      <div class="acts"><button class="btn" data-act="dialog-close">Cancel</button><button class="btn primary" data-act="restore-go" id="dlg-go">Restore</button></div></div></div>`;
  };
})();
