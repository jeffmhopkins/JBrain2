// Generates the four artboards of docs/mocks/sdr-tuning-view.
//
// The traces are synthesised rather than drawn by hand so the picture obeys the same
// arithmetic the real strip will: a span of twice the demodulator passband, a dB
// window, and signal shapes with the right widths for their mode.

import { writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Run it from anywhere: `node docs/mocks/sdr-tuning-view/generate.mjs`.
const OUT = dirname(fileURLToPath(import.meta.url));

const W = 362; // 390 phone − 2×14 body padding
const H = 78; // the chart — tall enough to read a shoulder, short enough to leave the transport on screen
const TOP_DB = -12;
const BOT_DB = -88;
const PTS = 182; // 2 px a point

// A tiny deterministic PRNG: the same picture every regeneration, so a diff of these
// files is a design change and never noise.
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/** A signal: flat-topped, gaussian shoulders — what an FM carrier actually looks like
 *  on a spectrum, as opposed to the single spike a naive mock draws. */
function signal(offsetHz, peakDb, widthHz) {
  return (hz) => {
    const d = Math.abs(hz - offsetHz);
    const flat = widthHz / 2;
    if (d <= flat * 0.72) return peakDb;
    const t = (d - flat * 0.72) / (flat * 0.45);
    return peakDb - 46 * t * t;
  };
}

function trace(spanHz, floorDb, signals, seed) {
  const rand = rng(seed);
  const out = [];
  // Two-pole smoothing on white noise: a floor with grain rather than a fuzz, which is
  // what an averaged FFT of thermal noise looks like at this width.
  let a = 0;
  let b = 0;
  for (let i = 0; i < PTS; i += 1) {
    const hz = (i / (PTS - 1) - 0.5) * spanHz;
    a = a * 0.55 + (rand() - 0.5) * 2;
    b = b * 0.72 + a * 0.28;
    let db = floorDb + b * 5.5;
    for (const s of signals) db = Math.max(db, s(hz) + b * 1.4);
    out.push(db);
  }
  return out;
}

const y = (db) => {
  const t = (db - BOT_DB) / (TOP_DB - BOT_DB);
  return (4 + (1 - Math.min(1, Math.max(0, t))) * (H - 8)).toFixed(1);
};
const x = (i) => ((i / (PTS - 1)) * W).toFixed(1);

function paths(db) {
  const line = db.map((v, i) => `${i ? "L" : "M"}${x(i)} ${y(v)}`).join("");
  return { line, area: `${line}L${W} ${H}L0 ${H}Z` };
}

/** The strip itself. `passHz` is what the demodulator hears; the span is twice it, so
 *  the shaded passband is ALWAYS the middle half — the invariant that makes "am I
 *  centred?" answerable without reading a number. */
function strip({ passHz, spanHz, db, peakHz, level, status, ok, unit, ticks }) {
  const { line, area } = paths(db);
  const passX = (W * (1 - passHz / spanHz)) / 2;
  const passW = W * (passHz / spanHz);
  const peakX = W * (0.5 + peakHz / spanHz);
  const peakI = Math.round((peakX / W) * (PTS - 1));
  const peakY = y(db[Math.min(PTS - 1, Math.max(0, peakI))]);
  const caret = ok
    ? ""
    : `<g transform="translate(${peakX.toFixed(1)} 0)">
        <path d="M0 ${peakY}l-4 -7h8Z" fill="var(--amber)"/>
        <line x1="0" y1="${(Number(peakY) - 9).toFixed(1)}" x2="0" y2="4" stroke="var(--amber)" stroke-width="1" stroke-dasharray="2 3" opacity=".7"/>
      </g>`;
  return `      <p class="sdr-label tv-label">Tuning<span class="tv-span">${
    spanHz >= 1000 ? `${(spanHz / 1000).toFixed(spanHz % 1000 ? 1 : 0)} kHz` : `${spanHz} Hz`
  } view · ${(passHz / 1000).toFixed(passHz % 1000 ? 1 : 0)} kHz passband</span></p>
      <div class="tv-chart">
        <span class="tv-lvl">${level}</span>
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
          <rect x="${passX.toFixed(1)}" y="0" width="${passW.toFixed(1)}" height="${H}" fill="var(--steel-tint)"/>
          <line x1="${passX.toFixed(1)}" y1="0" x2="${passX.toFixed(1)}" y2="${H}" stroke="var(--steel)" stroke-width="1" opacity=".6"/>
          <line x1="${(passX + passW).toFixed(1)}" y1="0" x2="${(passX + passW).toFixed(1)}" y2="${H}" stroke="var(--steel)" stroke-width="1" opacity=".6"/>
          <path d="${area}" fill="var(--steel)" opacity=".22"/>
          <path d="${line}" fill="none" stroke="var(--steel)" stroke-width="1.25" stroke-linejoin="round"/>
          <line x1="${W / 2}" y1="0" x2="${W / 2}" y2="${H}" stroke="var(--text)" stroke-width="1" opacity=".8"/>
          ${caret}
        </svg>
      </div>
      <div class="tv-axis">${ticks.map((t, i) => `<span>${t}${i === ticks.length - 1 ? ` ${unit}` : ""}</span>`).join("")}</div>
      <p class="tv-status"><span class="dot${ok ? " on" : " warn"}"></span>${status}</p>`;
}

const HEAD = (title) => `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${title}</title>
<script src="./support.js"></script>
<style>
  html, body { margin: 0; padding: 0; background: #0e0f11; }
  .ab {
    --bg: #0e0f11; --surface: #17181b; --surface-2: #1e2024; --border: #26282c;
    --text: #e6e7e9; --text-2: #9a9da3; --text-3: #5c5f66;
    --steel: #7fa7c9; --steel-tint: rgba(127, 167, 201, 0.13);
    --green: #8fbc9a; --amber: #c9a36a; --amber-tint: rgba(201, 163, 106, 0.13);
    --rose: #cf8a8f;
    --accent: var(--steel); --accent-tint: var(--steel-tint);
    --ok: var(--green); --warn: var(--amber); --mode: var(--steel); --mode-tint: var(--steel-tint);
    --font-scale: 0.75;
    --fs-micro: calc(12px * var(--font-scale));
    --fs-secondary: calc(14px * var(--font-scale));
    --fs-note: calc(15px * var(--font-scale));
    --r-input: 12px; --r-seg: 14px; --r-pill: 999px;
    --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
    width: 390px; height: 844px; box-sizing: border-box;
    background: var(--bg); color: var(--text); overflow: hidden;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .ab * { box-sizing: border-box; }
  .ab button { font: inherit; }
  .body { padding: 10px 14px 28px; }
  .rdetail-top { display: flex; align-items: center; gap: 4px; margin-bottom: 6px; }
  .radio-back { border: 0; background: none; color: var(--text-2); font-size: 22px; line-height: 1; padding: 0 4px; }
  .rdetail-title { margin: 0; font-size: var(--fs-note); font-weight: 600; }
  .rdesc { margin: 0; font-size: var(--fs-micro); color: var(--text-2); }
  .rstate { display: flex; align-items: center; gap: 7px; font-size: var(--fs-micro); color: var(--text-2); margin-top: 3px; }
  .rser { font-size: var(--fs-micro); font-variant-numeric: tabular-nums; color: var(--text-3); }
  .rused { margin-left: auto; }
  .dot { width: 8px; height: 8px; flex: none; border-radius: 50%; background: var(--text-3); }
  .dot.on { background: var(--ok); }
  .dot.warn { background: var(--warn); }
  .jobs { display: flex; align-items: center; gap: 6px; margin: 12px 0 0; padding: 0; border: 0; }
  .jobs .lbl { padding: 0; font-size: var(--fs-micro); letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3); }
  .jobs button { flex: 1; padding: 9px 4px; min-height: 44px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text-2); font-size: var(--fs-micro); }
  .jobs button[aria-pressed="true"] { border-color: var(--accent); background: var(--accent-tint); color: var(--text); }
  .jobs button:disabled { opacity: 0.4; }
  .jobsurface { margin-top: 14px; }
  .sdr-note { font-size: var(--fs-secondary); color: var(--text-2); line-height: 1.5; margin: 0 0 14px; }
  .sdr-label { font-size: var(--fs-micro); letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-3); font-weight: 600; margin: 14px 2px 8px; }
  .sdr-readout { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--r-input); padding: 14px; text-align: center; }
  .sdr-freq { font-family: var(--font-mono); font-size: 34px; font-weight: 600; letter-spacing: -0.5px; font-variant-numeric: tabular-nums; line-height: 1.1; display: block; width: 100%; border: 0; background: none; color: inherit; padding: 0; text-align: center; }
  .sdr-unit { font-size: var(--fs-note); color: var(--text-2); font-weight: 400; margin-left: 4px; }
  .sdr-station { font-size: var(--fs-secondary); color: var(--text-2); margin-top: 5px; }
  .sdr-tuner { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 12px; }
  .sdr-step { border: 1px solid var(--border); background: var(--surface); color: var(--text-2); width: 44px; height: 44px; border-radius: var(--r-input); font-size: 20px; }
  .sdr-stepsize { font-size: var(--fs-micro); color: var(--text-3); letter-spacing: 0.06em; text-transform: uppercase; min-width: 66px; border: 1px solid var(--border); border-radius: var(--r-pill); background: var(--surface); padding: 6px 10px; }
  .seg-row { display: flex; border: 1px solid var(--border); border-radius: var(--r-seg); overflow: hidden; }
  .seg { flex: 1; display: flex; align-items: center; justify-content: center; padding: 0.85em 0; font-size: var(--fs-note); font-weight: 500; color: var(--text-2); background: transparent; border: none; }
  .seg + .seg { border-left: 1px solid var(--border); }
  .seg-on { color: var(--text); font-weight: 600; background: var(--mode-tint); }
  .sdr-face { position: relative; margin: 14px 0 0; }
  .sdr-tape { width: 100%; height: 88px; display: block; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
  .sdr-face-elapsed { position: absolute; top: 7px; right: 10px; font-family: var(--font-mono); font-size: var(--fs-micro); color: var(--text-3); font-variant-numeric: tabular-nums; }
  .sdr-transport { display: flex; align-items: center; gap: 10px; margin: 12px 0 4px; }
  .sdr-play { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 46px; height: 46px; border-radius: 50%; border: 1px solid var(--border); background: var(--surface); color: var(--accent); border-color: var(--accent); }
  .sdr-livedot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); flex: 0 0 auto; }
  .sdr-livetag { font-size: var(--fs-micro); letter-spacing: 0.1em; color: var(--accent); flex: 0 0 auto; }
  .sdr-cc { flex: 0 0 auto; margin-left: auto; font-size: var(--fs-micro); font-weight: 700; letter-spacing: 0.06em; padding: 7px 11px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface); color: var(--text-3); }
  .sdr-actions { display: flex; gap: 9px; margin-top: 16px; }
  .sdr-act { flex: 1; display: inline-flex; align-items: center; justify-content: center; font-size: var(--fs-secondary); font-weight: 600; border: 1px solid transparent; border-radius: var(--r-input); padding: 13px 8px; min-height: 44px; }
  .sdr-act-ghost { color: var(--text-2); background: var(--surface-2); border-color: var(--border); opacity: 0.5; }
  .sdr-act-release { color: var(--steel); background: var(--steel-tint); }

  /* --- the new part ------------------------------------------------------------- */
  .tv-label { display: flex; align-items: baseline; justify-content: space-between; }
  .tv-span { letter-spacing: 0; text-transform: none; font-weight: 400; color: var(--text-3); font-variant-numeric: tabular-nums; }
  .tv-chart { position: relative; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; line-height: 0; }
  .tv-chart svg { display: block; width: 100%; height: 78px; }
  .tv-axis { display: flex; justify-content: space-between; margin-top: 4px; padding: 0 2px; font-size: var(--fs-micro); font-variant-numeric: tabular-nums; color: var(--text-3); }
  .tv-status { display: flex; align-items: center; gap: 6px; margin: 6px 2px 0; font-size: var(--fs-micro); line-height: 1.4; color: var(--text-2); font-variant-numeric: tabular-nums; }
  .tv-lvl { position: absolute; top: 6px; right: 9px; z-index: 1; line-height: 1; font-family: var(--font-mono); font-size: var(--fs-micro); color: var(--text-3); font-variant-numeric: tabular-nums; }
  .tv-nudge { margin-left: auto; flex: none; white-space: nowrap; border: 1px solid var(--amber); border-radius: var(--r-pill); background: var(--amber-tint); color: var(--amber); font-size: var(--fs-micro); padding: 3px 9px; }
  .tv-off { background: var(--surface-2); border: 1px dashed var(--border); border-radius: 8px; padding: 14px; text-align: center; }
  .tv-off p { margin: 0 0 10px; font-size: var(--fs-micro); color: var(--text-2); line-height: 1.55; }
  .tv-off b { color: var(--text); font-weight: 600; }
  .tv-hand { display: inline-flex; align-items: center; justify-content: center; min-height: 34px; padding: 8px 14px; border: 1px solid var(--border); border-radius: var(--r-input); background: var(--surface); color: var(--accent); font-size: var(--fs-micro); font-weight: 600; }
</style>
</head>
<body>
`;

/** The tape, drawn once — a stand-in waveform so the face reads as the instrument it
 *  is instead of an empty box. */
function tape(seed) {
  const rand = rng(seed);
  const bars = [];
  for (let i = 0; i < 90; i += 1) {
    const env = 0.25 + 0.75 * Math.abs(Math.sin(i / 7.5)) * (0.5 + rand() * 0.5);
    const h = (86 * env * 0.62).toFixed(1);
    bars.push(
      `<rect x="${(i * 4 + 1).toFixed(1)}" y="${((88 - Number(h)) / 2).toFixed(1)}" width="2" height="${h}" rx="1" fill="var(--steel)" opacity="${(0.35 + env * 0.4).toFixed(2)}"/>`,
    );
  }
  return `<svg class="sdr-tape" viewBox="0 0 362 88" preserveAspectRatio="none" aria-hidden="true">${bars.join("")}</svg>`;
}

function shell({ title, freq, mode, modes, elapsed, inner, note }) {
  return `${HEAD(title)}<div class="ab">
  <div class="body">
    <div class="rdetail-top">
      <button class="radio-back" aria-label="Back">&lsaquo;</button>
      <h2 class="rdetail-title">NooElec NESDR SMArt v5</h2>
    </div>
    <p class="rdesc">Telescoping antenna, desk</p>
    <div class="rstate"><span class="dot on"></span><span class="rser">09022796</span><span class="rused">Listening</span></div>
    <fieldset class="jobs">
      <legend class="lbl">Doing</legend>
      <button aria-pressed="true">Listen</button>
      <button disabled>APRS</button>
      <button disabled>Spectrum</button>
      <button>Idle</button>
    </fieldset>

    <div class="jobsurface">
      <p class="sdr-note">${note}</p>

      <div class="sdr-readout">
        <span class="sdr-freq">${freq}<span class="sdr-unit">MHz</span></span>
        <div class="sdr-station">${mode.toUpperCase()}</div>
        <div class="sdr-tuner">
          <button class="sdr-step" aria-label="Tune down">&minus;</button>
          <button class="sdr-stepsize">${modes.step}</button>
          <button class="sdr-step" aria-label="Tune up">+</button>
        </div>
      </div>

      <p class="sdr-label">Mode</p>
      <div class="seg-row">
        ${["wbfm", "fm", "am", "usb"]
          .map(
            (m) =>
              `<button class="seg${m === mode ? " seg-on" : ""}">${m.toUpperCase()}</button>`,
          )
          .join("\n        ")}
      </div>

${inner}

      <div class="sdr-face">
        ${tape(7)}
        <span class="sdr-face-elapsed">${elapsed}</span>
      </div>
      <div class="sdr-transport">
        <button class="sdr-play" aria-label="Pause"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg></button>
        <span class="sdr-livedot"></span>
        <span class="sdr-livetag">LIVE</span>
        <button class="sdr-cc">CC</button>
      </div>

      <div class="sdr-actions">
        <button class="sdr-act sdr-act-ghost">Record</button>
        <button class="sdr-act sdr-act-release">Release</button>
      </div>
    </div>
  </div>
</div>
</body>
</html>
`;
}

// --- 1. narrowband FM, on centre ---------------------------------------------------
const NFM_PASS = 16_000;
const NFM_SPAN = NFM_PASS * 2;
writeFileSync(
  `${OUT}/Main.dc.html`,
  shell({
    title: "Listen — the tuning view, signal centred",
    freq: "146.940",
    mode: "fm",
    modes: { step: "25 kHz" },
    elapsed: "4:12",
    note: "This session holds its radio until you release it.",
    inner: strip({
      passHz: NFM_PASS,
      spanHz: NFM_SPAN,
      db: trace(NFM_SPAN, -78, [signal(0, -34, 14_000)], 11),
      peakHz: 0,
      level: "−34.1 dBFS",
      status: "On centre",
      ok: true,
      unit: "kHz",
      ticks: ["−16", "−8", "0", "+8", "+16"],
    }),
  }),
);

// --- 2. narrowband FM, off tune ----------------------------------------------------
writeFileSync(
  `${OUT}/Offtune.dc.html`,
  shell({
    title: "Listen — the tuning view, signal off centre",
    freq: "146.940",
    mode: "fm",
    modes: { step: "25 kHz" },
    elapsed: "0:36",
    note: "This session holds its radio until you release it.",
    inner: strip({
      passHz: NFM_PASS,
      spanHz: NFM_SPAN,
      db: trace(NFM_SPAN, -78, [signal(6_200, -37, 14_000)], 23),
      peakHz: 6_200,
      level: "−37.4 dBFS",
      status: '6.2 kHz high — a third of it is outside the passband<button class="tv-nudge">Centre it</button>',
      ok: false,
      unit: "kHz",
      ticks: ["−16", "−8", "0", "+8", "+16"],
    }),
  }),
);

// --- 3. broadcast FM ---------------------------------------------------------------
const WFM_PASS = 192_000;
const WFM_SPAN = WFM_PASS * 2;
writeFileSync(
  `${OUT}/Wide.dc.html`,
  shell({
    title: "Listen — the tuning view on the broadcast dial",
    freq: "96.500",
    mode: "wbfm",
    modes: { step: "100 kHz" },
    elapsed: "12:48",
    note: "This session holds its radio until you release it.",
    inner: strip({
      passHz: WFM_PASS,
      spanHz: WFM_SPAN,
      db: trace(
        WFM_SPAN,
        -74,
        [signal(0, -19, 172_000), signal(-200_000, -33, 168_000), signal(200_000, -44, 168_000)],
        41,
      ),
      peakHz: 0,
      level: "−19.2 dBFS",
      status: "On centre · the neighbours 200 kHz out reach the edges",
      ok: true,
      unit: "kHz",
      ticks: ["−192", "−96", "0", "+96", "+192"],
    }),
  }),
);

// --- 4. what the box can actually do today -----------------------------------------
writeFileSync(
  `${OUT}/Unavailable.dc.html`,
  shell({
    title: "Listen — the tuning view before the demodulator lands",
    freq: "146.940",
    mode: "fm",
    modes: { step: "25 kHz" },
    elapsed: "4:12",
    note: "This session holds its radio until you release it.",
    inner: `      <p class="sdr-label tv-label">Tuning<span class="tv-span">32 kHz view · 16 kHz passband</span></p>
      <div class="tv-off">
        <p>
          Until the demodulator is wired in, the picture needs the other radio:
          listening runs <b>rtl_fm</b>, which holds this dongle to itself.
          <b>77192819</b> is free.
        </p>
        <button class="tv-hand">Draw it on 77192819</button>
      </div>`,
  }),
);

console.log("wrote 4 artboards to", OUT);
