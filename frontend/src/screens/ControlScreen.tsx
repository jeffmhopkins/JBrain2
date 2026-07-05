// The JPet phone Control screen (docs/proposed/JPET_V2_PLAN.md) — the mobile "remote"
// the kids hold, paired to the Wall. It subscribes to /api/pet/stream for live status
// and issues /api/pet/command. v2 is command-and-response PLAY: big, few, non-destructive
// buttons a 3–4-year-old can hit (dance, chase the ball, hide, jump, wave, spin, silly
// sound, sleep/wake), each firing on touch-DOWN for instant feedback, plus a push-to-talk
// mic and text box so a kid can just ask ("pick up the ball and put it in the corner").
// The room-map "send it somewhere" is demoted to a grown-ups affordance. Both surfaces
// render the same server-authoritative pet, so they stay in sync.

import { type PointerEvent, useCallback, useEffect, useState } from "react";
import { type PetCommand, type PetState, api } from "../api/client";
import "./control.css";
import { listenOnce, sttAvailable } from "./speech";

export interface ControlDeps {
  getPet: () => Promise<PetState>;
  sendPetCommand: (command: PetCommand) => Promise<PetState>;
  petStream: (signal?: AbortSignal) => AsyncGenerator<PetState>;
}

const defaultDeps: ControlDeps = {
  getPet: () => api.getPet(),
  sendPetCommand: (c) => api.sendPetCommand(c),
  petStream: (s) => api.petStream(s),
};

// The big kid play-buttons — each is a one-tap canned script. Few, large, and never
// destructive; `sleep`/`wake` is swapped in contextually below.
const PLAY: { action: PetCommand["action"]; ico: string; label: string }[] = [
  { action: "dance", ico: "💃", label: "Dance" },
  { action: "chase", ico: "⚽", label: "Chase ball" },
  { action: "hide", ico: "🙈", label: "Hide" },
  { action: "jump", ico: "⭐", label: "Jump!" },
  { action: "wave", ico: "👋", label: "Wave hi" },
  { action: "spin", ico: "🌀", label: "Spin" },
  { action: "jumprope", ico: "🤸", label: "Jump rope" },
  { action: "music", ico: "🎹", label: "Play music" },
  { action: "sing", ico: "🎤", label: "Sing" },
  { action: "fart", ico: "💨", label: "Toot!" },
  { action: "burp", ico: "🫧", label: "Burp!" },
  { action: "beep", ico: "🔊", label: "Silly sound" },
];

// The colour swatches — each recolours the robot on both surfaces. Names mirror the
// backend `PET_COLORS`; the hex here is the swatch face only (not the neon wall palette).
const COLORS: { name: string; label: string; hex: string }[] = [
  // "default" restores the pet's original synthwave look (magenta + cyan).
  {
    name: "default",
    label: "original robot",
    hex: "linear-gradient(135deg,#ff4fd8 0 50%,#37e0f0 50% 100%)",
  },
  { name: "cyan", label: "cyan", hex: "#37e0f0" },
  { name: "magenta", label: "magenta", hex: "#ff4fd8" },
  { name: "gold", label: "gold", hex: "#ffce3a" },
  { name: "orange", label: "orange", hex: "#ff8a3a" },
  { name: "blue", label: "blue", hex: "#4a7bff" },
  { name: "red", label: "red", hex: "#ff4d5e" },
  { name: "green", label: "green", hex: "#49f08a" },
  { name: "pink", label: "pink", hex: "#ff8ad0" },
  { name: "purple", label: "purple", hex: "#b06aff" },
  { name: "white", label: "white", hex: "#f0f4ff" },
  {
    name: "rainbow",
    label: "rainbow",
    hex: "conic-gradient(red,orange,gold,green,blue,purple,red)",
  },
];

// Normalized [-1, 1] → CSS percent for the dot's position on the map.
function pct(n: number): string {
  return `${((n + 1) / 2) * 100}%`;
}

interface ControlScreenProps {
  onClose: () => void;
  deps?: ControlDeps;
}

export function ControlScreen({ onClose, deps = defaultDeps }: ControlScreenProps) {
  const [pet, setPet] = useState<PetState | null>(null);
  const [text, setText] = useState("");
  const [listening, setListening] = useState(false);
  const [thinking, setThinking] = useState(false);

  const send = useCallback(
    async (command: PetCommand): Promise<void> => {
      // Talking can hit the LLM, which takes a moment — show a "thinking" indicator so the
      // wait never feels dead. (Play buttons are instant, so they don't flip it.)
      const talk = command.action === "say";
      if (talk) setThinking(true);
      try {
        setPet(await deps.sendPetCommand(command));
      } catch {
        // ignore — the next stream frame reconciles state
      } finally {
        if (talk) setThinking(false);
      }
    },
    [deps],
  );

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        setPet(await deps.getPet());
      } catch {
        // the stream still delivers the snapshot
      }
      try {
        for await (const state of deps.petStream(controller.signal)) setPet(state);
      } catch {
        // aborted on unmount or the connection dropped
      }
    })();
    return () => controller.abort();
  }, [deps]);

  // Fire a play command on touch-DOWN (not lift): a 3–4-year-old taps hard and expects an
  // instant reaction; waiting for lift reads as "nothing happened". preventDefault stops
  // the synthetic click/scroll so a fast double-tap doesn't zoom the page.
  const onPlayDown = (action: PetCommand["action"]) => (e: PointerEvent<HTMLButtonElement>) => {
    e.preventDefault();
    void send({ action });
  };

  // Tap the room map → send the pet to that normalized floor point (a grown-up control).
  const onMapDown = (e: PointerEvent<HTMLButtonElement>): void => {
    const r = e.currentTarget.getBoundingClientRect();
    const nx = r.width > 0 ? ((e.clientX - r.left) / r.width) * 2 - 1 : 0;
    const nz = r.height > 0 ? ((e.clientY - r.top) / r.height) * 2 - 1 : 0;
    const clamp = (n: number) => Math.max(-1, Math.min(1, n));
    void send({ action: "move", x: clamp(nx), z: clamp(nz) });
  };

  // Say something to the pet → the pet.turn brain answers and acts it out as a script.
  const talk = (): void => {
    const t = text.trim();
    if (!t) return;
    setText("");
    void send({ action: "say", text: t });
  };

  // Talk to it out loud: capture one spoken phrase and say it to the pet.
  const listen = (): void => {
    if (listening) return;
    const handle = listenOnce(
      (spoken) => void send({ action: "say", text: spoken }),
      () => setListening(false),
    );
    if (handle) setListening(true);
  };

  const name = pet?.name ?? "JPet";
  const asleep = pet?.asleep ?? false;
  const sleepBtn = asleep
    ? { action: "wake" as const, ico: "☀️", label: "Wake up" }
    : { action: "sleep" as const, ico: "😴", label: "Sleep" };
  return (
    <div className="pctl">
      <div className="pctl-head">
        <div className="pctl-avatar">🤖</div>
        <div>
          <div className="pctl-name">{name}</div>
          <div className="pctl-sub">
            <span className="pctl-dot" /> paired to Wall · live
          </div>
        </div>
        <div className="pctl-mood">
          <b>{pet?.mood ?? "…"}</b>
        </div>
        <button type="button" className="pctl-close" onClick={onClose} aria-label="Close control">
          ✕
        </button>
      </div>

      {sttAvailable() ? (
        <button
          type="button"
          className={`pctl-michero${listening || thinking ? " on" : ""}`}
          onClick={listen}
          aria-label="Talk to the pet by voice"
        >
          <span className="pctl-michero-ico">{thinking ? "💭" : "🎤"}</span>
          <span className="pctl-michero-label">
            {thinking ? "Thinking…" : listening ? "Listening…" : `Talk to ${name}!`}
          </span>
        </button>
      ) : null}

      <div className="pctl-card">
        <h3>Let's play!</h3>
        {thinking ? (
          <div className="pctl-speech pctl-think">💭 thinking…</div>
        ) : pet?.speech ? (
          <div className="pctl-speech">💬 {pet.speech}</div>
        ) : null}
        <div className="pctl-play">
          {PLAY.map(({ action, ico, label }) => (
            <button
              key={action}
              type="button"
              className="pctl-playbtn"
              onPointerDown={onPlayDown(action)}
              aria-label={label}
            >
              <span className="ico">{ico}</span>
              {label}
            </button>
          ))}
          <button
            type="button"
            className="pctl-playbtn"
            onPointerDown={onPlayDown(sleepBtn.action)}
            aria-label={sleepBtn.label}
          >
            <span className="ico">{sleepBtn.ico}</span>
            {sleepBtn.label}
          </button>
        </div>
      </div>

      <div className="pctl-card">
        <h3>Change colour</h3>
        <div className="pctl-colors">
          {COLORS.map(({ name: cname, label, hex }) => (
            <button
              key={cname}
              type="button"
              className={`pctl-swatch${pet?.color === cname ? " on" : ""}`}
              style={{ background: hex }}
              onPointerDown={(e) => {
                e.preventDefault();
                void send({ action: "color", text: cname });
              }}
              aria-label={cname === "default" ? "Original robot colours" : `Turn ${label}`}
            />
          ))}
        </div>
      </div>

      <div className="pctl-card">
        <h3>Or type to {name}</h3>
        <div className="pctl-talk">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") talk();
            }}
            placeholder={`Ask ${name} to do something…`}
            aria-label="Message to the pet"
          />
          <button type="button" onClick={talk} aria-label="Send message">
            ➤
          </button>
        </div>
      </div>

      <details className="pctl-card pctl-grownup">
        <summary>Grown-ups: send it somewhere</summary>
        <div className="pctl-maprow">
          <button
            type="button"
            className="pctl-map"
            onPointerDown={onMapDown}
            aria-label="Room map — tap to send the pet there"
          >
            <div
              className="pctl-petdot"
              style={{ left: pct(pet?.pos_x ?? 0), top: pct(pet?.pos_z ?? 0) }}
            />
          </button>
          <div className="pctl-maphint">
            Tap the room to send <b>{name}</b> there. It walks over on the Wall.
          </div>
        </div>
      </details>
    </div>
  );
}
