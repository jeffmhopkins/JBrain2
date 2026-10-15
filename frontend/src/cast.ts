// Cast — notices on the house's TVs: the screen's words and rules, kept apart from it so they
// can be tested (docs/plans/JPANEL_CAST_PLAN.md K2; mock docs/mocks/jpanel-cast/).
//
// The box draws the frame and decides, at the instant of casting, whether each TV takes it.
// The phone only says what the box said, in words, and never colours a state without them.

import type { CastNotice, CastOutcome, CastTv } from "./api/client";

/** The Default Media Receiver: what our own notice plays on. */
export const MEDIA_RECEIVER = "CC1AD845";
/** The box's limits (jbrain/cast/service.py), said while typing rather than refused after. */
export const TITLE_MAX = 120;
export const BODY_MAX = 600;
export const SECONDS = [15, 30, 60, 120, 300] as const;
export const SECONDS_DEFAULT = 30;
/** A photo is sent no bigger than the TV draws it; a 12 MP original buys nothing. */
export const PHOTO_MAX_SIDE = 1920;

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

/** The size to send a photo at: its own, or scaled down to fit `max` on its long side. */
export function fitSize(w: number, h: number, max = PHOTO_MAX_SIDE): { w: number; h: number } {
  const long = Math.max(w, h);
  if (long <= max) return { w, h };
  const k = max / long;
  return { w: Math.round(w * k), h: Math.round(h * k) };
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function secondsLabel(s: number): string {
  return s < 60 ? `${s} s` : `${s / 60} min`;
}
