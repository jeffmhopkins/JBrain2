/** The persona's sketch map of the house (growbot/patches/growbot-map.js), drawn from its soul
 *  (owner, 2026-11-17: "draw geometric shapes to approximate the geometry it has seen"). The
 *  creature writes it as it walks and looks: rooms are rectangles, doorways sit on their walls,
 *  things are points, and the arrow is where it thinks it is and which way its camera faces. It
 *  is a guess from dead reckoning, so the page says so; it changes it by talking to it, never
 *  here. */
import type { GrowbotSoul } from "../api/client";

interface Room {
  n: string;
  x: number;
  y: number;
  w: number;
  d: number;
}
interface Door {
  x: number;
  y: number;
  a: string;
  b: string;
}
interface Thing {
  n: string;
  x: number;
  y: number;
  room: string;
  t: number;
  rough: boolean;
}
export interface HouseMap {
  rooms: Room[];
  doors: Door[];
  things: Thing[];
  here: { x: number; y: number; h: number; room: string };
}

function finite(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}
function text(v: unknown): string {
  return typeof v === "string" ? v.slice(0, 32) : "";
}
/** One of each name: the creature keeps names unique, a hand-edited soul might not. */
function once<T extends { n: string }>(list: T[]): T[] {
  const seen = new Set<string>();
  return list.filter((x) => !seen.has(x.n) && seen.add(x.n));
}
function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

/** The soul's map, every field checked: it is the phone's own JSON. Null when there is none. */
export function readMap(soul: GrowbotSoul): HouseMap | null {
  const m = obj(soul.map);
  if (m.v !== 1) return null;
  // The page's own caps (growbot-map.js): a soul file imported by hand is not held to them,
  // and the render filters things per room.
  const list = (k: string, max: number) =>
    Array.isArray(m[k]) ? (m[k] as unknown[]).slice(0, max).map(obj) : [];
  const rooms = list("rooms", 24)
    .filter(
      (r) =>
        text(r.n) && finite(r.x) && finite(r.y) && finite(r.w) && finite(r.d) && r.w > 0 && r.d > 0,
    )
    .map((r) => ({
      n: text(r.n),
      x: r.x as number,
      y: r.y as number,
      w: r.w as number,
      d: r.d as number,
    }));
  const doors = list("doors", 48)
    .filter((o) => finite(o.x) && finite(o.y))
    .map((o) => ({ x: o.x as number, y: o.y as number, a: text(o.a), b: text(o.b) }));
  const things = list("things", 120)
    .filter((t) => text(t.n) && finite(t.x) && finite(t.y))
    .map((t) => ({
      n: text(t.n),
      x: t.x as number,
      y: t.y as number,
      room: text(t.room),
      t: finite(t.t) ? t.t : 0,
      rough: !!t.rough,
    }));
  const h = obj(m.here);
  const here = {
    x: finite(h.x) ? h.x : 0,
    y: finite(h.y) ? h.y : 0,
    h: finite(h.h) ? h.h : 90,
    room: text(h.room),
  };
  return { rooms: once(rooms), doors, things: once(things), here };
}

const PAD = 0.6;

/** The drawing's extent in map metres, so it fits whatever he has drawn. */
function bounds(m: HouseMap): [number, number, number, number] {
  const xs = [m.here.x];
  const ys = [m.here.y];
  for (const r of m.rooms) {
    xs.push(r.x - r.w / 2, r.x + r.w / 2);
    ys.push(r.y - r.d / 2, r.y + r.d / 2);
  }
  for (const p of [...m.doors, ...m.things]) {
    xs.push(p.x);
    ys.push(p.y);
  }
  return [
    Math.min(...xs) - PAD,
    Math.min(...ys) - PAD,
    Math.max(...xs) + PAD,
    Math.max(...ys) + PAD,
  ];
}

function r1(v: number): string {
  return (Math.round(v * 10) / 10).toString();
}

export function GrowbotHouseMap({ soul, name }: { soul: GrowbotSoul; name: string }) {
  const m = readMap(soul);
  if (!m || (!m.rooms.length && !m.things.length)) {
    return (
      <p className="gb-hint dim">
        {name} hasn't drawn a map yet. It starts one when it names the room it's in, and adds what
        it sees as it walks about the house.
      </p>
    );
  }
  const [x0, y0, x1, y1] = bounds(m);
  // SVG's y runs down; the map's runs up, so every y is flipped about the drawing.
  const Y = (y: number) => y0 + y1 - y;
  const a = (m.here.h * Math.PI) / 180;
  const hx = m.here.x;
  const hy = Y(m.here.y);
  const tip = `${hx + 0.32 * Math.cos(a)},${hy - 0.32 * Math.sin(a)}`;
  const left = `${hx + 0.14 * Math.cos(a + 2.4)},${hy - 0.14 * Math.sin(a + 2.4)}`;
  const right = `${hx + 0.14 * Math.cos(a - 2.4)},${hy - 0.14 * Math.sin(a - 2.4)}`;
  const rooms = new Map(m.rooms.map((r) => [r.n, r]));
  return (
    <div className="gb-map">
      <svg
        className="gb-map-svg"
        viewBox={`${x0} ${y0} ${x1 - x0} ${y1 - y0}`}
        role="img"
        aria-label={`${name}'s map of the house`}
      >
        {m.rooms.map((r) => (
          <g key={`r-${r.n}`}>
            <rect
              className={r.n === m.here.room ? "gb-map-room here" : "gb-map-room"}
              x={r.x - r.w / 2}
              y={Y(r.y + r.d / 2)}
              width={r.w}
              height={r.d}
            />
            <text className="gb-map-room-name" x={r.x} y={Y(r.y + r.d / 2) + 0.32}>
              {r.n}
            </text>
          </g>
        ))}
        {m.doors.map((o, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: doorways have no name; position is identity.
          <circle key={`d-${i}`} className="gb-map-door" cx={o.x} cy={Y(o.y)} r={0.13} />
        ))}
        {m.things.map((t) => (
          <g key={`t-${t.n}`}>
            <circle
              className={t.rough ? "gb-map-thing rough" : "gb-map-thing"}
              cx={t.x}
              cy={Y(t.y)}
              r={0.09}
            />
            <text className="gb-map-thing-name" x={t.x + 0.14} y={Y(t.y) - 0.1}>
              {t.n}
            </text>
          </g>
        ))}
        <polygon className="gb-map-me" points={`${tip} ${left} ${right}`} />
      </svg>
      <p className="gb-hint dim">
        A rough sketch: {name} guesses where it is from its own walking and puts itself back when it
        reaches something it knows. Tell it when something is wrong and it corrects the map.
      </p>
      <ul className="gb-map-list">
        {m.rooms.map((r) => {
          const things = m.things.filter((t) => t.room === r.n).map((t) => t.n);
          const ways = m.doors
            .filter((o) => o.a === r.n || o.b === r.n)
            .map((o) => (o.a === r.n ? o.b : o.a) || "somewhere unnamed");
          return (
            <li key={`l-${r.n}`}>
              <strong>{r.n}</strong>
              {r.n === m.here.room ? " (it is here)" : ""} · about {r1(r.w)} × {r1(r.d)} m
              {things.length ? ` · ${things.join(", ")}` : ""}
              {ways.length ? ` · doorways to ${ways.join(", ")}` : ""}
            </li>
          );
        })}
        {m.things.some((t) => !rooms.has(t.room)) && (
          <li>
            <strong>Not in a room</strong> ·{" "}
            {m.things
              .filter((t) => !rooms.has(t.room))
              .map((t) => t.n)
              .join(", ")}
          </li>
        )}
      </ul>
    </div>
  );
}
