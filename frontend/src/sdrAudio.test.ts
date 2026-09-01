// The radio's audio belongs to the LEASE, not to the tuner sheet.
//
// Every case here is a regression: shipped, the element was created and destroyed by
// the sheet, so the radio only made sound while the owner had the controls open —
// closing them silenced a session that was still holding the tuner.

import { afterEach, describe, expect, it } from "vitest";
import {
  SDR_AUDIO_SRC,
  attachSdrAudio,
  playSdrAudio,
  resetSdrAudio,
  stopSdrAudio,
} from "./sdrAudio";

afterEach(() => resetSdrAudio());

function element(): HTMLAudioElement | null {
  return document.querySelector("audio");
}

describe("the radio's audio element", () => {
  it("plays from the proxied stream, not the sidecar", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    playSdrAudio();
    attachSdrAudio(host);

    // Same-origin and owner-session-authed; the sidecar is on a network the browser
    // has no route to, so a direct URL here would simply not resolve.
    expect(element()?.src).toContain(SDR_AUDIO_SRC);
    host.remove();
  });

  it("keeps playing when the sheet unmounts", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    playSdrAudio();
    const detach = attachSdrAudio(host);
    const el = element();

    detach(); // the sheet closed

    // Out of the document but NOT torn down: a media element with a live reference
    // goes on playing, which is exactly what makes the lease outlive the sheet.
    expect(host.querySelector("audio")).toBeNull();
    expect(el?.src).toContain(SDR_AUDIO_SRC);
    expect(el?.getAttribute("src")).not.toBeNull();
    host.remove();
  });

  it("hands the same element back, so reopening does not restart the stream", () => {
    const first = document.createElement("div");
    const second = document.createElement("div");
    document.body.append(first, second);
    playSdrAudio();

    const detach = attachSdrAudio(first);
    const before = first.querySelector("audio");
    detach();
    attachSdrAudio(second);

    // A new element would mean a new request and a fresh buffering delay every time
    // the owner glanced at the tuner.
    expect(second.querySelector("audio")).toBe(before);
    first.remove();
    second.remove();
  });

  it("drops the stream only when the lease ends", () => {
    playSdrAudio();
    const host = document.createElement("div");
    document.body.appendChild(host);
    attachSdrAudio(host);

    stopSdrAudio();

    // Releasing the radio must not leave a connection open pulling audio nobody hears.
    expect(host.querySelector("audio")).toBeNull();
    host.remove();
  });
});

describe("when the browser refuses to play", () => {
  it("does not let the failure escape into the session store", () => {
    // playSdrAudio runs inside sdrSession's publish(), on a one-second poll. An
    // exception escaping it would stop every listener being notified and freeze the
    // composer icon on a stale reading — a dead UI caused by a muted speaker.
    const host = document.createElement("div");
    document.body.appendChild(host);
    playSdrAudio();
    attachSdrAudio(host);
    const el = element();
    if (el) {
      Object.defineProperty(el, "paused", { value: true, configurable: true });
      el.play = () => {
        throw new Error("NotAllowedError");
      };
    }

    expect(() => playSdrAudio()).not.toThrow();
    host.remove();
  });
});
