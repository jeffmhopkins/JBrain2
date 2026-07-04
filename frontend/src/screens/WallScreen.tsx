// The JPet Wall (docs/plans/JPET_PLAN.md W2) — the full-screen 3D room on a mounted
// display. It renders the server-authoritative pet: subscribes to /api/pet/stream and
// paints each state into the WebGL scene (isolated in ./petScene so this stays
// testable), and turns local input (click-to-walk, poke) + the care buttons into
// /api/pet/command POSTs. Every surface watching the same pet stays in sync.

import { useCallback, useEffect, useRef, useState } from "react";
import { type PetCommand, type PetState, api } from "../api/client";
import { type PetScene, createPetScene } from "./petScene";
import { speak } from "./speech";
import "./wall.css";

export interface WallDeps {
  getPet: () => Promise<PetState>;
  sendPetCommand: (command: PetCommand) => Promise<PetState>;
  petStream: (signal?: AbortSignal) => AsyncGenerator<PetState>;
}

const defaultDeps: WallDeps = {
  getPet: () => api.getPet(),
  sendPetCommand: (c) => api.sendPetCommand(c),
  petStream: (s) => api.petStream(s),
};

const DRIVES: { key: keyof PetState; label: string }[] = [
  { key: "food", label: "Food" },
  { key: "energy", label: "Energy" },
  { key: "fun", label: "Fun" },
  { key: "love", label: "Love" },
];

function fillColor(v: number): string {
  return v > 55 ? "#3bf0ff" : v > 28 ? "#ffb03a" : "#ff477e";
}

interface WallScreenProps {
  onClose: () => void;
  deps?: WallDeps;
}

export function WallScreen({ onClose, deps = defaultDeps }: WallScreenProps) {
  const glRef = useRef<HTMLCanvasElement>(null);
  const bloomRef = useRef<HTMLCanvasElement>(null);
  const sceneRef = useRef<PetScene | null>(null);
  const [pet, setPet] = useState<PetState | null>(null);
  // Voice is off by default — a talking wall can startle; the owner turns it on.
  const [sound, setSound] = useState(false);
  const spokenRef = useRef<string | null>(null);

  // Speak each new utterance aloud when sound is on (W6). Guarded to fire once per
  // distinct line so a re-render doesn't repeat it.
  useEffect(() => {
    if (!sound) return;
    const line = pet?.speech ?? null;
    if (line && line !== spokenRef.current) speak(line);
    spokenRef.current = line;
  }, [pet?.speech, sound]);

  // Apply a command and reflect the returned authoritative state immediately (the
  // stream will also carry it to every other surface). Failures are swallowed — a
  // dropped tap is harmless.
  const send = useCallback(
    async (command: PetCommand): Promise<void> => {
      try {
        const next = await deps.sendPetCommand(command);
        setPet(next);
        sceneRef.current?.update(next);
      } catch {
        // ignore — the next stream frame reconciles state
      }
    },
    [deps],
  );

  // Build the scene once, wiring pointer input (poke / click-to-walk) back to commands.
  useEffect(() => {
    const gl = glRef.current;
    const bloom = bloomRef.current;
    if (!gl || !bloom) return;
    const scene = createPetScene(gl, bloom, {
      onPoke: () => void send({ action: "poke" }),
      onFloor: (x, z) => void send({ action: "move", x, z }),
    });
    sceneRef.current = scene;
    return () => {
      scene.destroy();
      sceneRef.current = null;
    };
  }, [send]);

  // Subscribe to the live stream: initial snapshot then every change.
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        setPet(await deps.getPet());
      } catch {
        // stream still delivers the snapshot
      }
      try {
        for await (const state of deps.petStream(controller.signal)) {
          setPet(state);
          sceneRef.current?.update(state);
        }
      } catch {
        // aborted on unmount or the connection dropped
      }
    })();
    return () => controller.abort();
  }, [deps]);

  const mood = pet?.mood ?? "…";
  return (
    <div className="wall">
      <canvas ref={glRef} className="wall-gl" />
      <canvas ref={bloomRef} className="wall-bloom" />
      <div className="wall-frame" />
      <div className="wall-hud">
        <div className="wall-name">{(pet?.name ?? "JPet").toUpperCase()}</div>
        <div className="wall-mood">{mood}</div>
      </div>
      <button
        type="button"
        className="wall-sound"
        onClick={() => setSound((s) => !s)}
        aria-label={sound ? "Mute voice" : "Enable voice"}
        aria-pressed={sound}
      >
        {sound ? "🔊" : "🔇"}
      </button>
      <button type="button" className="wall-close" onClick={onClose} aria-label="Close wall">
        ✕
      </button>
      {pet?.speech ? <div className="wall-speech">{pet.speech}</div> : null}
      <div className="wall-bars">
        {DRIVES.map(({ key, label }) => {
          const v = Math.round((pet?.[key] as number) ?? 0);
          return (
            <div key={key} className="wall-bar">
              <span>
                <span>{label}</span>
                <span>{v}</span>
              </span>
              <div className="wall-track">
                <div className="wall-fill" style={{ width: `${v}%`, background: fillColor(v) }} />
              </div>
            </div>
          );
        })}
      </div>
      <div className="wall-hint">Click the floor → it walks there. Click the robot → poke.</div>
      <div className="wall-controls">
        <button type="button" onClick={() => void send({ action: "feed" })}>
          🍔 Feed
        </button>
        <button type="button" onClick={() => void send({ action: "play" })}>
          🎮 Play
        </button>
        <button type="button" onClick={() => void send({ action: "pet" })}>
          ✋ Pet
        </button>
        <button type="button" onClick={() => void send({ action: "sleep" })}>
          💤 Sleep
        </button>
      </div>
    </div>
  );
}
