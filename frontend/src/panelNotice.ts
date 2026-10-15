// "Send to panel" — the composer's rules, kept apart from its screens so they can be tested
// without a canvas (docs/plans/JPANEL_C6_PLAN.md §2, "The composer"; mocks 45-56).
//
// THE PANEL ONLY EVER GETS A PICTURE. Words the owner types onto a photo are drawn into it here,
// on the phone, so the panel's firmware needs no fonts, no layout and no styles for them: what
// Review shows is what lands. A question is the exception, because its answers are buttons the
// panel has to know about — that goes as text, with the picture as a strip above it.

import type { PanelNoticeIn, PanelNoticeOut } from "./api/client";

/** The glass, in the panel's own pixels. */
export const SCREEN = 480;
/** A picture over a question is a strip this tall; a two-line question takes 40 px of it. */
export const BANNER_H = 240;
export const BANNER_H_TWO_LINES = 200;
/** Past this many characters the panel wraps a question to a second line. */
const QUESTION_ONE_LINE = 28;
/** The box's limits (jpanel/notice.py), said while typing rather than refused after. */
export const WORDS_MAX = 60;
export const QUESTION_MAX = 60;
export const ANSWER_MAX = 16;
export const ANSWERS_MAX = 4;
/** Zoom past 3x is a handful of camera pixels per panel pixel, so the working copy of a photo
 *  is kept at most this long a side — a 12 MP original is 48 MB of canvas for no gain. */
export const PHOTO_WORKING_MAX = SCREEN * 3;
export const ZOOM_MAX = 3;
export const SIZE_MIN = 28;
export const SIZE_MAX = 160;
/** Words never run closer than this to the picture's edge. */
const TEXT_MARGIN = 20;
const TEXT_LINES_MAX = 4;
export const TEXT_FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

export type Layout = "empty" | "full" | "banner" | "plain";
export type TextStyle = "outline" | "dark" | "light" | "yellow";

/** The four styles the owner approved (2026-10-14). The outline is a dark stroke under the
 *  fill, so white words read on a white sky; the plates are for a busy photo. */
export const TEXT_STYLES: Record<
  TextStyle,
  { label: string; fill: string; stroke: string | null; plate: string | null }
> = {
  outline: { label: "Outline", fill: "#FFFFFF", stroke: "#111214", plate: null },
  dark: { label: "Dark plate", fill: "#FFFFFF", stroke: null, plate: "rgba(10,10,12,0.72)" },
  light: { label: "Light plate", fill: "#111214", stroke: null, plate: "rgba(255,255,255,0.9)" },
  yellow: { label: "Yellow", fill: "#FFD447", stroke: "#111214", plate: null },
};

export interface Photo {
  source: CanvasImageSource;
  w: number;
  h: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface Draft {
  photo: Photo | null;
  zoom: number;
  /** Offset of the photo from centred, in panel pixels. */
  panX: number;
  panY: number;
  words: string;
  size: number;
  style: TextStyle;
  /** Where the words sit (their centre), per picture shape: a strip and a square differ. */
  pos: { full: Point; banner: Point };
  ask: boolean;
  question: string;
  answers: string[];
  chime: boolean;
  /** Seconds a plain notice stays on the glass; 0 until tapped. */
  stayPlain: number;
  /** Seconds a question waits for its answer. */
  stayAsk: number;
}

export function emptyDraft(): Draft {
  return {
    photo: null,
    zoom: 1,
    panX: 0,
    panY: 0,
    words: "",
    size: 64,
    style: "outline",
    pos: { full: { x: SCREEN / 2, y: 128 }, banner: { x: SCREEN / 2, y: 52 } },
    ask: false,
    question: "",
    answers: [],
    chime: true,
    stayPlain: 20,
    stayAsk: 900,
  };
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** `text` cut to `max` UTF-8 bytes without splitting a character. */
export function clipBytes(text: string, max: number): string {
  let out = "";
  for (const ch of text) {
    if (utf8Bytes(out + ch) > max) break;
    out += ch;
  }
  return out;
}

/** A picture whenever there is a photo or words; a question puts it in a strip above itself.
 *  A question alone is the panel's own card, and nothing at all cannot be sent. */
export function noticeLayout(d: Draft): Layout {
  const picture = d.photo !== null || d.words.trim() !== "";
  if (picture) return d.ask ? "banner" : "full";
  return d.ask ? "plain" : "empty";
}

export function pictureSize(d: Draft, layout: Layout = noticeLayout(d)): { w: number; h: number } {
  if (layout !== "banner") return { w: SCREEN, h: SCREEN };
  return {
    w: SCREEN,
    h: d.question.trim().length > QUESTION_ONE_LINE ? BANNER_H_TWO_LINES : BANNER_H,
  };
}

/** Why Review cannot be reached yet, or null when it can. */
export function draftProblem(d: Draft): string | null {
  const layout = noticeLayout(d);
  if (layout === "empty") return "Add a photo, some words or a question.";
  if (!d.ask) return null;
  if (d.question.trim() === "") return "Write the question.";
  const answers = d.answers.map((a) => a.trim());
  if (answers.length === 0) return "Add at least one answer.";
  if (answers.some((a) => a === "")) return "An answer is empty.";
  if (new Set(answers).size !== answers.length) return "Two answers read the same.";
  return null;
}

/** The photo's scale and top-left in a `w` x `h` picture: it always covers the picture, zoom
 *  enlarges it about the centre, and the pan is held so no edge of the photo shows. */
export function photoPlacement(
  photo: { w: number; h: number },
  d: Pick<Draft, "zoom" | "panX" | "panY">,
  w: number,
  h: number,
): { scale: number; x: number; y: number; panX: number; panY: number } {
  const scale = Math.max(w / photo.w, h / photo.h) * d.zoom;
  const dw = photo.w * scale;
  const dh = photo.h * scale;
  const panX = clamp(d.panX, -(dw - w) / 2, (dw - w) / 2);
  const panY = clamp(d.panY, -(dh - h) / 2, (dh - h) / 2);
  return { scale, x: (w - dw) / 2 + panX, y: (h - dh) / 2 + panY, panX, panY };
}

export function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** Words wrapped to the picture's width the way they will be drawn, at most four lines. A word
 *  wider than the picture is broken by characters rather than left to run off the edge. */
export function wrapWords(text: string, maxW: number, measure: (s: string) => number): string[] {
  const out: string[] = [];
  let cur = "";
  const push = (word: string) => {
    const next = cur ? `${cur} ${word}` : word;
    if (measure(next) <= maxW) {
      cur = next;
      return;
    }
    if (cur) out.push(cur);
    cur = "";
    let piece = "";
    for (const ch of word) {
      if (piece && measure(piece + ch) > maxW) {
        out.push(piece);
        piece = "";
      }
      piece += ch;
    }
    cur = piece;
  };
  for (const word of text.trim().split(/\s+/)) if (word) push(word);
  if (cur) out.push(cur);
  return out.slice(0, TEXT_LINES_MAX);
}

/** The words' box in picture pixels, centred on `at` and kept inside the picture. */
export function textBox(
  lines: string[],
  size: number,
  at: Point,
  w: number,
  h: number,
  measure: (s: string) => number,
): { x: number; y: number; w: number; h: number; lineH: number; padX: number; padY: number } {
  const lineH = size * 1.12;
  const padX = size * 0.32;
  const padY = size * 0.12;
  const boxW = Math.max(0, ...lines.map(measure)) + padX * 2;
  const boxH = lines.length * lineH + padY * 2;
  const cx = clamp(at.x, Math.min(boxW / 2, w / 2), Math.max(w - boxW / 2, w / 2));
  const cy = clamp(at.y, Math.min(boxH / 2, h / 2), Math.max(h - boxH / 2, h / 2));
  return { x: cx - boxW / 2, y: cy - boxH / 2, w: boxW, h: boxH, lineH, padX, padY };
}

/** Draw the picture the panel will show into `ctx`, whose units are panel pixels. The one
 *  drawing serves the preview, the export and the thumbnail, so they cannot disagree. */
export function drawPicture(ctx: CanvasRenderingContext2D, d: Draft, w: number, h: number): void {
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, w, h);
  if (d.photo) {
    const p = photoPlacement(d.photo, d, w, h);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(d.photo.source, p.x, p.y, d.photo.w * p.scale, d.photo.h * p.scale);
  }
  const text = d.words.trim();
  if (!text) return;
  const st = TEXT_STYLES[d.style];
  ctx.font = `700 ${d.size}px ${TEXT_FONT}`;
  const measure = (s: string) => ctx.measureText(s).width;
  const lines = wrapWords(text, w - TEXT_MARGIN * 2, measure);
  const at = h === SCREEN ? d.pos.full : d.pos.banner;
  const box = textBox(lines, d.size, at, w, h, measure);
  if (st.plate) {
    ctx.fillStyle = st.plate;
    ctx.beginPath();
    ctx.roundRect(box.x, box.y, box.w, box.h, d.size * 0.22);
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
      ctx.lineWidth = d.size * 0.14;
      ctx.strokeText(line, cx, y);
    }
    ctx.fillStyle = st.fill;
    ctx.fillText(line, cx, y);
  });
}

/** A camera photo scaled once to the working size, so zooming and dragging redraw from a
 *  small copy. Never scaled up. */
export function workingSize(
  w: number,
  h: number,
  max = PHOTO_WORKING_MAX,
): { w: number; h: number } {
  const k = Math.min(1, max / Math.max(w, h));
  return { w: Math.max(1, Math.round(w * k)), h: Math.max(1, Math.round(h * k)) };
}

/** What goes to the box. `image` is the drawn picture (base64), absent for a question alone. */
export function noticePayload(d: Draft, deviceId: string, image: string | null): PanelNoticeIn {
  const layout = noticeLayout(d);
  const out: PanelNoticeIn = { device_id: deviceId, title: noticeTitle(d), chime: d.chime };
  if (image !== null && (layout === "full" || layout === "banner")) {
    out.image_b64 = image;
    out.image_mode = layout === "banner" ? "banner" : "full";
  }
  if (d.ask) {
    out.choices = d.answers.map((a) => a.trim());
    // Until answered, and answers are taken for as long as the owner chose.
    out.hide_after_s = 0;
    out.ttl_s = d.stayAsk;
  } else {
    out.hide_after_s = d.stayPlain;
    // "Until tap" means until tapped, not until the box's default fifteen-minute window
    // lapses it unseen behind the clock face.
    if (d.stayPlain === 0) out.ttl_s = 24 * 3600;
  }
  return out;
}

/** The notice's title: the question, else the words, else what it is. The panel shows it at
 *  night before the picture, and the history lists by it. */
export function noticeTitle(d: Draft): string {
  if (d.ask) return d.question.trim();
  return d.words.trim() || "Photo";
}

export const STAY_PLAIN: [number, string][] = [
  [20, "20 s"],
  [60, "1 min"],
  [300, "5 min"],
  [0, "Until tap"],
];
export const STAY_ASK: [number, string][] = [
  [900, "15 min"],
  [3600, "1 h"],
  [28800, "8 h"],
  [86400, "24 h"],
];

export function stayShort(s: number | null): string {
  if (!s) return "until tapped";
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}

export const OPEN_STATES = new Set(["queued", "delivered", "shown"]);

export type Tone = "ok" | "warn" | "dim" | "live";

/** How a sent notice is doing or how it ended, in the owner's words. */
export function outcome(n: PanelNoticeOut): { text: string; tone: Tone } {
  const ask = n.choices.length > 0;
  switch (n.state) {
    case "queued":
      return { text: "Sent · the panel picks it up within seconds", tone: "live" };
    case "delivered":
      return { text: "On the panel", tone: "live" };
    case "shown":
      return { text: ask ? "Waiting for an answer" : "On the glass", tone: "live" };
    case "answered":
      return { text: `Answered “${n.answer ?? ""}”`, tone: "ok" };
    case "dismissed":
      return ask
        ? { text: "Hidden without an answer", tone: "warn" }
        : { text: "Tapped away on the panel", tone: "dim" };
    case "withdrawn":
      return {
        text: n.delivered_at ? "Unsent · taken off the panel" : "Unsent before the panel had it",
        tone: "dim",
      };
    case "expired":
      if (!n.delivered_at) return { text: "The panel never picked it up", tone: "warn" };
      if (ask) return { text: `No answer in ${stayShort(n.ttl_s)}`, tone: "warn" };
      if (n.shown_at && n.hide_after_s)
        return { text: `Shown · hid itself after ${stayShort(n.hide_after_s)}`, tone: "dim" };
      return { text: "Lapsed before it was seen", tone: "warn" };
    default:
      return { text: n.state, tone: "dim" };
  }
}

export interface RefusalCopy {
  title: string;
  body: string;
  /** The remedy is a switch in Panel settings → Notices. */
  settings: boolean;
}

export function agoWords(s: number): string {
  if (s < 90) return `${Math.max(1, Math.round(s))} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

/** The box's refusal, said as the mocks say it: what happened, then what to do. Nothing waits
 *  on the box, so every one ends with sending again. */
export function refusalCopy(r: {
  status: number;
  reason: string;
  until?: string;
  last_seen_s?: number | null;
}): RefusalCopy {
  switch (r.reason) {
    case "offline":
      return {
        title: "your panel is offline",
        body: `${
          typeof r.last_seen_s === "number"
            ? `It was last heard from ${agoWords(r.last_seen_s)} ago.`
            : "The box has not heard from it."
        } Nothing waits on the box: send it again once the panel is back.`,
        settings: false,
      };
    case "not_accepting":
      return {
        title: "notices are off on the panel",
        body: "Accept notices is switched off. Turn it on in Panel settings, or on the panel, then send again.",
        settings: true,
      };
    case "quiet_hours":
      return {
        title: r.until
          ? `it’s night hours on the panel until ${r.until}`
          : "it’s night hours on the panel",
        body: `Refuse during night hours is on. Send it ${
          r.until ? `after ${r.until}` : "in the morning"
        }, or switch that off in Panel settings.`,
        settings: true,
      };
    case "busy":
      return {
        title: "4 notices already waiting",
        body: "The panel holds four at a time. Send again once one is answered or hidden.",
        settings: false,
      };
    case "no_notice_surface":
      return {
        title: "the panel’s firmware can’t show notices yet",
        body: "It updates itself. Send again once it has.",
        settings: false,
      };
    case "image_too_complex":
      return {
        title: "the picture is too detailed for the panel",
        body: "Even with fewer colours it would not fit. Zoom in, or choose a simpler photo.",
        settings: false,
      };
    case "image_too_big":
    case "image_format":
    case "image_unreadable":
      return {
        title: "the panel can’t take this picture",
        body: "Choose another photo, or send the words alone.",
        settings: false,
      };
    default:
      return {
        title: "the box turned it away",
        body: `It said “${r.reason}”. Nothing was sent.`,
        settings: false,
      };
  }
}

function dayKey(t: Date): string {
  return `${t.getFullYear()}-${t.getMonth()}-${t.getDate()}`;
}

/** A sent notice's day, as the history groups it. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const t = new Date(iso);
  if (dayKey(t) === dayKey(now)) return "Today";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (dayKey(t) === dayKey(y)) return "Yesterday";
  return t.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

export function groupByDay(
  notices: PanelNoticeOut[],
  now: Date = new Date(),
): { label: string; rows: PanelNoticeOut[] }[] {
  const out: { label: string; rows: PanelNoticeOut[] }[] = [];
  for (const n of notices) {
    const label = dayLabel(n.created_at, now);
    const last = out[out.length - 1];
    if (last && last.label === label) last.rows.push(n);
    else out.push({ label, rows: [n] });
  }
  return out;
}

export function clock(iso: string | null, seconds = false): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
    hour12: false,
  });
}

/** The deep link the launcher shortcut, the share target and an iPhone home-screen icon open. */
export const SEND_PATH = "/panel/send";
