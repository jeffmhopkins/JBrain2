// The three brain-log mocks share this: a GrowBot Body tab with one robot, and the export
// control each variant puts on it. window.VARIANT picks A, B or C. No network request.
(function () {
  const V = window.VARIANT;
  const S = { hours: 3, redact: true, names: "Quorra, Tamsin", sheet: false, preview: false, toast: "", open: false };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const counts = { 1: [214, 61, 58, 1820, "1.1 MB"], 3: [598, 170, 166, 4980, "2.9 MB"], 6: [1140, 322, 317, 9410, "5.4 MB"] };
  const label = { 1: "1 hour", 3: "3 hours", 6: "6 hours" };
  const file = (h) => `growbot-brain-log-20261112-0300-${h}h.zip`;

  function mockbar() {
    const a = (id, t) => `<a href="${id.toLowerCase()}-${t}.html" class="${V === id ? "on" : ""}">${id} — ${t.replace(/-/g, " ")}</a>`;
    return `<details><summary class="mb-title">Mock · ${a("A", "share-sheet")} ${a("B", "quick-buttons")} ${a("C", "preview-first")}</summary>
      <div class="mb-in"><div class="mb-row"><button data-act="theme">Theme</button></div></div></details>`;
  }

  function seg() {
    const b = (h) => `<button class="${S.hours === h ? "on" : ""}" data-act="hours" data-arg="${h}" aria-pressed="${S.hours === h}">${label[h]}</button>`;
    return `<div class="seg" style="margin:0 0 14px">${b(1)}${b(3)}${b(6)}</div>`;
  }

  function privacy() {
    const c = (on, t1, t2, v) => `<label class="choice${on ? " sel" : ""}"><input type="radio" name="red" data-act="redact" data-arg="${v}" ${on ? "checked" : ""}><span class="tx"><span>${t1}</span><em>${t2}</em></span></label>`;
    return `<div class="choices" style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">
      ${c(S.redact, "Hash what people said", "Recommended. Transcripts and quoted speech become hashes, the way GrowBot's own diary does. The creature's words stay.", "1")}
      ${c(!S.redact, "Keep what people said", "Every transcript as it was heard.", "0")}</div>
      <label class="field">Names to hide <input data-act="names" value="${esc(S.names)}" placeholder="Comma-separated">
      <span class="hint dim" style="display:block;margin-top:6px">Replaced with [name] everywhere in the file, in either mode. Remembered on this phone.</span></label>`;
  }

  function inside() {
    return `<p class="hint dim">Every model turn, transcription and spoken line with where the time went, the creature's own diary, and a latency summary. No camera frames, keys, codes or box ids: personas and robots are numbered.</p>`;
  }

  function preview() {
    const [llm, stt, tts, diary, size] = counts[S.hours];
    return `<div class="card tight" style="margin-bottom:12px"><div class="facts" style="display:grid;grid-template-columns:1fr auto;gap:4px 12px;font-size:var(--fs-secondary)">
      <span>Model turns</span><b>${llm}</b><span>Transcriptions</span><b>${stt}</b><span>Spoken lines</span><b>${tts}</b><span>Diary rows</span><b>${diary}</b><span>About</span><b>${size}</b></div>
      <p class="hint dim" style="margin-top:10px">A transcription as it will read: <span class="mono">${S.redact ? "«3fa91c0e·5w»" : "can you dance for [name]"}</span></p></div>`;
  }

  function body() {
    let h = `<section class="card"><div style="display:flex;align-items:center;gap:8px"><span class="dot" style="width:10px;height:10px;border-radius:50%;background:var(--green)"></span><b>Fish</b><span class="hint">on · seen 12 s ago</span></div>
      <p class="hint" style="margin-top:8px">Sprout is wearing it, on Pixel 6a.</p></section>
      <button class="btn block">Add a robot</button>`;
    if (V === "A") h += `<div class="sec-h">For GrowBot's developer</div><button class="btn block" data-act="sheet">Share a brain log…</button>`;
    if (V === "B") h += `<section class="card"><b>Brain log for GrowBot's developer</b><p class="hint" style="margin:6px 0 12px">Download the last…</p>
      <div style="display:flex;gap:8px"><button class="btn" style="flex:1" data-act="get" data-arg="1">Hour</button><button class="btn" style="flex:1" data-act="get" data-arg="3">3 hours</button><button class="btn" style="flex:1" data-act="get" data-arg="6">6 hours</button></div>
      <details style="margin-top:12px" ${S.open ? "open" : ""}><summary class="hint" data-act="open">${S.redact ? "Speech hashed" : "Speech kept"} · ${S.names.split(",").filter((x) => x.trim()).length} names hidden</summary><div style="margin-top:10px">${privacy()}</div></details></section>`;
    if (V === "C") h += `<div class="sec-h">For GrowBot's developer</div><button class="btn block" data-act="sheet">Brain log…</button>`;
    return h;
  }

  function sheet() {
    if (!S.sheet) return "";
    const foot = V === "C" && !S.preview
      ? `<button class="btn primary block" data-act="preview">See what's in it</button>`
      : `<button class="btn primary block" data-act="get" data-arg="${S.hours}">Download brain log (.zip)</button>`;
    return `<div class="scrim" data-act="close"><div class="sheet" role="dialog" aria-label="Brain log" data-stop="1"><div class="handle"></div>
      <div class="sh"><span class="t">${V === "C" ? "Brain log" : "Share a brain log"}</span><button class="x" data-act="close" aria-label="Close">✕</button></div>
      <div style="padding:0 16px 16px;overflow-y:auto">${seg()}${privacy()}${V === "C" && S.preview ? preview() : inside()}<div style="margin-top:12px">${foot}</div></div></div></div>`;
  }

  function render() {
    document.getElementById("mockbar").innerHTML = mockbar();
    document.getElementById("phone").innerHTML = `<div class="topbar"><span class="ttl">GrowBot</span></div>
      <div class="seg"><button>Personas<span class="n">2</span></button><button class="on">Body</button><button>Phones</button></div>
      <div class="body">${body()}</div>${sheet()}${S.toast ? `<div class="toast" role="status" style="position:absolute;left:16px;right:16px;bottom:16px">${esc(S.toast)}</div>` : ""}`;
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-act]");
    if (!t) return;
    if (t.dataset.act === "close" && e.target.closest("[data-stop]") && !e.target.closest(".x")) return;
    const a = t.dataset.act, arg = t.dataset.arg;
    if (a === "theme") document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    if (a === "sheet") { S.sheet = true; S.preview = false; }
    if (a === "close") S.sheet = false;
    if (a === "hours") { S.hours = +arg; S.preview = false; }
    if (a === "redact") S.redact = arg === "1";
    if (a === "open") { e.preventDefault(); S.open = !S.open; }
    if (a === "preview") S.preview = true;
    if (a === "get") { S.sheet = false; S.toast = `Saved ${file(arg)} to Downloads. Send it to GrowBot's developer as it is.`; setTimeout(() => { S.toast = ""; render(); }, 4000); }
    if (a !== "names") render();
  });
  document.addEventListener("input", (e) => { if (e.target.dataset.act === "names") S.names = e.target.value; });
  render();
})();
