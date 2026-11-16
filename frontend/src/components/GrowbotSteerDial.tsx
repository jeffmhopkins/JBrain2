import { type KeyboardEvent, type PointerEvent, useId, useRef } from "react";

// Steer live's dial (owner, 2026-11-16: "Steer live is a bit hard to control. Maybe a kind of
// half circle graphics version that show origin and has a draggable vector?", then "can't we
// steer beyond 180°"). The robot sits at the centre; the arrow's angle off straight up is the
// heading and its length is how fast. The box takes any heading from 180° left to 180° right of
// where the drive began (serve.py's steer bounds, wrapped in growbot-drive.js), so the dial is
// a whole circle: pointing behind turns it round. Two sliders made the person read two numbers
// to know where it was going; one arrow shows it. Arrow keys work it too: left and right turn
// it 5°, up and down change the length a tenth, Home points it straight on.

const SIZE = 240;
const R = 104;
const O = SIZE / 2;

export interface SteerAim {
  /** Degrees off straight on, right positive, -175 to 180. */
  angle: number;
  /** 0 to 1. */
  length: number;
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

/** Into -180..180, with straight behind named 180. */
export function wrap180(deg: number): number {
  const d = ((((deg + 180) % 360) + 360) % 360) - 180;
  return d === -180 ? 180 : d;
}

function tidy(a: SteerAim): SteerAim {
  return {
    angle: wrap180(Math.round(a.angle / 5) * 5),
    length: clamp(Math.round(a.length * 10) / 10, 0, 1),
  };
}

function at(angle: number, r: number): { x: number; y: number } {
  const rad = (angle * Math.PI) / 180;
  return { x: O + r * Math.sin(rad), y: O - r * Math.cos(rad) };
}

export function GrowbotSteerDial({
  aim,
  onAim,
  lengthOn,
  label,
  valueText,
}: {
  aim: SteerAim;
  onAim: (a: SteerAim) => void;
  /** False for a walk that takes no speed: the arrow is then all direction, always full. */
  lengthOn: boolean;
  label: string;
  valueText: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  // The finger steering it; a second one (a palm, a thumb) is ignored until this one lifts.
  const held = useRef<number | null>(null);

  function from(e: PointerEvent<HTMLDivElement>) {
    const r = box.current?.getBoundingClientRect();
    if (!r || r.width === 0) return;
    const x = ((e.clientX - r.left) / r.width) * SIZE - O;
    const y = O - ((e.clientY - r.top) / r.height) * SIZE;
    const angle = (Math.atan2(x, y) * 180) / Math.PI;
    const length = lengthOn ? Math.hypot(x, y) / R : 1;
    onAim(tidy({ angle, length }));
  }

  function key(e: KeyboardEvent<HTMLDivElement>) {
    const step: Record<string, SteerAim> = {
      ArrowLeft: { angle: aim.angle - 5, length: aim.length },
      ArrowRight: { angle: aim.angle + 5, length: aim.length },
      ArrowUp: { angle: aim.angle, length: aim.length + 0.1 },
      ArrowDown: { angle: aim.angle, length: aim.length - 0.1 },
      Home: { angle: 0, length: aim.length },
    };
    const next = step[e.key];
    if (!next) return;
    e.preventDefault();
    onAim(tidy(lengthOn ? next : { ...next, length: 1 }));
  }

  const tip = at(aim.angle, Math.max(lengthOn ? aim.length : 1, 0.04) * R);
  const hint = `gb-dial-hint-${useId()}`;
  const ticks = [0, 45, 90, 135, 180, -135, -90, -45];
  return (
    <div
      ref={box}
      className="gb-dial"
      role="slider"
      tabIndex={0}
      aria-label={label}
      aria-valuemin={-180}
      aria-valuemax={180}
      aria-valuenow={aim.angle}
      aria-valuetext={valueText}
      aria-describedby={hint}
      onKeyDown={key}
      onPointerDown={(e) => {
        if (held.current !== null) return;
        held.current = e.pointerId;
        e.currentTarget.setPointerCapture?.(e.pointerId);
        from(e);
      }}
      onPointerMove={(e) => {
        if (held.current === e.pointerId) from(e);
      }}
      onPointerUp={(e) => {
        if (held.current === e.pointerId) held.current = null;
      }}
      onPointerCancel={(e) => {
        if (held.current === e.pointerId) held.current = null;
      }}
    >
      <span id={hint} className="gb-sr-only">
        {lengthOn
          ? "Left and right turn it; up and down change the speed."
          : "Left and right turn it."}
      </span>
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} aria-hidden="true">
        <circle className="gb-dial-face" cx={O} cy={O} r={R} />
        {lengthOn && <circle className="gb-dial-ring" cx={O} cy={O} r={R / 2} />}
        {ticks.map((t) => {
          const a = at(t, R - (t === 0 ? 14 : 8));
          const b = at(t, R);
          return (
            <line
              key={t}
              className={`gb-dial-tick${t === 0 ? " ahead" : ""}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
            />
          );
        })}
        <line className="gb-dial-vec" x1={O} y1={O} x2={tip.x} y2={tip.y} />
        <circle className="gb-dial-origin" cx={O} cy={O} r={6} />
        <circle className="gb-dial-tip" cx={tip.x} cy={tip.y} r={13} />
      </svg>
    </div>
  );
}
