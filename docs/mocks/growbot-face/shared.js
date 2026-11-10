// Mock state and controls shared by the three face variants, so they differ only in drawing.
//
// The behaviour is lifted from today's face (`growbot/site/v3/next.html:drawFace`): the seven
// emotions the brain sets (EMO / GLOW), the mood sky's hue (gloomy indigo to joyful green), the
// blink every 2.3-5.7 s, the 1.6 s glance at the last touch, the 8-step pupil orbit while a
// think is in flight, the narrow searching squint while looking, the half-lidded nap, the dream
// veil, and the shake that scatters the stars. Nothing here talks to a network.
(function () {
  "use strict";
  const F = window.GROWBOT_FACES;
  const { LW, LH } = F;

  const GLOW = {
    calm: "#5ee6ff",
    curious: "#5ee6ff",
    happy: "#74ffb4",
    alert: "#ffc24d",
    scared: "#8fb4ff",
    sleepy: "#9a86ff",
    tired: "#3d5a66",
  };
  const EMO = {
    calm: { eye: 0.85, smile: 0.45 },
    curious: { eye: 1.15, smile: 0.2 },
    happy: { eye: 0.6, smile: 1 },
    alert: { eye: 1.25, smile: -0.1 },
    scared: { eye: 1.3, smile: -0.6 },
    sleepy: { eye: 0.25, smile: 0.15 },
    tired: { eye: 0.3, smile: 0.05 },
  };
  // GrowBot's seven emotions mapped onto the panels' Cozmo-style lid geometry
  // (`frontend/src/pet/face.ts:SPECS`). `r: null` mirrors the left eye.
  const e = (uy, ua, ly, lb, sx, sy) => ({ uy, ua, ly, lb, sx, sy });
  const LIDS = {
    calm: { l: e(0.12, 0, 0.14, 0.18, 1, 1), r: null, tilt: 0, mouth: "small", crest: 0 },
    curious: { l: e(0.3, -10, 0.2, 0.2, 1, 1), r: e(0.05, 12, 0, 0, 1.08, 1.08), tilt: -7, mouth: "small", crest: 0.5 },
    happy: { l: e(0, 0, 0.4, 0.4, 1, 1), r: null, tilt: 0, mouth: "smile", crest: 0.6 },
    alert: { l: e(0, 0, 0, 0, 1.1, 1.15), r: null, tilt: 0, mouth: "o", crest: 1 },
    scared: { l: e(0, 26, 0.35, 0.1, 1.2, 1.3), r: null, tilt: 0, mouth: "o", crest: 1 },
    sleepy: { l: e(0.48, 4, 0.45, 0, 1, 0.9), r: null, tilt: 3, mouth: "flat", crest: -0.4 },
    tired: { l: e(0.55, -8, 0.3, 0, 0.95, 0.85), r: null, tilt: 5, mouth: "flat", crest: -0.6 },
  };
  const LINES = [
    "Oh! Is that a new plant on your desk?",
    "I think I can see a cat. Is that a cat?",
    "Hmm, let me think about that for a second.",
    "That was fun. Can we do it again?",
  ];
  const COLORS = {
    steel: "#7FA7C9",
    cyan: "#3bf0ff",
    gold: "#ffd23f",
    green: "#49f08a",
    pink: "#ff8ad0",
    purple: "#b06aff",
  };

  const S = {
    face: document.body.dataset.face || "ostrich",
    emotion: "curious",
    color: "steel",
    speaking: false,
    thinking: false,
    looking: false,
    dreaming: false,
    napping: false,
    listening: true,
    mood: 0.2,
    energy: 0.45,
  };
  const lids = mirror(LIDS.curious);
  const P = { eye: 0.85, smile: 0.45, crest: 0, tilt: 0, talk: 0, startle: 0, blush: 0 };
  let nextBlink = 1500;
  let blinkUntil = 0;
  let touch = null;
  let shakeT = -1e9;
  let shakeCur = 0;
  let speakUntil = 0;
  let line = "";
  let skyHue = 200;
  let starHue = 200;
  const stars = [];
  for (let i = 0; i < 70; i++) {
    stars.push({ i, x: Math.random() * LW, y: Math.random() * LH * 0.8, z: Math.random() < 0.25 ? 6 : 3 });
  }
  let sparks = [];

  function mirror(spec) {
    const r = spec.r ? { ...spec.r } : { ...spec.l, ua: -spec.l.ua };
    return { l: { ...spec.l }, r };
  }

  function burst(x, y, n, hue) {
    for (let i = 0; i < n; i++) {
      sparks.push({
        x,
        y,
        vx: (Math.random() - 0.5) * 9,
        vy: (Math.random() - 0.5) * 9 - 2,
        life: 1,
        hue: hue ?? Math.round(40 + Math.random() * 300),
      });
    }
    sparks = sparks.slice(-200);
  }

  // ------------------------------------------------------------------ UI
  const app = document.getElementById("app");
  const tabs = document.body.dataset.tabs === "1";
  app.innerHTML = `
    <header class="top">
      <div>
        <p class="eyebrow">GrowBot · default face</p>
        <h1 id="title"></h1>
        <p class="lede" id="lede"></p>
      </div>
      ${tabs ? `<nav class="tabs" role="tablist" aria-label="Face variant">
        <button role="tab" data-face="ostrich">A · Ostrich</button>
        <button role="tab" data-face="robot">B · Robot</button>
        <button role="tab" data-face="lantern">C · Lantern</button>
      </nav>` : ""}
    </header>
    <main class="stage">
      <div class="phone"><canvas id="face" aria-label="GrowBot face preview"></canvas>
        <p class="say" id="say"></p></div>
      <section class="controls" aria-label="Mock controls">
        <p class="hint">Mock controls, not design. Tap the face to make it glance and spark.</p>
        <fieldset><legend>Emotion the brain sets</legend><div class="chips" id="emo"></div></fieldset>
        <fieldset><legend>What it is doing</legend><div class="chips" id="acts"></div></fieldset>
        <fieldset><legend>Inner weather</legend>
          <label class="slider">Mood <span>gloomy</span><input id="mood" type="range" min="-1" max="1" step="0.05"><span>joyful</span></label>
          <label class="slider">Energy <span>low</span><input id="energy" type="range" min="0" max="1" step="0.05"><span>buzzing</span></label>
        </fieldset>
        <fieldset id="colorSet"><legend>Body colour</legend><div class="chips" id="colors"></div></fieldset>
        <div class="chips"><button id="shake">Shake it</button><button id="say1">Say a line</button></div>
      </section>
    </main>
    <section class="notes" id="notes"></section>`;

  const NOTES = {
    ostrich: {
      title: "A · The panels' ostrich",
      lede: "The bird the twins picked for the JPanels, drawn at its own proportions and cropped to head and neck, since GrowBot already has real legs.",
      notes: [
        "Same eyes, lids and catchlight as the panel, so an emotion looks the same on a GrowBot and on a JPanel in the same room.",
        "The beak is the mouth: the lower three segments drop while it talks, the way the panel's do.",
        "The crest perks when it is alert or happy and droops when it is tired. The neck is the panels' stack of rings, short, standing on the bird's body with its wing and tail.",
        "Body colour is the panel palette, so a kid can make GrowBot match their panel. The mood still lives in the sky behind it.",
      ],
    },
    robot: {
      title: "B · The panels' robot",
      lede: "The JPanel robot head at full phone size, with its antenna, a ringed neck, and a crop of the chest plate at the bottom.",
      notes: [
        "Same eyes and lids as the panel. The mouth is the panel's smile, which opens out of the smile line while it talks.",
        "The antenna bulb lights in the mood colour while a think is in flight, and the chest light pulses while the ear is open.",
        "Reads most like a robot, which GrowBot is. It is also the busiest of the three.",
        "Body colour is the panel palette. The mood still lives in the sky behind it.",
      ],
    },
    lantern: {
      title: "C · Lantern eyes",
      lede: "No head: the whole phone is the face, as today, but redrawn at full resolution as two big glowing eyes in the mood's own colour.",
      notes: [
        "Every emotion is carried by eye shape: lids cut in from above, the cheek raise for happy, the squint for looking.",
        "A pupil appears only when it has a job: thinking (it orbits), looking (it searches) and glancing at a touch.",
        "The thin light under the eyes is its voice. It moves only while it speaks and curves gently with the mood at rest.",
        "Closest to today's face in spirit and the cheapest to draw. It does not tie GrowBot to the panels' look.",
      ],
    },
  };

  const $ = (id) => document.getElementById(id);
  const chip = (label, on, fn, title) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    if (title) b.title = title;
    b.addEventListener("click", fn);
    return b;
  };

  function renderControls() {
    const n = NOTES[S.face];
    $("title").textContent = n.title;
    $("lede").textContent = n.lede;
    $("notes").innerHTML = `<h2>What this one does</h2><ul>${n.notes.map((x) => `<li>${x}</li>`).join("")}</ul>`;
    document.querySelectorAll(".tabs button").forEach((b) => {
      b.setAttribute("aria-selected", b.dataset.face === S.face ? "true" : "false");
    });
    const emo = $("emo");
    emo.replaceChildren(
      ...Object.keys(EMO).map((k) =>
        chip(k, S.emotion === k, () => {
          S.emotion = k;
          if (k === "scared" || k === "alert") P.startle = 1;
          renderControls();
        }),
      ),
    );
    const acts = [
      ["speaking", "Speaking"],
      ["thinking", "Thinking"],
      ["looking", "Looking"],
      ["listening", "Ear open"],
      ["napping", "Napping"],
      ["dreaming", "Dreaming"],
    ];
    $("acts").replaceChildren(
      ...acts.map(([k, label]) =>
        chip(label, S[k], () => {
          S[k] = !S[k];
          if (k === "speaking") {
            if (S.speaking) sayLine();
            else speakUntil = 0;
          }
          renderControls();
        }),
      ),
    );
    $("colorSet").hidden = !F[S.face].tinted;
    $("colors").replaceChildren(
      ...Object.keys(COLORS).map((k) => {
        const b = chip(k, S.color === k, () => {
          S.color = k;
          renderControls();
        });
        b.style.setProperty("--sw", COLORS[k]);
        b.classList.add("swatch");
        return b;
      }),
    );
    $("mood").value = String(S.mood);
    $("energy").value = String(S.energy);
  }

  function sayLine() {
    line = LINES[Math.floor(Math.random() * LINES.length)];
    speakUntil = performance.now() + 900 + line.length * 70;
    S.speaking = true;
  }

  document.querySelectorAll(".tabs button").forEach((b) =>
    b.addEventListener("click", () => {
      S.face = b.dataset.face;
      try {
        localStorage.setItem("gbface", S.face);
      } catch (_) {}
      renderControls();
    }),
  );
  if (tabs) {
    try {
      const saved = localStorage.getItem("gbface");
      if (saved && F[saved]) S.face = saved;
    } catch (_) {}
  }
  $("mood").addEventListener("input", (ev) => (S.mood = Number(ev.target.value)));
  $("energy").addEventListener("input", (ev) => (S.energy = Number(ev.target.value)));
  $("shake").addEventListener("click", () => {
    shakeT = performance.now();
    burst(LW / 2, LH * 0.38, 40, null);
  });
  $("say1").addEventListener("click", () => {
    sayLine();
    renderControls();
  });

  const cv = $("face");
  const ctx = cv.getContext("2d");
  cv.addEventListener("pointerdown", (ev) => {
    const r = cv.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width;
    const y = (ev.clientY - r.top) / r.height;
    touch = { x, y, t: performance.now() };
    burst(x * LW, y * LH, 12, null);
    P.blush = 1;
  });

  function fit() {
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const r = cv.getBoundingClientRect();
    cv.width = Math.round(r.width * dpr);
    cv.height = Math.round(r.height * dpr);
  }
  new ResizeObserver(fit).observe(cv);

  // ------------------------------------------------------------------ the loop
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function frame(t) {
    requestAnimationFrame(frame);
    if (!cv.width) return;
    const tgt = EMO[S.emotion];
    const spec = mirror(LIDS[S.emotion]);
    P.eye += (tgt.eye - P.eye) * 0.1;
    P.smile += (tgt.smile + S.mood * 0.15 - P.smile) * 0.1;
    P.crest += (LIDS[S.emotion].crest - P.crest) * 0.08;
    P.tilt += (LIDS[S.emotion].tilt - P.tilt) * 0.08;
    P.startle *= 0.94;
    P.blush *= 0.97;
    for (const side of ["l", "r"]) {
      for (const k of Object.keys(spec[side])) lids[side][k] += (spec[side][k] - lids[side][k]) * 0.15;
    }
    if (S.speaking && speakUntil && t > speakUntil) {
      S.speaking = false;
      renderControls();
    }
    const talking = S.speaking && !S.dreaming;
    const syll = talking ? Math.max(0, Math.sin(t / 85) * 0.6 + Math.sin(t / 37) * 0.4) : 0;
    P.talk += (syll - P.talk) * 0.45;

    if (t > nextBlink) {
      blinkUntil = t + 130;
      nextBlink = t + 2300 + Math.random() * 3400;
    }
    const think = S.thinking && !talking;
    const looking = S.looking && !S.dreaming;
    // The panel eye already reads at 1.0; today's EMO.eye is a 0.25-1.3 openness, so the
    // emotion's lid shape does the work and openness only nudges it.
    let open = 0.75 + 0.25 * Math.min(1.3, P.eye) + Math.max(-0.15, Math.min(0.15, (S.energy - 0.4) * 0.3));
    if (S.napping) open = 0.35 + 0.08 * Math.sin(t / 900);
    if (S.dreaming || t < blinkUntil) open = 0.04;
    if (think && t >= blinkUntil) open = Math.max(open, 0.6);
    if (looking && t >= blinkUntil) open = 0.42 + 0.04 * Math.sin(t / 500);

    const orbit = (Math.floor(t / 150) % 8) * (Math.PI / 4);
    let pupil = { x: 0, y: 0 };
    let gaze = { x: 0, y: 0 };
    let glance = 0;
    if (touch) {
      const age = t - touch.t;
      glance = age < 1600 ? 1 - age / 1600 : 0;
      gaze = { x: (touch.x - 0.5) * 2 * glance, y: (touch.y - 0.4) * 2 * glance };
    }
    if (think) pupil = { x: Math.cos(orbit), y: Math.sin(orbit) * 0.8 };
    else if (looking) pupil = { x: Math.sin(t / 600), y: 0 };
    else pupil = { x: gaze.x, y: gaze.y };

    if (t - shakeT < 500) shakeCur += (1 - shakeCur) * 0.4;
    else shakeCur += (0 - shakeCur) * 0.055;

    const hue = Math.round(250 - ((S.mood + 1) / 2) * 110);
    skyHue += (hue - skyHue) * 0.05;
    starHue += (hue - starHue) * 0.08;
    for (const s of stars) {
      s.x += (0.1 + 0.8 * S.energy * (s.z > 3 ? 1.6 : 1)) * (1 + shakeCur * 3);
      if (s.x >= LW) s.x -= LW;
    }
    for (const p of sparks) {
      p.x += p.vx;
      p.y += p.vy;
      p.vy += 0.2;
      p.life -= 0.025;
    }
    sparks = sparks.filter((p) => p.life > 0 && p.y < LH + 10);

    const bob = (S.napping || S.dreaming ? 6 * Math.sin(t / 900) : 3 * Math.sin(t / 1300)) + Math.sin(t * 0.05) * shakeCur * 8;
    const d = {
      t,
      open,
      lids,
      pupil,
      gaze: { x: gaze.x + Math.sin(t * 0.045) * shakeCur * 0.6, y: gaze.y },
      showPupil: think || looking || glance > 0.05,
      think,
      looking,
      dreaming: S.dreaming,
      listening: S.listening && !S.dreaming,
      talking,
      talk: P.talk,
      smile: P.smile,
      mouth: LIDS[S.emotion].mouth,
      tilt: P.tilt,
      crest: P.crest,
      startle: P.startle,
      blush: P.blush,
      glow: GLOW[S.emotion],
      color: COLORS[S.color],
      skyHue: Math.round(skyHue),
      starHue: Math.round(starHue),
      energy: S.energy,
      shake: shakeCur,
      bob: reduce ? 0 : bob,
      stars,
      sparks,
    };
    ctx.setTransform(cv.width / LW, 0, 0, cv.height / LH, 0, 0);
    ctx.clearRect(0, 0, LW, LH);
    F[S.face].draw(ctx, d);
    const say = $("say");
    const txt = talking ? line : "";
    if (say.textContent !== txt) say.textContent = txt;
  }
  renderControls();
  requestAnimationFrame(frame);
})();
