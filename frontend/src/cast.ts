// Cast — notices on the house's TVs: the screen's words and rules, kept apart from it so they
// can be tested (docs/plans/JPANEL_CAST_PLAN.md K2; mock docs/mocks/jpanel-cast/).
//
// The box draws the frame and decides, at the instant of casting, whether each TV takes it.
// The phone only says what the box said, in words, and never colours a state without them.

import type { CastNotice, CastOutcome, CastTv } from "./api/client";
import {
  type Photo,
  type Point,
  TEXT_FONT,
  TEXT_STYLES,
  type TextStyle,
  clamp,
  clipBytes,
  textBox,
  wrapWords,
} from "./panelNotice";

/** The Default Media Receiver: what our own notice plays on. */
export const MEDIA_RECEIVER = "CC1AD845";
/** The box's limits (jbrain/cast/service.py), said while typing rather than refused after. */
export const TITLE_MAX = 120;
export const BODY_MAX = 600;
export const SECONDS = [15, 30, 60, 120, 300] as const;
export const SECONDS_DEFAULT = 30;

export type Tone = "ready" | "off" | "busy" | "gone";

export interface TvWords {
  /** The word on the row's right: Ready, Off, In use, Not answering. */
  label: string;
  /** What it is doing, under its name. */
  sub: string;
  tone: Tone;
}

/** What a TV is doing, in words. Our own notice reads `busy` to the gate (two must not stack),
 *  but the owner should read that it is showing one, not that somebody is watching. */
export function tvWords(tv: CastTv): TvWords {
  switch (tv.state) {
    case "idle":
      return { label: "Ready", sub: idleSub(tv.why), tone: "ready" };
    case "standby":
      return { label: "Off", sub: "Off · will turn on", tone: "off" };
    case "busy":
      if (tv.reading.app_id === MEDIA_RECEIVER) {
        return { label: "In use", sub: "Showing a notice", tone: "busy" };
      }
      return { label: "In use", sub: capitalise(tv.why), tone: "busy" };
    default:
      return {
        label: "Not answering",
        sub: tv.why === "not connected" ? "Not reachable right now" : "Still connecting",
        tone: "gone",
      };
  }
}

function idleSub(why: string): string {
  if (!why || why === "nothing open") return "Home screen, nothing playing";
  return capitalise(why);
}

function capitalise(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/** The launcher badge: TVs that would take a notice right now. */
export function readyCount(tvs: readonly CastTv[]): number {
  return tvs.filter((t) => t.castable).length;
}

/** Why the owner can't pick a TV, or null when it can take a notice. */
export function skipReason(tv: CastTv): string | null {
  if (tv.castable) return null;
  return `${tv.name} is ${tvWords(tv).sub.toLowerCase()} and will be skipped.`;
}

export function outcomeLabel(outcome: CastOutcome): string {
  switch (outcome) {
    case "shown":
      return "On screen";
    case "woke":
      return "Turned on";
    case "skipped":
      return "Skipped";
    default:
      return "Didn't cast";
  }
}

/** The line under a TV in the result: what it was doing when the notice arrived. */
export function outcomeSub(o: { outcome: CastOutcome; why: string }): string {
  if (o.outcome === "woke") return "Was off; it stays on until its own timer or the remote";
  if (o.outcome === "shown") return o.why ? `Over ${idleSub(o.why).toLowerCase()}` : "";
  return capitalise(o.why);
}

export function landed(n: CastNotice): number {
  return n.tvs.filter((t) => t.outcome === "shown" || t.outcome === "woke").length;
}

/** "Cast to 2 of 3" — the result sheet's title. */
export function resultTitle(n: CastNotice): string {
  const got = landed(n);
  if (got === 0) return n.tvs.length === 1 ? "Not cast" : `Cast to none of ${n.tvs.length}`;
  return `Cast to ${got} of ${n.tvs.length}`;
}

/** One Sent row's line: when, from whom unless the owner, where it landed and what was
 *  skipped. */
export function sentSummary(n: CastNotice, zone?: string): string {
  const at = new Date(n.created_at).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(zone ? { timeZone: zone } : {}),
  });
  const parts = [at];
  if (n.sender !== "owner") parts.push(`from ${senderLabel(n.sender)}`);
  const shown = n.tvs.filter((t) => t.outcome === "shown" || t.outcome === "woke");
  parts.push(shown.length ? shown.map((t) => t.name).join(", ") : "on no TV");
  const missed = n.tvs.length - shown.length;
  if (missed > 0) parts.push(`${missed} skipped`);
  return parts.join(" · ");
}

export function senderLabel(sender: string): string {
  if (sender === "debug") return "a jbrain test";
  if (sender === "jerv") return "Jerv";
  if (sender === "ops") return "Ops";
  return sender.replace(/^workflow:/, "");
}

/** Whether Take it down still has anything to do: something landed, it hasn't been taken
 *  down, and it may still be up. */
export function canTakeDown(n: CastNotice, now = Date.now()): boolean {
  if (n.stopped_at !== null || landed(n) === 0) return false;
  return now < new Date(n.created_at).getTime() + n.seconds * 1000;
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function secondsLabel(s: number): string {
  return s < 60 ? `${s} s` : `${s / 60} min`;
}

// ---- the composer: a 16:9 frame drawn on the phone, built from things the owner adds -------
//
// The owner adds words and pictures, each a layer they hold directly: a drag moves it, a pinch
// sizes it and a twist turns it (owner, 2026-10-15: "just click 'add text' or 'add image' and
// then use mobile touch to zoom rotate, place the things"). Pictures always sit under the
// words, so a picture added last can never hide a message.

/** The TV's screen, in its own pixels: what the phone draws and the box casts as is. */
export const TV_W = 1920;
export const TV_H = 1080;
/** The frame's sheet with no photo: the box's own frames' near-black (cast/frame.py). */
export const SHEET = "#0c0d10";
export const WORDS_MAX = TITLE_MAX;
export const SIZE_MIN = 48;
export const SIZE_MAX = 320;
export const SIZE_DEFAULT = 140;
/** A picture's scale: 1 fits it whole on the screen. */
export const ZOOM_MAX = 4;
export const ZOOM_MIN = 0.15;
/** Enough layers for a message and a few pictures, few enough that working copies of camera
 *  photos don't crowd the phone's memory. */
export const LAYERS_MAX = 8;
/** A photo's working copy, long side: enough for 2x zoom on a 1920-wide screen without
 *  keeping a 12 MP original on a canvas. */
export const PHOTO_WORKING_MAX = TV_W * 2;
const WORDS_MARGIN = 60;

interface LayerBase {
  id: number;
  /** Its centre, in TV pixels. */
  x: number;
  y: number;
  /** Its turn about its centre, in radians, clockwise. */
  turn: number;
}

export interface PictureLayer extends LayerBase {
  kind: "picture";
  photo: Photo;
  /** 1 fits the picture whole on the screen. */
  zoom: number;
}

export interface WordsLayer extends LayerBase {
  kind: "words";
  text: string;
  /** The words' size in pixels on the TV, before any shrink to fit. */
  size: number;
  style: TextStyle;
}

export type Layer = PictureLayer | WordsLayer;

export interface CastDraft {
  layers: Layer[];
  nextId: number;
}

export function emptyCastDraft(): CastDraft {
  return { layers: [], nextId: 1 };
}

/** Something to cast: a picture, or some words. */
export function draftReady(d: CastDraft): boolean {
  return d.layers.some((l) => l.kind === "picture" || l.text.trim() !== "");
}

/** What the record and the receiver's title call it: the words, else "Picture". */
export function draftTitle(d: CastDraft): string {
  const words = d.layers
    .filter((l): l is WordsLayer => l.kind === "words")
    .map((l) => l.text.trim().replace(/\s+/g, " "))
    .filter(Boolean)
    .join(" · ");
  return clipBytes(words, TITLE_MAX) || "Picture";
}

export function canAdd(d: CastDraft): boolean {
  return d.layers.length < LAYERS_MAX;
}

/** The order they are drawn in, bottom first: pictures, then words, each as added. */
export function drawOrder(d: CastDraft): Layer[] {
  return [
    ...d.layers.filter((l) => l.kind === "picture"),
    ...d.layers.filter((l) => l.kind === "words"),
  ];
}

export function layerById(d: CastDraft, id: number | null): Layer | null {
  return d.layers.find((l) => l.id === id) ?? null;
}

/** New words, centred, or a step below the last words so two don't land on each other. */
export function addWords(d: CastDraft, text: string, style: TextStyle = "outline"): CastDraft {
  if (!canAdd(d)) return d;
  const others = d.layers.filter((l) => l.kind === "words").length;
  const step = [0, 1, -1][others % 3] ?? 0;
  const y = TV_H / 2 + step * SIZE_DEFAULT * 1.4;
  const layer: WordsLayer = {
    id: d.nextId,
    kind: "words",
    text: clipBytes(text, WORDS_MAX),
    size: SIZE_DEFAULT,
    style,
    x: TV_W / 2,
    y,
    turn: 0,
  };
  return { layers: [...d.layers, layer], nextId: d.nextId + 1 };
}

/** A new picture: the first fills the screen, as a backdrop usually does; later ones come in
 *  at half size so they read as something placed on it. */
export function addPicture(d: CastDraft, photo: Photo): CastDraft {
  if (!canAdd(d)) return d;
  const first = !d.layers.some((l) => l.kind === "picture");
  const layer: PictureLayer = {
    id: d.nextId,
    kind: "picture",
    photo,
    zoom: first ? coverZoom(photo) : 0.5,
    x: TV_W / 2,
    y: TV_H / 2,
    turn: 0,
  };
  return { layers: [...d.layers, layer], nextId: d.nextId + 1 };
}

/** The zoom at which a picture just covers the screen. */
export function coverZoom(photo: { w: number; h: number }): number {
  const fit = Math.min(TV_W / photo.w, TV_H / photo.h);
  return Math.max(TV_W / photo.w, TV_H / photo.h) / fit;
}

export function updateLayer(d: CastDraft, id: number, fn: (l: Layer) => Layer): CastDraft {
  let changed = false;
  const layers = d.layers.map((l) => {
    if (l.id !== id) return l;
    const next = fn(l);
    if (next !== l) changed = true;
    return next;
  });
  return changed ? { ...d, layers } : d;
}

export function removeLayer(d: CastDraft, id: number): CastDraft {
  return { ...d, layers: d.layers.filter((l) => l.id !== id) };
}

/** Change a layer's words; emptied, the layer goes. */
export function setWords(d: CastDraft, id: number, text: string): CastDraft {
  if (!text.trim()) return removeLayer(d, id);
  const clipped = clipBytes(text, WORDS_MAX);
  return updateLayer(d, id, (l) => (l.kind === "words" ? { ...l, text: clipped } : l));
}

export function setStyle(d: CastDraft, id: number, style: TextStyle): CastDraft {
  return updateLayer(d, id, (l) => (l.kind === "words" ? { ...l, style } : l));
}

/** How the words were drawn: the lines, at what size, and whether all of them made it. */
export interface WordsFit {
  lines: string[];
  size: number;
  whole: boolean;
}

const FIT_STEP = 4;

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
  /** Its turn about its centre, in radians; none when absent. */
  turn?: number;
}

/** Turn `ctx` by `turn` about (cx, cy), so what is drawn next in screen pixels lands turned. */
function turnAbout(ctx: CanvasRenderingContext2D, turn: number, cx: number, cy: number): void {
  if (!turn) return;
  ctx.translate(cx, cy);
  ctx.rotate(turn);
  ctx.translate(-cx, -cy);
}

/** The words as they will be drawn: at the chosen size when they all fit, else the largest
 *  size down to SIZE_MIN that shows every word on the screen. The wrap keeps four lines and
 *  drops the rest, and a tall block runs off the top and bottom, so without this the end of a
 *  message could vanish from the TV while Sent still lists it. `whole` is false only when
 *  even SIZE_MIN can't hold them. Leaves `ctx.font` at the size returned. */
export function fitWords(ctx: CanvasRenderingContext2D, text: string, size: number): WordsFit {
  const want = text.replace(/\s+/g, "").length;
  const measure = (s: string) => ctx.measureText(s).width;
  let best: WordsFit = { lines: [], size, whole: false };
  for (let s = size; s >= SIZE_MIN; s -= FIT_STEP) {
    ctx.font = `700 ${s}px ${TEXT_FONT}`;
    const lines = wrapWords(text, TV_W - WORDS_MARGIN * 2, measure);
    const all = lines.join("").replace(/\s+/g, "").length === want;
    const box = textBox(lines, s, { x: TV_W / 2, y: TV_H / 2 }, TV_W, TV_H, measure);
    best = { lines, size: s, whole: all && box.h <= TV_H };
    if (best.whole) return best;
  }
  return best;
}

/** The words' box about their centre, at the size they fit at. */
function wordsBox(ctx: CanvasRenderingContext2D, l: WordsLayer, fit: WordsFit) {
  const at = { x: 0, y: 0 };
  const b = textBox(
    fit.lines,
    fit.size,
    at,
    Number.MAX_SAFE_INTEGER,
    Number.MAX_SAFE_INTEGER,
    (s) => ctx.measureText(s).width,
  );
  return { ...b, x: l.x - b.w / 2, y: l.y - b.h / 2 };
}

/** A picture's box on the screen, unturned. */
export function pictureRect(l: PictureLayer): Rect {
  const fit = Math.min(TV_W / l.photo.w, TV_H / l.photo.h) * l.zoom;
  const w = l.photo.w * fit;
  const h = l.photo.h * fit;
  return { x: l.x - w / 2, y: l.y - h / 2, w, h, turn: l.turn };
}

/** A layer's box on the screen as it is drawn, turned with it; null for empty words. */
export function layerRect(ctx: CanvasRenderingContext2D, l: Layer): Rect | null {
  if (l.kind === "picture") return pictureRect(l);
  const text = l.text.trim();
  if (!text) return null;
  const b = wordsBox(ctx, l, fitWords(ctx, text, l.size));
  return { x: b.x, y: b.y, w: b.w, h: b.h, turn: l.turn };
}

/** Draw the frame into `ctx`, whose units are TV pixels: the pictures, then the words over
 *  them. The one drawing serves the preview and what is sent, so they cannot disagree. Returns
 *  how each layer of words fit, by id. */
export function drawFrame(ctx: CanvasRenderingContext2D, d: CastDraft): Map<number, WordsFit> {
  const fits = new Map<number, WordsFit>();
  ctx.fillStyle = SHEET;
  ctx.fillRect(0, 0, TV_W, TV_H);
  for (const l of drawOrder(d)) {
    if (l.kind === "picture") {
      const r = pictureRect(l);
      ctx.save();
      turnAbout(ctx, l.turn, l.x, l.y);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(l.photo.source, r.x, r.y, r.w, r.h);
      ctx.restore();
      continue;
    }
    const text = l.text.trim();
    if (!text) continue;
    const st = TEXT_STYLES[l.style];
    const fit = fitWords(ctx, text, l.size);
    fits.set(l.id, fit);
    const box = wordsBox(ctx, l, fit);
    ctx.save();
    turnAbout(ctx, l.turn, l.x, l.y);
    if (st.plate) {
      ctx.fillStyle = st.plate;
      ctx.beginPath();
      ctx.roundRect(box.x, box.y, box.w, box.h, fit.size * 0.22);
      ctx.fill();
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    fit.lines.forEach((line, i) => {
      const y = box.y + box.padY + box.lineH * (i + 0.5);
      if (st.stroke) {
        ctx.strokeStyle = st.stroke;
        ctx.lineWidth = fit.size * 0.14;
        ctx.strokeText(line, l.x, y);
      }
      ctx.fillStyle = st.fill;
      ctx.fillText(line, l.x, y);
    });
    ctx.restore();
  }
  return fits;
}

/** What to tell the owner when the words aren't drawn as asked, or null when they are. */
export function fitNote(fit: WordsFit | null | undefined, asked: number): string | null {
  if (!fit) return null;
  if (!fit.whole) return "Too long for the screen: the end is cut off. Shorten the words.";
  if (fit.size < asked) return `Shrunk to ${fit.size} px so every word fits on the screen.`;
  return null;
}

/** What a touch at `at` (TV pixels) takes hold of: the topmost layer as drawn, with a little
 *  slack around words for a finger. */
export function layerAt(
  ctx: CanvasRenderingContext2D,
  d: CastDraft,
  at: Point,
  slack = 0,
): number | null {
  const order = drawOrder(d);
  for (let i = order.length - 1; i >= 0; i--) {
    const l = order[i] as Layer;
    const r = layerRect(ctx, l);
    if (r && inside(r, at, l.kind === "words" ? slack : 0)) return l.id;
  }
  return null;
}

export function inside(r: Rect, at: Point, slack = 0): boolean {
  const p = r.turn ? turnPoint(at, { x: r.x + r.w / 2, y: r.y + r.h / 2 }, -r.turn) : at;
  return (
    p.x >= r.x - slack && p.x <= r.x + r.w + slack && p.y >= r.y - slack && p.y <= r.y + r.h + slack
  );
}

export function turnPoint(p: Point, about: Point, turn: number): Point {
  const c = Math.cos(turn);
  const s = Math.sin(turn);
  const dx = p.x - about.x;
  const dy = p.y - about.y;
  return { x: about.x + dx * c - dy * s, y: about.y + dx * s + dy * c };
}

/** Where a rect's corner handle sits on the screen: its bottom-right corner, turned with it. */
export function handleAt(r: Rect): Point {
  const corner = { x: r.x + r.w, y: r.y + r.h };
  return turnPoint(corner, { x: r.x + r.w / 2, y: r.y + r.h / 2 }, r.turn ?? 0);
}

/** A drag of (dx, dy) TV pixels: the layer's centre moves, kept on the screen so nothing can
 *  be lost off its edge. */
export function moveLayer(d: CastDraft, id: number, dx: number, dy: number): CastDraft {
  return updateLayer(d, id, (l) => ({
    ...l,
    x: clamp(l.x + dx, 0, TV_W),
    y: clamp(l.y + dy, 0, TV_H),
  }));
}

/** How big a layer is now: words' size in pixels, a picture's zoom. */
export function layerScale(l: Layer): number {
  return l.kind === "words" ? l.size : l.zoom;
}

/** Resize a layer to `value` (as layerScale reads it), kept to its range. */
export function scaleLayer(d: CastDraft, id: number, value: number): CastDraft {
  return updateLayer(d, id, (l) =>
    l.kind === "words"
      ? { ...l, size: Math.round(clamp(value, SIZE_MIN, SIZE_MAX)) }
      : { ...l, zoom: clamp(value, ZOOM_MIN, ZOOM_MAX) },
  );
}

/** Within this of square (a multiple of 90 degrees), a turn settles square: a hand can't hold
 *  a photo exactly level, and level is nearly always what is meant. */
const SQUARE_SNAP = (5 * Math.PI) / 180;
const QUARTER = Math.PI / 2;

/** A turn kept within one revolution and settled square when near. */
export function settleTurn(turn: number): number {
  let t = turn % (2 * Math.PI);
  if (t > Math.PI) t -= 2 * Math.PI;
  if (t <= -Math.PI) t += 2 * Math.PI;
  const square = Math.round(t / QUARTER) * QUARTER;
  if (Math.abs(t - square) <= SQUARE_SNAP) t = square;
  if (Math.abs(t) === Math.PI) t = Math.PI;
  return t || 0;
}

export function turnLayer(d: CastDraft, id: number, turn: number): CastDraft {
  return updateLayer(d, id, (l) => ({ ...l, turn: settleTurn(turn) }));
}
