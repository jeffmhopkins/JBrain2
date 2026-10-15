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
  photoPlacement,
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

// ---- the composer: a 16:9 frame drawn on the phone, the photo always under the words --------

/** The TV's screen, in its own pixels: what the phone draws and the box casts as is. */
export const TV_W = 1920;
export const TV_H = 1080;
/** The frame's sheet with no photo: the box's own frames' near-black (cast/frame.py). */
export const SHEET = "#0c0d10";
export const WORDS_MAX = TITLE_MAX;
export const SIZE_MIN = 48;
export const SIZE_MAX = 320;
export const ZOOM_MAX = 3;
/** A photo's working copy, long side: enough for 2x zoom on a 1920-wide screen without
 *  keeping a 12 MP original on a canvas. */
export const PHOTO_WORKING_MAX = TV_W * 2;
const WORDS_MARGIN = 60;

export interface CastDraft {
  photo: Photo | null;
  zoom: number;
  /** Offset of the photo from centred, in TV pixels. */
  panX: number;
  panY: number;
  words: string;
  size: number;
  style: TextStyle;
  /** Where the words sit (their centre), in TV pixels. */
  pos: Point;
}

export function emptyCastDraft(): CastDraft {
  return {
    photo: null,
    zoom: 1,
    panX: 0,
    panY: 0,
    words: "",
    size: 140,
    style: "outline",
    pos: { x: TV_W / 2, y: TV_H / 2 },
  };
}

/** Something to cast: a photo, some words, or both. */
export function draftReady(d: CastDraft): boolean {
  return d.photo !== null || d.words.trim() !== "";
}

/** What the record and the receiver's title call it: the words, else "Picture". */
export function draftTitle(d: CastDraft): string {
  return d.words.trim().replace(/\s+/g, " ") || "Picture";
}

/** How the words were drawn: the lines, at what size, and whether all of them made it. */
export interface WordsFit {
  lines: string[];
  size: number;
  whole: boolean;
}

const FIT_STEP = 4;

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

/** Draw the frame into `ctx`, whose units are TV pixels: the photo covering the screen
 *  (zoomed and panned), then the words over it. The one drawing serves the preview and what is
 *  sent, so they cannot disagree. Returns how the words fit, or null with none. */
export function drawFrame(ctx: CanvasRenderingContext2D, d: CastDraft): WordsFit | null {
  ctx.fillStyle = SHEET;
  ctx.fillRect(0, 0, TV_W, TV_H);
  if (d.photo) {
    const p = photoPlacement(d.photo, d, TV_W, TV_H);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(d.photo.source, p.x, p.y, d.photo.w * p.scale, d.photo.h * p.scale);
  }
  const text = d.words.trim();
  if (!text) return null;
  const st = TEXT_STYLES[d.style];
  const fit = fitWords(ctx, text, d.size);
  const { lines, size } = fit;
  const measure = (s: string) => ctx.measureText(s).width;
  const box = textBox(lines, size, d.pos, TV_W, TV_H, measure);
  if (st.plate) {
    ctx.fillStyle = st.plate;
    ctx.beginPath();
    ctx.roundRect(box.x, box.y, box.w, box.h, size * 0.22);
    ctx.fill();
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  const cx = box.x + box.w / 2;
  lines.forEach((line, i) => {
    const y = box.y + box.padY + box.lineH * (i + 0.5);
    if (st.stroke) {
      ctx.strokeStyle = st.stroke;
      ctx.lineWidth = size * 0.14;
      ctx.strokeText(line, cx, y);
    }
    ctx.fillStyle = st.fill;
    ctx.fillText(line, cx, y);
  });
  return fit;
}

/** What to tell the owner when the words aren't drawn as asked, or null when they are. */
export function fitNote(fit: WordsFit | null, asked: number): string | null {
  if (!fit) return null;
  if (!fit.whole) return "Too long for the screen: the end is cut off. Shorten the words.";
  if (fit.size < asked) return `Shrunk to ${fit.size} px so every word fits on the screen.`;
  return null;
}

/** A drag of (dx, dy) TV pixels: it moves the words when they are what is being placed, else
 *  pans the photo, held to its edges so a long drag past one banks no slack. */
export function dragDraft(
  d: CastDraft,
  what: "words" | "photo",
  dx: number,
  dy: number,
): CastDraft {
  if (what === "words" && d.words.trim()) {
    return {
      ...d,
      pos: { x: clamp(d.pos.x + dx, 0, TV_W), y: clamp(d.pos.y + dy, 0, TV_H) },
    };
  }
  if (!d.photo) return d;
  const p = photoPlacement(d.photo, { ...d, panX: d.panX + dx, panY: d.panY + dy }, TV_W, TV_H);
  return { ...d, panX: p.panX, panY: p.panY };
}
