// The phone, as the Dave simulator's headless Chromium plays it (growbot/sim/README.md).
// Runs before any page script. The camera is a canvas the harness paints the sim's rendered
// frames into, the microphone is silence (speech is handed to the page as heard text), and
// the motion sensors are events the harness fires from the sim's IMU each control tick.
(() => {
  "use strict";
  const W = 360, H = 480;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const g = canvas.getContext("2d");
  g.fillStyle = "#808080";
  g.fillRect(0, 0, W, H);
  let stream = null;
  let painting = Promise.resolve();

  window.__simFrame = (b64) => {
    painting = painting.then(() => {
      const img = new Image();
      img.src = "data:image/jpeg;base64," + b64;
      return img.decode().then(() => g.drawImage(img, 0, 0, W, H)).catch(() => {});
    });
    return painting;
  };

  function videoTrack() {
    if (!stream) stream = canvas.captureStream(30);
    const t = stream.getVideoTracks()[0].clone();
    const settings = t.getSettings.bind(t);
    // The robot's phone's front camera: portrait, facing the user (camera test run #223).
    t.getSettings = () => Object.assign(settings(), { facingMode: "user", width: W, height: H, frameRate: 30 });
    t.getCapabilities = () => ({ facingMode: ["user"], width: { min: 1, max: W }, height: { min: 1, max: H } });
    t.applyConstraints = () => Promise.resolve();
    return t;
  }

  let audio = null;
  function audioTrack() {
    if (!audio) {
      const ac = new AudioContext();
      const out = ac.createMediaStreamDestination();
      const src = ac.createConstantSource();
      src.offset.value = 0;
      src.connect(out);
      src.start();
      audio = out.stream;
    }
    return audio.getAudioTracks()[0].clone();
  }

  const md = navigator.mediaDevices || {};
  md.getUserMedia = (c) => {
    const tracks = [];
    if (c && c.video) tracks.push(videoTrack());
    if (c && c.audio) tracks.push(audioTrack());
    return Promise.resolve(new MediaStream(tracks));
  };
  md.enumerateDevices = () => Promise.resolve([
    { kind: "videoinput", deviceId: "front", label: "front camera", groupId: "phone" },
    { kind: "audioinput", deviceId: "mic", label: "microphone", groupId: "phone" },
  ]);

  // An Android phone: motion needs no permission prompt (iOS's requestPermission is absent).
  for (const E of [window.DeviceMotionEvent, window.DeviceOrientationEvent]) {
    try { if (E) Object.defineProperty(E, "requestPermission", { value: undefined, configurable: true }); } catch (e) {}
  }

  window.__simMotion = (m) => {
    const o = m.orientation;
    window.dispatchEvent(new DeviceOrientationEvent("deviceorientation", {
      alpha: o.alpha, beta: o.beta, gamma: o.gamma, absolute: false,
    }));
    window.dispatchEvent(new DeviceMotionEvent("devicemotion", {
      accelerationIncludingGravity: m.accel,
      acceleration: m.linear,
      rotationRate: m.rate,
      interval: 40,
    }));
  };

  // What the creature does, for the run's timeline: its own event log, as it happens.
  window.__simEvents = [];
  const keep = (kind, data) => {
    try { window.__simEvents.push({ t: Date.now(), kind: kind, data: JSON.parse(JSON.stringify(data || {})) }); }
    catch (e) { window.__simEvents.push({ t: Date.now(), kind: kind, data: String(data) }); }
  };
  window.__simKeep = keep;
  // logEv is the page's own telemetry; wrap it once it exists, whenever that is.
  const hook = setInterval(() => {
    if (typeof window.logEv === "function" && !window.logEv.__sim) {
      const real = window.logEv;
      const wrapped = function (kind, data) { keep(kind, data); return real.apply(this, arguments); };
      wrapped.__sim = true;
      window.logEv = wrapped;
      clearInterval(hook);
    }
  }, 50);
})();
