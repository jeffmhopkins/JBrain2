/* The persona page's Checkpoints tab in three variants. Everything the three share lives here
   (the state, the compact bar, the tabs, the swipe rail, the sheets, the toast), so the
   variants differ only in how the list is laid out. No network request is made. */
(function () {
  const VARIANTS = [
    ["a-chats-list.html", "A — the Chats list"],
    ["b-by-day.html", "B — grouped by day"],
    ["c-row-sheet.html", "C — a row opens a sheet"],
  ];
  const now = new Date(2026, 10, 12, 9, 30);
  const at = (d, h, m) => new Date(2026, 10, d, h, m);
  const seed = () => [
    { id: "c6", name: "Before editing personality", auto: true, why: "made before an edit", t: at(12, 8, 41), vm: 2 },
    { id: "c5", name: "Before restoring “Day 1”", auto: true, why: "made before a restore", t: at(12, 8, 2), vm: 2 },
    { id: "c4", name: "Day 1", auto: false, why: "", t: at(11, 20, 25), vm: 0 },
    { id: "c3", name: "Before editing expression", auto: true, why: "made before an edit", t: at(11, 19, 58), vm: 0 },
    { id: "c2", name: "Before reset", auto: true, why: "made before a reset", t: at(10, 14, 12), vm: 3 },
    { id: "c1", name: "Before the beach trip", auto: false, why: "", t: new Date(2026, 9, 29, 10, 5), vm: 3 },
  ];
  const S = { cps: seed(), rail: null, armed: null, sheet: null, dialog: null, toast: null, filter: "all" };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const two = (n) => String(n).padStart(2, "0");
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  function dayLabel(t) {
    if (sameDay(t, now)) return "Today";
    if (sameDay(t, new Date(now.getTime() - 864e5))) return "Yesterday";
    return `${t.getDate()} ${MON[t.getMonth()]}`;
  }
  const time = (t) => `${two(t.getHours())}:${two(t.getMinutes())}`;
  const when = (t) => `${t.getDate()} ${MON[t.getMonth()]}, ${time(t)}`;
  const shortWhen = (t) => (sameDay(t, now) ? time(t) : `${t.getDate()} ${MON[t.getMonth()]}`);
  const vms = (n) => (n ? `${n} visual memor${n === 1 ? "y" : "ies"}` : "");

  const ICON = {
    restore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
    seed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v8M8 12h8"/></svg>',
    del: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
    car: '<svg class="car" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg>',
  };
  const RAIL = 3 * 64;

  let armTimer = 0;
  function arm(key) {
    if (S.armed === key) { S.armed = null; clearTimeout(armTimer); return true; }
    S.armed = key;
    clearTimeout(armTimer);
    armTimer = setTimeout(() => { S.armed = null; G.render(); }, 3000);
    return false;
  }
  let toastTimer = 0;
  function say(text, undo) {
    S.toast = { text, undo };
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { S.toast = null; G.render(); }, 5000);
  }

  // The compact row: a dot (filled named, hollow automatic), the name, the time, a chevron.
  function row(c, { rail = true, meta } = {}) {
    const open = S.rail === c.id;
    const delArmed = S.armed === `del:${c.id}`;
    const railHtml = rail && open
      ? `<div class="rail">
          <button class="r-restore" data-act="restore" data-arg="${c.id}">${ICON.restore}restore</button>
          <button class="r-seed" data-act="seed" data-arg="${c.id}">${ICON.seed}new persona</button>
          <button class="r-del${delArmed ? " armed" : ""}" data-act="del" data-arg="${c.id}">${delArmed ? "tap again" : `${ICON.del}delete`}</button>
        </div>` : "";
    return `<div class="wrap${c.auto ? " auto-row" : ""}">${railHtml}
      <div class="slide" data-row="${c.id}" data-swipe="${rail ? 1 : 0}" style="transform:translateX(${open ? -RAIL : 0}px)">
        <button class="tap" data-act="open" data-arg="${c.id}">
          <span class="dot${c.auto ? " auto" : ""}" title="${c.auto ? "automatic" : "named"}"></span>
          <span class="m-title">${esc(c.name)}</span>
          <span class="m-meta">${c.vm ? `<span class="vm">${c.vm} vm</span>` : ""}<span>${meta ? meta(c) : shortWhen(c.t)}</span></span>
          ${ICON.car}
        </button>
      </div></div>`;
  }

  function sheet() {
    if (!S.sheet) return "";
    if (S.sheet.kind === "save") {
      return `<div class="scrim" data-act="close"><div class="sheet" data-stop>
        <div class="grab"></div><h3>Save a checkpoint</h3>
        <p class="kv">A copy of Fish's whole soul and visual memories as they are now, kept on the box until you delete it.</p>
        <input id="cpname" placeholder="Name it, e.g. Before the trip" maxlength="80" value="${esc(S.sheet.draft || "")}">
        <div class="acts"><button class="btn primary block" data-act="save">Save</button></div>
      </div></div>`;
    }
    const c = S.cps.find((x) => x.id === S.sheet.id);
    if (!c) return "";
    const delArmed = S.armed === `del:${c.id}`;
    return `<div class="scrim" data-act="close"><div class="sheet" data-stop>
      <div class="grab"></div><h3>${esc(c.name)}</h3>
      <p class="kv">${when(c.t)}${c.auto ? ` · automatic, ${esc(c.why)}` : " · named"}<br>Keeps its whole soul${c.vm ? ` and ${vms(c.vm)}` : " and no visual memories"}.</p>
      <div class="acts">
        <button class="btn primary block" data-act="restore" data-arg="${c.id}">Restore Fish to this</button>
        <button class="btn block" data-act="seed" data-arg="${c.id}">New persona from it</button>
        <button class="btn block danger${delArmed ? " armed" : ""}" data-act="del" data-arg="${c.id}">${delArmed ? "Tap again — this can't be undone" : "Delete checkpoint"}</button>
      </div></div></div>`;
  }

  function dialog() {
    const c = S.dialog && S.cps.find((x) => x.id === S.dialog);
    if (!c) return "";
    return `<div class="scrim dialog-scrim"><div class="dialog">
      <h3>Restore “${esc(c.name)}”?</h3>
      <p>Fish goes back to how it was on ${when(c.t)}, and gets back the visual memories it had then. Its soul as it is now is saved as a checkpoint first, so this can be undone.</p>
      <div class="row"><button class="btn" data-act="dclose">Cancel</button><button class="btn primary" data-act="drestore" data-arg="${c.id}">Restore</button></div>
    </div></div>`;
  }

  const G = {
    S, esc, row, dayLabel, when, shortWhen, vms, arm, say, ICON,
    variant: "",
    list: () => "",
    render() {
      const me = location.pathname.split("/").pop();
      document.getElementById("mockbar").innerHTML =
        VARIANTS.map(([f, l]) => `<a href="${f}" class="${f === me ? "on" : ""}">${l}</a>`).join(" · ") +
        ` <button data-act="theme">Theme</button> <button data-act="reset">Reset data</button> <button data-act="empty">Empty</button>
        <span class="why">${G.why || ""}</span>`;
      const n = S.cps.length;
      document.getElementById("phone").innerHTML = `
        <div class="pbar"><button class="back" aria-label="Back to personas">‹</button>
          <span class="ttl">Fish</span><span class="sub">on this phone<br>wearing Fish</span></div>
        <div class="seg" role="tablist"><button>Soul</button><button>Memories <span class="seg-n">2</span></button>
          <button class="on">Checkpoints <span class="seg-n">${n}</span></button><button>Manage</button></div>
        <div class="body">${G.list()}</div>
        <div class="foot"><button class="btn primary">Open Fish…</button></div>
        ${S.toast ? `<div class="toast"><span>${esc(S.toast.text)}</span>${S.toast.undo ? '<button data-act="undo">Undo</button>' : ""}</div>` : ""}
        ${sheet()}${dialog()}`;
      const inp = document.getElementById("cpname");
      if (inp) { inp.focus(); inp.oninput = () => { S.sheet.draft = inp.value; }; inp.onkeydown = (e) => { if (e.key === "Enter") ACT.save(); }; }
    },
  };

  const ACT = {
    theme() { const d = document.documentElement; d.dataset.theme = d.dataset.theme === "dark" ? "light" : "dark"; },
    reset() { S.cps = seed(); S.rail = null; },
    empty() { S.cps = []; S.rail = null; },
    filter(f) { S.filter = f; S.rail = null; },
    newcp() { S.sheet = { kind: "save", draft: "" }; },
    save() {
      const name = (S.sheet.draft || "").trim();
      if (!name) return;
      S.cps.unshift({ id: `n${Date.now()}`, name, auto: false, why: "", t: new Date(now.getTime() + 6e4), vm: 2 });
      S.sheet = null; say(`Saved “${name}”.`);
    },
    open(id) {
      if (S.rail) { S.rail = null; return; }
      if (G.onOpen) G.onOpen(id); else S.sheet = { kind: "cp", id };
    },
    close() { S.sheet = null; S.armed = null; },
    restore(id) { S.sheet = null; S.rail = null; S.dialog = id; },
    dclose() { S.dialog = null; },
    drestore(id) {
      const c = S.cps.find((x) => x.id === id);
      S.dialog = null;
      const saved = { id: `r${Date.now()}`, name: `Before restoring “${c.name}”`, auto: true, why: "made before a restore", t: new Date(now.getTime() + 6e4), vm: 2 };
      S.cps.unshift(saved);
      say(`Fish is back to “${c.name}”. “${saved.name}” keeps what it was.`, true);
    },
    undo() { S.toast = null; say("Undone — Fish is as it was before."); },
    seed(id) { const c = S.cps.find((x) => x.id === id); S.sheet = null; S.rail = null; say(`New persona from “${c.name}” — opens the New persona sheet with it picked.`); },
    del(id) {
      if (!arm(`del:${id}`)) return;
      const c = S.cps.find((x) => x.id === id);
      S.cps = S.cps.filter((x) => x.id !== id);
      S.sheet = null; S.rail = null;
      say(`Deleted “${c.name}”.`);
    },
    delauto() {
      if (!arm("delauto")) return;
      const n = S.cps.filter((c) => c.auto).length;
      S.cps = S.cps.filter((c) => !c.auto);
      say(`Deleted ${n} automatic checkpoint${n === 1 ? "" : "s"}. Named ones are kept.`);
    },
  };
  G.ACT = ACT;

  document.addEventListener("click", (e) => {
    const stop = e.target.closest("[data-stop]");
    const t = e.target.closest("[data-act]");
    if (!t || (stop && !stop.contains(t))) return;
    if (t.dataset.act === "close" && stop) return;
    if (dragged) { dragged = false; return; }
    const f = ACT[t.dataset.act];
    if (f) { f(t.dataset.arg); G.render(); }
  });

  // The swipe: drag a row left past half the rail to open it; one rail open at a time.
  let drag = null, dragged = false;
  document.addEventListener("pointerdown", (e) => {
    const s = e.target.closest(".slide[data-swipe='1']");
    if (!s || e.target.closest(".rail")) return;
    const open = S.rail === s.dataset.row;
    drag = { el: s, id: s.dataset.row, x0: e.clientX, y0: e.clientY, base: open ? -RAIL : 0, dx: 0, h: null };
  });
  document.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (drag.h === null && Math.abs(dx) + Math.abs(dy) > 8) drag.h = Math.abs(dx) > Math.abs(dy);
    if (!drag.h) return;
    drag.dx = dx;
    drag.el.style.transition = "none";
    drag.el.style.transform = `translateX(${Math.max(-RAIL, Math.min(0, drag.base + dx))}px)`;
  });
  document.addEventListener("pointerup", () => {
    if (!drag) return;
    if (drag.h) {
      dragged = true;
      setTimeout(() => { dragged = false; }, 0);
      S.rail = drag.base + drag.dx < -RAIL / 2 ? drag.id : null;
      S.armed = null;
      drag.el.style.transition = "";
      G.render();
    }
    drag = null;
  });

  window.G = G;
})();
