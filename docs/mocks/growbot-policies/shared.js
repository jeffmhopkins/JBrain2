// The simulated upstream check every variant renders. The mock bar (dashed, above the phone)
// switches what the last check found; it is mock controls, not design.
window.G = (function () {
  const S = { state: "behind", checking: false, theme: "dark", sheet: false, toast: "" };
  const PIN = { sha: "4a2cfc29ea5e", date: "2026-10-13" };
  const FOUND = {
    current: { head: PIN, commits: [], policies: [], other: 0, checked: "2 hours ago" },
    behind: {
      head: { sha: "9e41b07c22d1", date: "2026-11-09" },
      commits: [
        { title: "policies: diy-v2 walk and spin, trained on the 85 mm body", date: "Nov 9" },
        { title: "normal-spin: retune cw for MG90S backlash", date: "Nov 4" },
        { title: "app: steadier gait handoff", date: "Oct 28" },
      ],
      policies: [
        { path: "policies/diy-v2/walk_fwd.json", change: "new" },
        { path: "policies/diy-v2/spin_cw.json", change: "new" },
        { path: "policies/diy-v2/spin_ccw.json", change: "new" },
        { path: "policies/normal-spin-1005/cw.json", change: "changed" },
      ],
      other: 7,
      checked: "12 minutes ago",
    },
    error: { checked: "yesterday", error: "GitHub didn't answer. The box tries again in 12 hours." },
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const f = () => FOUND[S.state];

  function stateLine() {
    if (S.checking) return `<span class="state"><span class="dot"></span><span class="dim-t">Checking GitHub…</span></span>`;
    if (S.state === "current") return `<span class="state"><span class="dot ok"></span><span class="ok-t">Up to date</span></span>`;
    if (S.state === "behind") return `<span class="state"><span class="dot warn"></span><span class="warn-t">${f().policies.length} new or changed</span></span>`;
    return `<span class="state"><span class="dot bad"></span><span class="bad-t">Couldn't check</span></span>`;
  }

  function policyList() {
    return `<ul class="rlist">${f().policies.map((p) => `<li><span class="nm mono">${esc(p.path.replace(/^policies\//, ""))}</span><span class="badge ${p.change === "new" ? "ok" : "warn"}">${p.change}</span></li>`).join("")}</ul>`;
  }

  function commitList() {
    return `<details class="more"><summary>${f().commits.length} commits since your copy, ${f().other} other files</summary><ul class="rlist">${f().commits.map((c) => `<li><span class="nm">${esc(c.title)}</span><span class="when">${c.date}</span></li>`).join("")}</ul></details>`;
  }

  // How to bring them in: a sync of the GrowBot copy is a pull request, so the page says who does it.
  function howTo() {
    return `<p class="hint">These come in with a GrowBot sync, which is a code change: ask Claude to "sync GrowBot to upstream". When it has merged, <strong>Ops → Update</strong> puts the new walks and spins on the robot.</p>`;
  }

  function facts() {
    const x = f();
    return `<dl class="facts"><dt>Your copy</dt><dd class="mono">${PIN.sha.slice(0, 7)} · ${PIN.date}</dd>${x.head ? `<dt>Upstream</dt><dd class="mono">${x.head.sha.slice(0, 7)} · ${x.head.date}</dd>` : ""}<dt>Checked</dt><dd>${x.checked}</dd></dl>`;
  }

  function checkBtn(cls) {
    return `<button class="btn ${cls || ""}" data-act="check"${S.checking ? " disabled" : ""}>${S.checking ? "Checking…" : "Check now"}</button>`;
  }

  function mockbar(el, variant, render) {
    const b = (k, v, l) => `<button data-mk="${k}" data-v="${v}" class="${S[k] === v ? "on" : ""}">${l}</button>`;
    el.innerHTML = `<div class="mb-title">Mock ${variant} · what the last check found</div>
      <div class="mb-row">${b("state", "current", "Up to date")}${b("state", "behind", "New policies")}${b("state", "error", "Couldn't check")}
      <span style="flex:1"></span>${b("theme", "dark", "Dark")}${b("theme", "light", "Light")}</div>`;
    el.onclick = (e) => {
      const t = e.target.closest("button[data-mk]");
      if (!t) return;
      S[t.dataset.mk] = t.dataset.v;
      S.sheet = false;
      document.documentElement.dataset.theme = S.theme;
      mockbar(el, variant, render);
      render();
    };
  }

  function wire(root, render, extra) {
    root.onclick = (e) => {
      const t = e.target.closest("[data-act]");
      if (!t) return;
      const act = t.dataset.act;
      if (act === "check") {
        S.checking = true; render();
        setTimeout(() => { S.checking = false; if (S.state === "error") S.state = "current"; S.toast = S.state === "behind" ? "Still 4 new or changed policies upstream." : "Up to date with upstream."; render(); setTimeout(() => { S.toast = ""; render(); }, 2600); }, 900);
      } else if (act === "github") {
        S.toast = "Opens github.com/britcruise9/GrowBot/compare in a new tab."; render(); setTimeout(() => { S.toast = ""; render(); }, 2600);
      } else if (extra) extra(act, t);
    };
  }

  const toast = () => (S.toast ? `<div class="toast" role="status">${esc(S.toast)}</div>` : "");
  return { S, f, esc, stateLine, policyList, commitList, howTo, facts, checkBtn, mockbar, wire, toast };
})();
