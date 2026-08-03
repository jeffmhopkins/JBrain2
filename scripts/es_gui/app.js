"use strict";
// PWA controller for the Erdős–Straus 10^12 launcher.
// - polls /api/status + /api/metrics for the dashboard,
// - streams /api/stream (SSE) into a live terminal that reattaches on reopen,
// - drives start / kill / publish, and surfaces the public share link.

const $ = (id) => document.getElementById(id);

// --- token handling: accept #token=… once, then persist locally -------------
(function initToken() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    localStorage.setItem("es_token", decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();
const token = () => localStorage.getItem("es_token") || "";
const authHeaders = () => (token() ? { Authorization: "Bearer " + token() } : {});
const withTok = (url) => (token() ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token()) : url);

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (r.status === 401) { showGate(); throw new Error("unauthorized"); }
  return r;
}

function showGate() { $("tokenGate").hidden = false; }
$("tokenBtn").onclick = () => {
  localStorage.setItem("es_token", $("tokenInput").value.trim());
  $("tokenGate").hidden = true;
  poll();
  openTerminal();
};

// --- dashboard rendering ----------------------------------------------------
const PHASE_LABEL = {
  idle: "idle", setup: "setup", starting: "starting", generation: "generating",
  verification: "verifying", packaging: "packaging", complete: "complete",
};

function setPill(text, cls) {
  const p = $("phasePill");
  p.textContent = text;
  p.className = "pill" + (cls ? " " + cls : "");
}

function renderStatus(s) {
  const label = PHASE_LABEL[s.phase] || s.phase;
  $("mPhase").textContent = label;
  $("mRunning").textContent = s.running ? "yes" : (s.starting ? "starting" : "no");

  if (s.phase === "complete") setPill(s.verify_ok ? "✓ complete" : "complete", "ok");
  else if (s.exit_code && s.exit_code !== 0) setPill("failed (exit " + s.exit_code + ")", "err");
  else if (s.running || s.starting) setPill("● " + label, "run");
  else setPill(label);

  const t = s.timings || {};
  $("mElapsed").textContent = t.elapsed || "—";

  if (s.scratch) {
    $("mScratch").textContent = s.scratch.pct + "%";
    $("scratchBar").style.width = s.scratch.pct + "%";
  } else {
    $("mScratch").textContent = s.phase === "complete" ? "done" : "—";
    $("scratchBar").style.width = s.phase === "complete" ? "100%" : "0";
  }

  fillKV("timings", [
    ["Generation", t.generation], ["Verification", t.verification],
    ["Packaging", t.packaging], ["Total", t.total],
  ]);
  fillKV("specs", Object.entries(s.specs || {}).map(([k, v]) => [k, v]));

  const hc = $("headlineCard");
  if (s.headline && s.headline.length) {
    hc.hidden = false;
    $("headline").textContent = s.headline.join("\n");
  } else hc.hidden = true;

  const sc = $("shareCard");
  if (s.sharelink) {
    sc.hidden = false;
    $("shareLink").href = s.artifact_url || s.sharelink;
    $("shareLink").textContent = s.artifact_url || s.sharelink;
    $("shareSub").textContent = "Public when this dashboard is reached over your tunnel domain.";
  } else sc.hidden = true;

  // Button availability follows the run lifecycle.
  const busy = s.running || s.starting;
  $("btnStart").disabled = busy;
  $("btnStop").disabled = !busy;
  $("btnPublish").disabled = !(s.phase === "complete" && s.verify_ok);
}

function fillKV(id, rows) {
  const tb = $(id).querySelector("tbody");
  tb.innerHTML = "";
  for (const [k, v] of rows) {
    if (v == null || v === "") continue;
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    td1.textContent = k;
    td2.textContent = v;
    tr.append(td1, td2);
    tb.append(tr);
  }
}

function renderMetrics(m) {
  const cpu = m.cpu_pct == null ? 0 : m.cpu_pct;
  $("mCpu").textContent = cpu.toFixed(0) + "%" + (m.cores ? " · " + m.cores + " cores" : "");
  $("cpuBar").style.width = cpu + "%";
  if (m.mem && m.mem.total_gb) {
    $("mRam").textContent = m.mem.used_gb + " / " + m.mem.total_gb + " GB";
    $("ramBar").style.width = m.mem.pct + "%";
  }
}

async function poll() {
  try {
    const [s, m] = await Promise.all([
      api("/api/status").then((r) => r.json()),
      api("/api/metrics").then((r) => r.json()),
    ]);
    renderStatus(s);
    renderMetrics(m);
  } catch (_) { /* keep the last view; next tick retries */ }
}

// --- controls ---------------------------------------------------------------
function flash(msg) { $("actionMsg").textContent = msg; }

$("btnStart").onclick = async () => {
  if (!confirm("Start the ~6–10 h census run? It uses all cores and is not resumable.")) return;
  flash("starting…");
  const r = await api("/api/start", { method: "POST" });
  flash(r.ok ? "launch requested — watch the terminal" : "already running");
  openTerminal(); poll();
};

$("btnStop").onclick = async () => {
  if (!confirm("Kill the run? It is not resumable — a restart begins from scratch.")) return;
  flash("killing…");
  await api("/api/stop", { method: "POST" });
  flash("stop requested"); poll();
};

$("btnPublish").onclick = async () => {
  flash("packaging & verifying…");
  const r = await api("/api/publish", { method: "POST" });
  const j = await r.json();
  flash(j.ok ? "published" : "publish refused — check verification");
  poll();
};

$("btnCopy").onclick = async () => {
  try { await navigator.clipboard.writeText($("shareLink").href); flash("link copied"); }
  catch (_) { flash("copy failed"); }
};

// --- live terminal (SSE, reattaches on reopen) ------------------------------
let es = null;
const term = $("terminal");
const MAX_CHARS = 400_000; // cap the DOM buffer for long runs

function appendTerm(text) {
  const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
  term.textContent += text;
  if (term.textContent.length > MAX_CHARS) {
    term.textContent = term.textContent.slice(-MAX_CHARS);
  }
  if ($("follow").checked || atBottom) term.scrollTop = term.scrollHeight;
}

function openTerminal() {
  closeTerminal();
  term.classList.remove("closed");
  term.textContent = "";
  // No stored offset across opens: the server keeps the feed, so we re-request
  // the recent tail and then stream live. Reset events clear on a new run.
  es = new EventSource(withTok("/api/stream?from=tail"));
  es.onmessage = (e) => appendTerm(e.data + "\n");
  es.addEventListener("reset", () => { term.textContent = ""; });
  es.onerror = () => { /* EventSource auto-reconnects; Last-Event-ID resumes */ };
  $("btnTerm").textContent = "Close";
}

function closeTerminal() {
  if (es) { es.close(); es = null; }
}

$("btnTerm").onclick = () => {
  if (es) { closeTerminal(); term.classList.add("closed"); $("btnTerm").textContent = "Open"; }
  else { openTerminal(); }
};

// Pause the stream while hidden (mobile background), resume on return — the
// server-side feed persists, so nothing is lost.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) closeTerminal();
  else if (!term.classList.contains("closed")) openTerminal();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

poll();
setInterval(poll, 2000);
openTerminal();
