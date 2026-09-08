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
  ensureSdrAudioLive,
  isSdrPlaying,
  playSdrAudio,
  resetSdrAudio,
  sdrAnalyser,
  sdrAudioLag,
  sdrHeardAt,
  sdrLevels,
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

/** A context that counts taps and can be told what state to report. */
function ctxClass(state = "running") {
  const taps = { count: 0 };
  class Ctx {
    state = state;
    destination = {};
    resume() {
      this.state = "running";
      return Promise.resolve();
    }
    createMediaElementSource() {
      taps.count += 1;
      return { connect: vi.fn(), disconnect: vi.fn() };
    }
    createAnalyser() {
      return {
        fftSize: 2048,
        smoothingTimeConstant: 0,
        connect: vi.fn(),
        disconnect: vi.fn(),
        getByteTimeDomainData: vi.fn(),
      };
    }
  }
  return { Ctx, taps };
}

/** A stubbed AudioContext whose state the test can move afterwards — the real sequence
 *  is a tap taken while running, then an OS suspend. Returns a handle on the instance
 *  the module built, so the test can flip `state` the way an interruption does. */
function stubContext(resume: () => Promise<void>): { current: { state: string } | null } {
  const made: { current: { state: string } | null } = { current: null };
  class FakeContext {
    state = "running";
    destination = {};
    resume = resume;
    constructor() {
      made.current = this;
    }
    createMediaElementSource() {
      return { connect: vi.fn(), disconnect: vi.fn() };
    }
    createAnalyser() {
      return {
        fftSize: 2048,
        smoothingTimeConstant: 0,
        connect: vi.fn(),
        disconnect: vi.fn(),
      };
    }
  }
  vi.stubGlobal("AudioContext", FakeContext);
  return made;
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

describe("a tap that must not outlive its element", () => {
  it("takes the tap AGAIN for the element of the next lease", () => {
    // The waveform's oldest and worst failure, and it lasted a whole page rather than a
    // moment. `createMediaElementSource` may be called once per element, so the "already
    // tapped" latch is right — but it latched for the MODULE while meaning something
    // about ONE element. Release and listen again and the element is new, while the
    // analyser still pointed at the discarded one: it reports 128s, which is silence, so
    // the tape drew a flat line through audio the owner could hear perfectly.
    const { Ctx, taps } = ctxClass();
    vi.stubGlobal("AudioContext", Ctx);

    playSdrAudio();
    expect(sdrAnalyser()).not.toBeNull();
    expect(taps.count).toBe(1);

    stopSdrAudio();
    playSdrAudio();

    // A NEW element, so a new tap — not the cached node over a dead one.
    expect(sdrAnalyser()).not.toBeNull();
    expect(taps.count).toBe(2);
    vi.unstubAllGlobals();
  });

  it("still refuses to tap the same element twice", () => {
    // The guard the fix must not trade away.
    const { Ctx, taps } = ctxClass();
    vi.stubGlobal("AudioContext", Ctx);

    playSdrAudio();
    sdrAnalyser();
    sdrAnalyser();

    expect(taps.count).toBe(1);
    vi.unstubAllGlobals();
  });
});

describe("putting the supply back after the app was away", () => {
  it("resumes a context the OS suspended AFTER the tap was taken", () => {
    // The real sequence, and the one the sampler cannot cover: the tap is taken while
    // the context runs, then the phone locks or a call arrives and the OS suspends it.
    // A backgrounded app's timers are throttled to a crawl and frozen outright on iOS,
    // so the 20 Hz sampler — the one thing that would notice and recover — is asleep at
    // exactly the moment the context dies. Something has to run on the way back.
    const resume = vi.fn(() => Promise.resolve());
    const ctx = stubContext(resume);
    playSdrAudio();
    // The tap succeeds, so the analyser is cached and the re-tap branch is NOT the one
    // under test — this pins the resume itself.
    expect(sdrAnalyser()).not.toBeNull();
    resume.mockClear();
    if (ctx.current) ctx.current.state = "suspended";

    ensureSdrAudioLive();

    expect(resume).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("resumes an iOS context that says `interrupted` rather than `suspended`", () => {
    // A call, or another app taking the audio. This state used to fall straight through
    // the "suspended" test and was never resumed at all — and it is the state a phone
    // is most often in when the owner comes back to the app.
    const resume = vi.fn(() => Promise.resolve());
    const ctx = stubContext(resume);
    playSdrAudio();
    sdrAnalyser();
    resume.mockClear();
    if (ctx.current) ctx.current.state = "interrupted";

    ensureSdrAudioLive();

    expect(resume).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("leaves a CLOSED context alone", () => {
    // Closed is terminal: resuming it throws, and the recovery would become the fault.
    const resume = vi.fn(() => Promise.resolve());
    const ctx = stubContext(resume);
    playSdrAudio();
    sdrAnalyser();
    resume.mockClear();
    if (ctx.current) ctx.current.state = "closed";

    ensureSdrAudioLive();

    expect(resume).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("is safe to call when there is nothing wrong", () => {
    // It runs on pageshow, focus AND online, several of which fire for one resume, so
    // it has to be idempotent or it is a stampede.
    const { Ctx, taps } = ctxClass();
    vi.stubGlobal("AudioContext", Ctx);
    playSdrAudio();
    sdrAnalyser();

    ensureSdrAudioLive();
    ensureSdrAudioLive();
    ensureSdrAudioLive();

    expect(taps.count).toBe(1);
    vi.unstubAllGlobals();
  });

  it("tries again after a resume that never comes back", () => {
    // The latch that used to be a boolean. `sample()` writes NOTHING while a resume is
    // pending — recording a suspended analyser would put a measurement in the tape that
    // was never taken — so a promise that never settles froze the tape for the life of
    // the page. iOS leaves `resume()` pending indefinitely when the page is still
    // interrupted, which is exactly when this matters.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    const resume = vi.fn(() => new Promise<void>(() => {})); // never settles
    const ctx = stubContext(resume);
    playSdrAudio();
    sdrAnalyser();
    resume.mockClear();
    if (ctx.current) ctx.current.state = "suspended";

    ensureSdrAudioLive();
    expect(resume).toHaveBeenCalledTimes(1);

    // Straight away, the in-flight attempt still stands: one per tick would be a
    // stampede, which is what the latch is for.
    ensureSdrAudioLive();
    expect(resume).toHaveBeenCalledTimes(1);

    // Long enough that the attempt is not coming back. Trying again is the only way out.
    vi.setSystemTime(new Date("2026-01-01T00:00:05Z"));
    ensureSdrAudioLive();
    expect(resume).toHaveBeenCalledTimes(2);

    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("does nothing when no radio is playing", () => {
    expect(() => ensureSdrAudioLive()).not.toThrow();
  });
});

describe("the tape's history", () => {
  it("is recorded by the module, not by the sheet that draws it", () => {
    // The owner asked to open the tuner and see what already happened. If sampling
    // lived in the component, opening the sheet would START the recording and the
    // first thing they saw would always be an empty tape — the wrong answer on a
    // channel whose traffic arrives in bursts.
    const { levels, length } = sdrLevels();
    expect(length).toBeGreaterThan(0);
    expect(levels).toBeInstanceOf(Float32Array);
    expect(levels.length).toBe(length);
  });

  it("clears the history when the lease ends, not when the sheet closes", () => {
    const { levels } = sdrLevels();
    levels[0] = 0.9;
    playSdrAudio();

    stopSdrAudio(); // the radio was released

    // A new session starts from nothing; the previous station's audio is not its past.
    expect(sdrLevels().levels[0]).toBe(0);
    expect(sdrLevels().at).toBe(0);
  });
});

describe("a context that goes away under the tap", () => {
  /** A context that starts running, can be suspended, and counts resume attempts. */
  class Interruptible {
    static last: Interruptible | null = null;
    state = "running";
    resumes = 0;
    destination = { id: "speakers" };
    node = {
      fftSize: 8,
      smoothingTimeConstant: 0,
      connect: vi.fn(),
      // A live analyser hands back a real waveform; a suspended one hands back 128s,
      // which is silence — and that is the whole defect.
      getByteTimeDomainData: (out: Uint8Array) => {
        out.fill(this.state === "running" ? 200 : 128);
      },
    };
    constructor() {
      Interruptible.last = this;
    }
    resume() {
      this.resumes += 1;
      return Promise.resolve();
    }
    createMediaElementSource() {
      return { connect: vi.fn() };
    }
    createAnalyser() {
      return this.node;
    }
  }

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    stopSdrAudio();
  });

  it("keeps trying to resume, because the audio came back without it", () => {
    // `sdrAnalyser` resumes the context once and then returns the cached node for ever.
    // iOS suspends the context on any interruption, and the ELEMENT recovers on its own
    // because it reaches the speakers without the graph — so nothing was left to notice
    // the analyser had stopped. REPORTED as "the live waveform is intermittent, but the
    // spectrum is good".
    vi.useFakeTimers();
    vi.stubGlobal("AudioContext", Interruptible);
    playSdrAudio();
    pretendPlaying(element());
    expect(sdrAnalyser()).not.toBeNull();
    const ctx = Interruptible.last as Interruptible;
    const before = ctx.resumes;

    ctx.state = "suspended";
    vi.advanceTimersByTime(500);

    expect(ctx.resumes).toBeGreaterThan(before);
  });

  it("does not stack a resume per tick while one is in flight", () => {
    vi.useFakeTimers();
    vi.stubGlobal("AudioContext", Interruptible);
    playSdrAudio();
    pretendPlaying(element());
    sdrAnalyser();
    const ctx = Interruptible.last as Interruptible;
    const before = ctx.resumes;

    ctx.state = "suspended";
    vi.advanceTimersByTime(1000); // ~20 ticks at SAMPLE_HZ

    // One attempt, not twenty: a rejected resume must not become a promise per tick.
    expect(ctx.resumes - before).toBe(1);
  });

  it("records no level at all while it cannot hear, rather than recording silence", () => {
    // The tape's claim is "this is what came through", so a flat line means nothing
    // came through. A suspended analyser reports 128s; writing those puts a measurement
    // in the tape that was never taken — which is what drew a dead line through audio
    // the owner could hear.
    vi.useFakeTimers();
    vi.stubGlobal("AudioContext", Interruptible);
    playSdrAudio();
    pretendPlaying(element());
    sdrAnalyser();
    const ctx = Interruptible.last as Interruptible;

    vi.advanceTimersByTime(500); // running: real levels go in
    const heard = sdrLevels().at;
    expect(heard).toBeGreaterThan(0);

    ctx.state = "suspended";
    vi.advanceTimersByTime(1000);

    // The write pointer has not moved: no invented silence.
    expect(sdrLevels().at).toBe(heard);
  });
});

describe("how far behind the air the speaker is", () => {
  /** jsdom's play() is not implemented, so the refusal path — the one that matters
   *  here — is unreachable without saying what the browser did. */
  function playAnswers(with_: "yes" | "no"): void {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() =>
      with_ === "yes" ? Promise.resolve() : Promise.reject(new Error("NotAllowedError")),
    );
  }

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("drops the stream the browser refused, rather than buffering it unheard", async () => {
    // THE EIGHT SECONDS. `playSdrAudio` fires from the session poll, which is not a user
    // gesture, so a phone refuses it — and the refusal does not stop the LOAD. The
    // element went on pulling live audio into its buffer for as long as it took the
    // owner to reach the play button, and playback then started at the top of that
    // buffer and stayed exactly that far behind the air for the whole session.
    playAnswers("no");

    playSdrAudio(1000);
    await Promise.resolve();
    await Promise.resolve();

    expect(element()?.hasAttribute("src")).toBe(false);
    expect(isSdrPlaying()).toBe(false);
  });

  it("still anchors the stream the owner re-opened by hand", async () => {
    // The tap has no clock reading of its own — only the poll gets those — so before
    // this the reconnect anchored to null and threw the caption timeline away.
    vi.useFakeTimers();
    vi.setSystemTime(0);
    playAnswers("no");
    playSdrAudio(1000); // the poll: the box's clock reads 1000
    await vi.advanceTimersByTimeAsync(4000);

    playAnswers("yes");
    toggleSdrAudio(); // the owner's tap, four seconds later
    const el = element();
    pretendPlaying(el);
    Object.defineProperty(el as HTMLAudioElement, "currentTime", {
      value: 2,
      configurable: true,
    });

    // Anchored at 1004 — the box's clock carried forward — so two seconds in, the ear
    // is at 1006. Anchoring to the stale 1000 would put every caption four seconds out.
    expect(sdrHeardAt()).toBeCloseTo(1006, 3);
  });

  it("measures the delay rather than asserting it", () => {
    // "~8.3 s" lived in a comment for months with nothing on screen able to contradict
    // it, and the owner has no terminal to measure from (CLAUDE.md #10).
    playAnswers("yes");
    playSdrAudio(1000);
    const el = element() as HTMLAudioElement;
    pretendPlaying(el);
    Object.defineProperty(el, "buffered", {
      value: { length: 1, end: () => 9.2 },
      configurable: true,
    });
    Object.defineProperty(el, "currentTime", { value: 1, configurable: true });

    expect(sdrAudioLag()).toBeCloseTo(8.2, 3);
  });

  it("reports no delay at all when nothing is playing", () => {
    // A paused element's buffer is not a measurement of anything the owner can hear.
    playAnswers("yes");
    playSdrAudio(1000);

    expect(sdrAudioLag()).toBeNull();
  });
});

describe("keeping playback near the air", () => {
  /** An element whose lag we can set: `<audio>` gives no way to ask for a short buffer,
   *  so the only lever is what `buffered.end` and `currentTime` say. */
  function lagOf(el: HTMLAudioElement, seconds: number, played = 10): void {
    Object.defineProperty(el, "paused", { value: false, configurable: true });
    Object.defineProperty(el, "currentTime", { value: played, configurable: true });
    Object.defineProperty(el, "buffered", {
      value: { length: 1, end: () => played + seconds },
      configurable: true,
    });
  }

  function start(): HTMLAudioElement {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    playSdrAudio(1000);
    const el = element();
    if (!el) throw new Error("no audio element");
    return el;
  }

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("does not fight the buffer the browser chose for itself", () => {
    // The reason this measures DRIFT from the stream's own floor and not an absolute
    // ceiling. A browser that always buffers three and a half seconds before it starts
    // is not LATE — that is simply what it does — and a fixed ceiling below whatever it
    // picked would reconnect for ever, putting a gap in the audio every few seconds to
    // chase a delay that was never going to go away.
    //
    // Deliberately WIDER than DRIFT_S, which is what makes this a test of the floor
    // rather than of the threshold: a ceiling at DRIFT_S would tear this stream down.
    vi.useFakeTimers();
    const el = start();
    lagOf(el, 3.5);
    const load = vi.spyOn(el, "load");

    vi.advanceTimersByTime(30_000);

    expect(load).not.toHaveBeenCalled();
    expect(sdrAudioLag()).toBeCloseTo(3.5, 3);
  });

  it("rejoins the live edge once playback falls behind where it was", () => {
    // A stall — a lock screen, a lost second of network — is paid back by `<audio>` as
    // PERMANENT delay: it plays what it has at 1x and nothing ever catches it up.
    // Reconnecting is the one move that does, because MP3 has no header and the sidecar
    // simply starts sending from wherever the air is now.
    vi.useFakeTimers();
    const el = start();
    lagOf(el, 0.4);
    vi.advanceTimersByTime(2000); // the floor is established at 0.4 s

    const load = vi.spyOn(el, "load");
    lagOf(el, 5.0); // the stall
    vi.advanceTimersByTime(2000);

    expect(load).toHaveBeenCalled();
    expect(el.getAttribute("src")).toContain(SDR_AUDIO_SRC);
  });

  it("re-anchors the timeline it just moved", () => {
    // Position zero is a NEW moment on the box's clock after a rejoin. Keeping the old
    // anchor would put every caption — and every spectrum row, which is aligned the
    // same way — out by the whole of the drift that was just corrected.
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const el = start();
    lagOf(el, 0.4);
    vi.advanceTimersByTime(2000);

    lagOf(el, 5.0);
    vi.advanceTimersByTime(2000);
    Object.defineProperty(el, "currentTime", { value: 0, configurable: true });

    // The drift appears at t=2 s and the watchdog checks once a second, so the rejoin
    // lands at t=3: the fresh stream's position zero is the air at 1003, not the stale
    // 1000 the first attach recorded.
    expect(sdrHeardAt()).toBeCloseTo(1003, 1);
  });

  it("does not stutter its way through a link that keeps losing ground", () => {
    // Each rejoin costs an audible gap. A connection falling further behind every
    // second would otherwise be answered with a gap every second — the stutter being
    // worse than the delay it is chasing, and no nearer to fixing it.
    //
    // A GROWING lag, because a steady one is already handled by the floor resetting
    // after a rejoin; only a climbing one reaches the rate limit at all.
    vi.useFakeTimers();
    const el = start();
    let behind = 0.4;
    Object.defineProperty(el, "paused", { value: false, configurable: true });
    Object.defineProperty(el, "currentTime", { value: 10, configurable: true });
    Object.defineProperty(el, "buffered", {
      value: { length: 1, end: () => 10 + behind },
      configurable: true,
    });
    vi.advanceTimersByTime(2000); // the floor is established at 0.4 s
    const load = vi.spyOn(el, "load");

    for (let second = 0; second < 12; second += 1) {
      behind += 3;
      vi.advanceTimersByTime(1000);
    }

    expect(load).toHaveBeenCalledTimes(1);
  });
});
