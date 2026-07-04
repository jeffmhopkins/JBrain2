// The JPet phone Control screen (docs/plans/JPET_PLAN.md W3) — the mobile "remote"
// the kids hold, paired to the Wall. It subscribes to /api/pet/stream for live status
// and issues /api/pet/command: care actions (feed/play/pet/sleep) and "send it here"
// (tap the room map → a move command → the Wall's robot walks there). Both surfaces
// render the same server-authoritative pet, so they stay in sync.
//
// (Talking to the pet arrives in W4 with the pet.turn brain; W3 is the care + move
// remote, all working against W1's command API.)

import { type PointerEvent, useCallback, useEffect, useState } from "react";
import { type PetCommand, type PetState, api } from "../api/client";
import "./control.css";

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

const DRIVES: { key: keyof PetState; label: string }[] = [
  { key: "food", label: "🍔 Food" },
  { key: "energy", label: "⚡ Energy" },
  { key: "fun", label: "🎮 Fun" },
  { key: "love", label: "💗 Love" },
];

const CARE: { action: PetCommand["action"]; ico: string; label: string }[] = [
  { action: "feed", ico: "🍔", label: "Feed" },
  { action: "play", ico: "🎮", label: "Play" },
  { action: "pet", ico: "✋", label: "Pet" },
  { action: "sleep", ico: "💤", label: "Sleep" },
];

function fillColor(v: number): string {
  return v > 55 ? "#3bf0ff" : v > 28 ? "#ffb03a" : "#ff477e";
}

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

  const send = useCallback(
    async (command: PetCommand): Promise<void> => {
      try {
        setPet(await deps.sendPetCommand(command));
      } catch {
        // ignore — the next stream frame reconciles state
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

  // Tap the room map → send the pet to that normalized floor point.
  const onMapDown = (e: PointerEvent<HTMLButtonElement>): void => {
    const r = e.currentTarget.getBoundingClientRect();
    const nx = r.width > 0 ? ((e.clientX - r.left) / r.width) * 2 - 1 : 0;
    const nz = r.height > 0 ? ((e.clientY - r.top) / r.height) * 2 - 1 : 0;
    const clamp = (n: number) => Math.max(-1, Math.min(1, n));
    void send({ action: "move", x: clamp(nx), z: clamp(nz) });
  };

  // Say something to the pet → the pet.turn brain answers (W4).
  const talk = (): void => {
    const t = text.trim();
    if (!t) return;
    setText("");
    void send({ action: "say", text: t });
  };

  const name = pet?.name ?? "JPet";
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

      <div className="pctl-card">
        <h3>Needs</h3>
        <div className="pctl-meters">
          {DRIVES.map(({ key, label }) => {
            const v = Math.round((pet?.[key] as number) ?? 0);
            return (
              <div key={key} className="pctl-meter">
                <span>
                  <span>{label}</span>
                  <b>{v}</b>
                </span>
                <div className="pctl-track">
                  <div className="pctl-fill" style={{ width: `${v}%`, background: fillColor(v) }} />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="pctl-card">
        <h3>Take care of {name}</h3>
        <div className="pctl-care">
          {CARE.map(({ action, ico, label }) => (
            <button key={action} type="button" onClick={() => void send({ action })}>
              <span className="ico">{ico}</span>
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="pctl-card">
        <h3>Send it somewhere</h3>
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
      </div>

      <div className="pctl-card">
        <h3>Talk to {name}</h3>
        {pet?.speech ? <div className="pctl-speech">💬 {pet.speech}</div> : null}
        <div className="pctl-talk">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") talk();
            }}
            placeholder={`Tell ${name} something…`}
            aria-label="Message to the pet"
          />
          <button type="button" onClick={talk} aria-label="Send message">
            ➤
          </button>
        </div>
      </div>
    </div>
  );
}
