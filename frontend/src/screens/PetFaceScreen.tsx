// The pet FACE — the room-endpoint display, running in the PWA ahead of the hardware
// (`docs/plans/PET_ENDPOINT_PWA_PLAN.md`; binding mock `docs/mocks/room-endpoint/pet-face.html`).
//
// Owner-only, and built for TROUBLESHOOTING: the panel itself is pixel-exact to the ordered
// Waveshare ESP32-S3-Touch-AMOLED-1.8 (368x448, physically 29.0 x 35.3 mm) and draws nothing the
// device could not, while everything around it is scaffolding that will not ship to the device.
//
// **It speaks only the APIs the hardware will speak** — `GET /api/pet`, `GET /api/pet/stream`,
// `POST /api/pet/command` — so what is validated here is the real contract, not a mock of it.
// Two consequences worth knowing before editing:
//   - The pet is SERVER-AUTHORITATIVE. This screen renders `script` and never invents pet state.
//     The only thing chosen locally is which *variant* of a reaction plays, which is presentation.
//   - `/api/pet/stream` frames DROP every ephemeral effect (creature form, scale, scene), so the
//     creature form can only arrive by polling `GET /api/pet`. That is a shipped defect, not a
//     design; the poll below exists solely because of it and should be deleted when it is fixed.
//
// Interaction is the settled model (`docs/reference/DESIGN.md`): ONE whole-screen target, ONE
// gesture, NO thresholds. A four-year-old's ordinary tap runs to 4.2 s, so his tap and his hold
// are the same gesture and no threshold can separate them. Contact means "I'm paying attention
// to you" — the pet flinches toward the finger immediately; release acts.

import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { type PetCommand, type PetState, api } from "../api/client";
import { COLOR_NAMES, FORMS, PANEL_H, PANEL_W, type Scene, drawScene } from "../pet/draw";
import { type FaceKey, approach, isFaceKey, resolveFace } from "../pet/face";
import { ACTIONS, figureFor, rigFor } from "../pet/rig";
import { type PoolMemory, newMemory, pickVariant, repetitionPenalty } from "../pet/variants";
import "./petface.css";

export interface PetFaceDeps {
  getPet: () => Promise<PetState>;
  sendPetCommand: (command: PetCommand) => Promise<PetState>;
  petStream: (signal?: AbortSignal) => AsyncGenerator<PetState>;
}

const defaultDeps: PetFaceDeps = {
  getPet: () => api.getPet(),
  sendPetCommand: (c) => api.sendPetCommand(c),
  petStream: (s) => api.petStream(s),
};

/** Effects-only poll. See the header: the SSE stream drops the creature form, so it cannot be
 *  the only subscription. The wall uses 1 Hz for the same reason. */
const EFFECT_POLL_MS = 1000;

/** One playing action. `mag` is the repetition penalty already applied. */
interface Playing {
  name: string;
  t: number;
  dur: number;
  mag: number;
}

interface PetFaceScreenProps {
  onClose: () => void;
  deps?: PetFaceDeps;
}

export function PetFaceScreen({ onClose, deps = defaultDeps }: PetFaceScreenProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [pet, setPet] = useState<PetState | null>(null);
  const [trueSize, setTrueSize] = useState(false);
  const [say, setSay] = useState("");
  const [log, setLog] = useState<string[]>([]);
  // Local overrides exist ONLY for design validation — they let the owner see a form or an
  // emotion the server is not currently in. null = follow the server, which is the real behaviour.
  const [formOverride, setFormOverride] = useState<string | null>(null);
  const [faceOverride, setFaceOverride] = useState<FaceKey | null>(null);

  // Animation state lives in a ref: it changes every frame and must never re-render React.
  const anim = useRef({
    t: 0,
    blink: 0,
    nextBlink: 1400,
    nextSaccade: 900,
    gaze: { x: 0, y: 0, tx: 0, ty: 0 },
    act: null as Playing | null,
    listening: false,
    caption: "",
    mem: newMemory() as PoolMemory,
    cur: { sx: 1, sy: 1, ang: 0 },
  });
  const petRef = useRef<PetState | null>(null);
  const overrides = useRef({ form: null as string | null, face: null as FaceKey | null });
  overrides.current = { form: formOverride, face: faceOverride };

  const note = useCallback((line: string) => {
    setLog((prev) =>
      [`${new Date().toLocaleTimeString([], { hour12: false })} ${line}`, ...prev].slice(0, 60),
    );
  }, []);

  /** Play a reaction locally RIGHT NOW, then tell the server. The immediate local play is not a
   *  shortcut: a reaction that waits for a round trip reads as the toy ignoring the child, and
   *  the sub-100 ms acknowledgement is what carries perceived aliveness. The server's script
   *  reconciles on the next frame. */
  const react = useCallback(
    (pool: string, command: PetCommand | null) => {
      const a = anim.current;
      const mag = repetitionPenalty(pool, a.mem, a.t);
      const variant = pickVariant(pool, a.mem, a.t);
      const spec = ACTIONS[variant] ?? ACTIONS.wiggle;
      a.act = { name: variant, t: 0, dur: spec?.dur ?? 1100, mag };
      a.caption = variant;
      note(`${pool} → ${variant} ×${mag.toFixed(2)}`);
      if (command) {
        deps.sendPetCommand(command).then(
          (s) => setPet(s),
          () => note("command failed — the stream will reconcile"),
        );
      }
    },
    [deps, note],
  );

  // ── the real API surface: snapshot, then the live stream, plus the effects poll ──────────
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setInterval> | undefined;
    (async () => {
      try {
        const first = await deps.getPet();
        setPet(first);
        petRef.current = first;
      } catch {
        // the stream still delivers a snapshot
      }
      timer = setInterval(() => {
        deps.getPet().then(
          (s) => {
            // Effects only. The stream owns everything else, so a slow poll cannot stutter
            // the animation by overwriting a fresher frame.
            const prev = petRef.current;
            if (!prev) return;
            if (s.pet_form !== prev.pet_form || s.color !== prev.color) {
              // `exactOptionalPropertyTypes` forbids writing an explicit undefined, and the
              // distinction is real here: "the server sent no form" must not overwrite a form
              // we already know with a hole.
              const merged: PetState =
                s.pet_form === undefined
                  ? { ...prev, color: s.color }
                  : { ...prev, pet_form: s.pet_form, color: s.color };
              petRef.current = merged;
              setPet(merged);
            }
          },
          () => undefined,
        );
      }, EFFECT_POLL_MS);
      try {
        for await (const state of deps.petStream(controller.signal)) {
          // Carry the last known form across stream frames, which never include it.
          const form = state.pet_form ?? petRef.current?.pet_form;
          const merged: PetState = form === undefined ? state : { ...state, pet_form: form };
          petRef.current = merged;
          setPet(merged);
        }
      } catch {
        // aborted on unmount, or the connection dropped
      }
    })();
    return () => {
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [deps]);

  // ── the frame loop ───────────────────────────────────────────────────────────────────────
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(64, now - last);
      last = now;
      const a = anim.current;
      a.t += dt;

      // Blink: ~167 ms per half, 1-5 s apart, and the eye widens as it closes. Without this
      // and the saccades below, the character reads as dead however good the pose is.
      a.nextBlink -= dt;
      if (a.nextBlink <= 0) {
        a.blink = 334;
        a.nextBlink = 1000 + Math.random() * 4000;
      }
      if (a.blink > 0) a.blink -= dt;
      a.nextSaccade -= dt;
      if (a.nextSaccade <= 0 && !a.listening) {
        a.gaze.tx = (Math.random() * 2 - 1) * 16;
        a.gaze.ty = (Math.random() * 2 - 1) * 10;
        a.nextSaccade = 600 + Math.random() * 2000;
      }
      a.gaze.x += (a.gaze.tx - a.gaze.x) * 0.28;
      a.gaze.y += (a.gaze.ty - a.gaze.y) * 0.28;
      if (a.act) {
        a.act.t += dt;
        if (a.act.t >= a.act.dur) {
          a.act = null;
          a.caption = "";
        }
      }

      const p = petRef.current;
      const playing = a.act;
      // Emotion precedence: a local override (validation only) → the action's own face → the
      // step the server is playing → the pet's top-level `emotion`. That last one is the field
      // the wall never reads, and reading it here is part of what this surface validates.
      const serverFace = p?.script?.[0]?.emotion ?? p?.emotion;
      const key: FaceKey =
        overrides.current.face ??
        (playing && isFaceKey(ACTIONS[playing.name]?.face)
          ? (ACTIONS[playing.name]?.face as FaceKey)
          : isFaceKey(serverFace)
            ? serverFace
            : "happy");
      const face = resolveFace(key);
      const prog = playing ? playing.t / playing.dur : 0;
      const mag = playing?.mag ?? 1;
      const rig = rigFor(playing?.name ?? null, prog, mag, a.t);
      const figure = figureFor(playing?.name ?? null, prog, mag, a.t, face.face.ang, rig.lean);
      // Tween the whole-figure values so an abrupt state change eases instead of snapping.
      a.cur.sx = approach(a.cur.sx, figure.sx, face.rate);
      a.cur.sy = approach(a.cur.sy, figure.sy, face.rate);
      a.cur.ang = approach(a.cur.ang, figure.ang, face.rate);

      // Guarded: a browser can legitimately refuse a 2D context (too many live contexts, a
      // lost GPU), and jsdom has none at all. A throw here would kill the whole frame loop and
      // take the screen with it, so a missing context just means "draw nothing this frame".
      let ctx: CanvasRenderingContext2D | null = null;
      try {
        ctx = canvasRef.current?.getContext("2d") ?? null;
      } catch {
        ctx = null;
      }
      if (ctx) {
        const scene: Scene = {
          face,
          rig,
          figure: { ...figure, sx: a.cur.sx, sy: a.cur.sy, ang: a.cur.ang },
          form: overrides.current.form ?? p?.pet_form ?? "robot",
          color: p?.color ?? null,
          blink: a.blink > 0,
          gaze: a.gaze,
          t: a.t,
          listening: a.listening,
          caption: a.caption || undefined,
        };
        drawScene(ctx, scene);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  // ── touch: one target, one gesture, no thresholds ────────────────────────────────────────
  const onDown = useCallback((e: ReactPointerEvent<HTMLCanvasElement>) => {
    const el = e.currentTarget;
    const r = el.getBoundingClientRect();
    const a = anim.current;
    // Stage one, immediately: flinch toward the finger and open the mic. This is the whole
    // press-to-talk model — hold to speak, and holding costs nothing because the whole screen
    // was already the only target a four-year-old can reliably hit.
    a.gaze.tx = Math.max(
      -18,
      Math.min(18, ((e.clientX - r.left) * (PANEL_W / r.width) - PANEL_W / 2) / 6),
    );
    a.gaze.ty = Math.max(
      -14,
      Math.min(14, ((e.clientY - r.top) * (PANEL_H / r.height) - PANEL_H * 0.46) / 8),
    );
    a.listening = true;
    // Capture keeps the release ours even if the finger slides off the panel — a four-year-old
    // does not lift cleanly. It can throw on an already-released pointer, which must not cost
    // us the interaction.
    try {
      el.setPointerCapture(e.pointerId);
    } catch {
      // no capture available; the pointerup still lands on the element
    }
  }, []);

  const onUp = useCallback(() => {
    const a = anim.current;
    if (!a.listening) return;
    a.listening = false;
    // Stage two: act on what was said, or be silly if nothing was. `wiggle` is the canonical
    // server action for a poke; the variant that actually plays is chosen locally.
    react("poke", { action: "wiggle" });
  }, [react]);

  const sendSay = useCallback(() => {
    const text = say.trim();
    if (!text) return;
    setSay("");
    note(`say → "${text}" (server routes it: intents.py first, LLM only if nothing matches)`);
    deps.sendPetCommand({ action: "say", text }).then(
      (s) => setPet(s),
      () => note("say failed"),
    );
  }, [deps, note, say]);

  const quick = (action: PetCommand["action"], pool?: string) => {
    if (pool) react(pool, { action });
    else {
      const spec = ACTIONS[action];
      if (spec) anim.current.act = { name: action, t: 0, dur: spec.dur, mag: 1 };
      anim.current.caption = action;
      note(`${action} → POST /api/pet/command`);
      deps.sendPetCommand({ action }).then(
        (s) => setPet(s),
        () => note("command failed"),
      );
    }
  };

  return (
    <div className="pf-wrap">
      <div className="pf-bar">
        <button type="button" onClick={onClose}>
          Back
        </button>
        <h1>Pet face — endpoint preview</h1>
        <button type="button" aria-pressed={trueSize} onClick={() => setTrueSize((v) => !v)}>
          {trueSize ? "1:1 pixels" : "True size"}
        </button>
      </div>

      <div className="pf-body">
        <div>
          <div className={`pf-stage${trueSize ? " pf-true" : ""}`}>
            <canvas
              ref={canvasRef}
              width={PANEL_W}
              height={PANEL_H}
              aria-label="Pet face preview panel"
              onPointerDown={onDown}
              onPointerUp={onUp}
              onPointerCancel={onUp}
            />
          </div>
          <p className="pf-note">
            {trueSize
              ? "true physical size — 29.0 × 35.3 mm, 322 ppi"
              : "1:1 device pixels — 368 × 448; the real panel is 29 × 35 mm"}
          </p>
          <p className="pf-note">press and hold the panel · release to poke</p>
        </div>

        <div className="pf-panel">
          <div className="pf-card">
            <h2>Say to the pet</h2>
            <div className="pf-say">
              <input
                value={say}
                onChange={(e) => setSay(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") sendSay();
                }}
                placeholder='"turn red" · "be a dragon" · "tell me a joke"'
                aria-label="Say to the pet"
              />
              <button type="button" onClick={sendSay}>
                Say
              </button>
            </div>
            <div className="pf-row" style={{ marginTop: 10 }}>
              <button type="button" onClick={() => quick("dance", "dance")}>
                dance
              </button>
              <button type="button" onClick={() => quick("jump")}>
                jump
              </button>
              <button type="button" onClick={() => quick("wave")}>
                wave
              </button>
              <button type="button" onClick={() => quick("hide")}>
                peekaboo
              </button>
              <button type="button" onClick={() => quick("fart", "fart")}>
                fart
              </button>
              <button type="button" onClick={() => quick("burp")}>
                burp
              </button>
            </div>
          </div>

          <div className="pf-card">
            <h2>Face override — validation only</h2>
            <div className="pf-row">
              {(["happy", "excited", "curious", "sleepy", "silly", "scared"] as FaceKey[]).map(
                (k) => (
                  <button
                    key={k}
                    type="button"
                    aria-pressed={faceOverride === k}
                    onClick={() => setFaceOverride((v) => (v === k ? null : k))}
                  >
                    {k}
                  </button>
                ),
              )}
            </div>
            <p className="pf-note" style={{ textAlign: "left", marginTop: 8 }}>
              Off = follow the server. The wall never reads `emotion`; this does.
            </p>
          </div>

          <div className="pf-card">
            <h2>Form override — validation only</h2>
            <div className="pf-row">
              {FORMS.map((f) => (
                <button
                  key={f}
                  type="button"
                  aria-pressed={formOverride === f}
                  onClick={() => setFormOverride((v) => (v === f ? null : f))}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>

          <div className="pf-card">
            <h2>Server state</h2>
            <p className="pf-note" style={{ textAlign: "left" }}>
              {pet
                ? `${pet.name} · ${pet.mood} · emotion ${pet.emotion} · colour ${pet.color ?? "default"} · form ${pet.pet_form ?? "robot"} · ${pet.script.length} step(s)`
                : "connecting…"}
            </p>
            <p className="pf-note" style={{ textAlign: "left" }}>
              Colours: {COLOR_NAMES.join(" · ")}
            </p>
          </div>

          <div className="pf-card">
            <h2>Log</h2>
            <div className="pf-log">
              {log.map((line) => (
                <div key={line}>{line}</div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
