// The radio's audio belongs to the LEASE, not to the tuner sheet.
//
// These replace an earlier set that asserted the element could be detached from the
// document and keep playing. jsdom happily agreed; Chromium does not, because the
// HTML spec pauses a media element the moment it leaves a document. So the property
// worth pinning here is not "it survives detaching" but "it is never detached at
// all" — the sheet borrows nothing, and nothing about playback depends on a mounted
// component. The behaviour jsdom cannot speak to is verified against real Chromium
// instead; see the harness described in docs/plans/SDR_RADIO_PLAN.md D6.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  SDR_AUDIO_SRC,
  isSdrPlaying,
  playSdrAudio,
  resetSdrAudio,
  sdrAnalyser,
  stopSdrAudio,
  subscribeSdrAudio,
  toggleSdrAudio,
} from "./sdrAudio";

afterEach(() => resetSdrAudio());

function element(): HTMLAudioElement | null {
  return document.querySelector("audio");
}

/** jsdom does not implement play(), so `paused` never goes false on its own and the
 *  toggle would always take the play branch. Force the state a real browser would
 *  reach, so the pause path is actually exercised rather than silently skipped. */
function pretendPlaying(el: HTMLAudioElement | null): void {
  if (!el) return;
  Object.defineProperty(el, "paused", { value: false, configurable: true, writable: true });
}

describe("the radio's audio element", () => {
  it("plays from the proxied stream, not the sidecar", () => {
    playSdrAudio();

    // Same-origin and owner-session-authed; the sidecar is on a network the browser
    // has no route to, so a direct URL here would simply not resolve.
    expect(element()?.src).toContain(SDR_AUDIO_SRC);
  });

  it("parks the element in the document and leaves it there", () => {
    playSdrAudio();

    // The whole fix: a media element removed from a document is paused by the user
    // agent, so this one is hidden in <body> rather than moved in and out with the
    // sheet. If this ever becomes false, closing the tuner goes silent again.
    expect(element()?.parentElement).toBe(document.body);
    expect(element()?.style.display).toBe("none");
  });

  it("carries no native controls", () => {
    playSdrAudio();

    // A live stream has no timeline: the native transport renders a scrubber over
    // nothing and sits at 0:00 / 0:00. The sheet draws play/pause instead.
    expect(element()?.controls).toBe(false);
  });

  it("drops the connection when paused, and rejoins live on resume", () => {
    playSdrAudio();
    const el = element();
    expect(el?.getAttribute("src")).not.toBeNull();
    pretendPlaying(el);

    toggleSdrAudio();

    // Paused radio should not go on buffering: resuming must rejoin the broadcast as
    // it is now, not play out a backlog from while nobody was listening.
    expect(el?.hasAttribute("src")).toBe(false);
    expect(isSdrPlaying()).toBe(false);

    // A real browser sets this itself when pause() lands; jsdom needs telling.
    Object.defineProperty(el as HTMLAudioElement, "paused", { value: true, configurable: true });

    toggleSdrAudio();
    expect(element()?.getAttribute("src")).toContain(SDR_AUDIO_SRC);
  });

  it("tears down only when the lease ends", () => {
    playSdrAudio();
    stopSdrAudio();

    // Releasing the radio must not leave a connection open pulling audio nobody hears.
    expect(element()).toBeNull();
    expect(isSdrPlaying()).toBe(false);
  });

  it("tells the transport when the state changes", () => {
    const seen = vi.fn();
    playSdrAudio();
    pretendPlaying(element());
    const off = subscribeSdrAudio(seen);

    toggleSdrAudio();

    // The sheet's button reads the element rather than any state of its own, so it
    // must hear about a pause that it did not initiate.
    expect(seen).toHaveBeenCalled();
    off();
  });

  it("does not let a refusing play() escape into the session store", () => {
    // playSdrAudio runs inside sdrSession's publish(), on a one-second poll. An
    // exception escaping it would stop every listener being notified and freeze the
    // composer icon on a stale reading — a dead UI caused by a muted speaker.
    playSdrAudio();
    const el = element();
    if (el) {
      el.play = () => {
        throw new Error("NotAllowedError");
      };
    }

    expect(() => playSdrAudio()).not.toThrow();
  });
});

describe("the analyser tap", () => {
  it("refuses to take the element while the context is suspended", () => {
    // The one-way door. createMediaElementSource routes the element's output through
    // the graph permanently, so taking it while the context cannot run would trade a
    // working radio for a picture of one — this file's oldest bug in a new costume.
    const created = vi.fn();
    class Suspended {
      state = "suspended";
      resume() {
        return Promise.resolve();
      }
      createMediaElementSource = created;
      createAnalyser = created;
    }
    vi.stubGlobal("AudioContext", Suspended);
    playSdrAudio();

    expect(sdrAnalyser()).toBeNull();
    expect(created).not.toHaveBeenCalled();
    // Still pointed at the stream and playable: the audio path is untouched.
    expect(element()?.getAttribute("src")).toContain(SDR_AUDIO_SRC);
    vi.unstubAllGlobals();
  });

  it("connects through to the destination once the context is running", () => {
    // A graph that ends at the analyser is a dead end: the radio would go silent.
    const connect = vi.fn();
    const node = { fftSize: 0, smoothingTimeConstant: 0, connect };
    class Running {
      state = "running";
      destination = { id: "speakers" };
      resume() {
        return Promise.resolve();
      }
      createMediaElementSource() {
        return { connect };
      }
      createAnalyser() {
        return node;
      }
    }
    vi.stubGlobal("AudioContext", Running);
    playSdrAudio();

    expect(sdrAnalyser()).toBe(node);
    expect(connect).toHaveBeenCalledWith(node);
    expect(connect).toHaveBeenCalledWith({ id: "speakers" });
    vi.unstubAllGlobals();
  });

  it("hands back the same analyser rather than tapping twice", () => {
    // createMediaElementSource throws on a second call for the same element.
    let taps = 0;
    const node = { fftSize: 0, smoothingTimeConstant: 0, connect: vi.fn() };
    class Running {
      state = "running";
      destination = {};
      resume() {
        return Promise.resolve();
      }
      createMediaElementSource() {
        taps += 1;
        return { connect: vi.fn() };
      }
      createAnalyser() {
        return node;
      }
    }
    vi.stubGlobal("AudioContext", Running);
    playSdrAudio();

    expect(sdrAnalyser()).toBe(sdrAnalyser());
    expect(taps).toBe(1);
    vi.unstubAllGlobals();
  });
});
