import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { emitReadAloudSettings } from "./readAloudBus";
import { useReadAloud } from "./useReadAloud";

vi.mock("../api/client", () => ({
  api: { getSettings: vi.fn(), brainVoices: vi.fn(), brainTts: vi.fn() },
}));
const getSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>;
const brainVoices = api.brainVoices as unknown as ReturnType<typeof vi.fn>;
const brainTts = api.brainTts as unknown as ReturnType<typeof vi.fn>;

// The native (Web Speech) engine stand-ins — records the text handed to the engine.
const synth = { speak: vi.fn(), cancel: vi.fn() };
class FakeUtterance {
  text: string;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(t: string) {
    this.text = t;
  }
}
function stubNative() {
  Object.defineProperty(window, "speechSynthesis", { configurable: true, value: synth });
  Object.defineProperty(window, "SpeechSynthesisUtterance", {
    configurable: true,
    value: FakeUtterance,
  });
}

// The piper (box) engine stand-in — each new Audio() is captured so a test can fire
// onended to advance the sequential clip playback.
const audios: FakeAudio[] = [];
class FakeAudio {
  src: string;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  played = false;
  constructor(src: string) {
    this.src = src;
    audios.push(this);
  }
  play() {
    this.played = true;
    return Promise.resolve();
  }
  pause() {}
}

beforeEach(() => {
  audios.length = 0;
  synth.speak.mockClear();
  synth.cancel.mockClear();
  // Default: native engine, no box voices — the native path, as the pre-piper hook behaved.
  getSettings.mockReset().mockResolvedValue({
    brain_read_aloud: true,
    brain_answer_voice: "kokoro-af_heart",
    brain_read_aloud_engine: "native",
  });
  brainVoices.mockReset().mockResolvedValue([]);
  brainTts.mockReset().mockResolvedValue(new Blob(["wav"], { type: "audio/wav" }));
  stubNative();
  Object.defineProperty(window, "Audio", { configurable: true, value: FakeAudio });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:x" });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
  localStorage.clear();
});

describe("useReadAloud", () => {
  it("is available only when the setting is on and the browser can speak", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
  });

  it("retries a failed settings read so one dropped request does not hide the control", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      getSettings.mockRejectedValueOnce(new Error("tunnel blip"));
      const { result } = renderHook(() => useReadAloud());
      await act(async () => {
        await Promise.resolve();
      });
      expect(result.current.available).toBe(false);
      await act(async () => {
        vi.advanceTimersByTime(5_000);
        await Promise.resolve();
      });
      vi.useRealTimers();
      await waitFor(() => expect(result.current.available).toBe(true));
      expect(getSettings).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("is unavailable when the setting is off", async () => {
    getSettings.mockResolvedValue({ brain_read_aloud: false });
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(getSettings).toHaveBeenCalled());
    await waitFor(() => expect(result.current.available).toBe(false));
  });

  it("plays a turn's stripped text and marks it playing, cancelling any prior utterance", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("a", "**Hello** `world` [link](http://x)"));
    expect(synth.cancel).toHaveBeenCalled();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("Hello world link.");
    expect(result.current.playing).toBe("a");
  });

  it("does not play empty/whitespace-only content", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("a", "   \n  "));
    expect(synth.speak).not.toHaveBeenCalled();
    expect(result.current.playing).toBeNull();
  });

  it("pauses the turn already playing when toggled again", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("a", "read me"));
    expect(result.current.playing).toBe("a");

    synth.cancel.mockClear();
    act(() => result.current.toggle("a", "read me")); // tap again -> pause
    expect(synth.cancel).toHaveBeenCalled();
    expect(result.current.playing).toBeNull();
  });

  it("switches playback to another turn, stopping the first", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("a", "first"));
    expect(result.current.playing).toBe("a");

    act(() => result.current.toggle("b", "second"));
    expect(synth.speak).toHaveBeenCalledTimes(2);
    expect(result.current.playing).toBe("b");
  });

  it("clears the playing state once the utterance finishes on its own", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("a", "read me"));
    const utt = synth.speak.mock.calls[0]?.[0];
    expect(result.current.playing).toBe("a");

    act(() => utt.onend?.());
    expect(result.current.playing).toBeNull();
  });
});

describe("useReadAloud auto-play", () => {
  it("persists the auto-play mode across the localStorage key", () => {
    const { result } = renderHook(() => useReadAloud());
    expect(result.current.autoPlay).toBe(false);
    act(() => result.current.toggleAutoPlay());
    expect(result.current.autoPlay).toBe(true);
    expect(localStorage.getItem("readAloudAutoPlay")).toBe("1");

    act(() => result.current.toggleAutoPlay());
    expect(result.current.autoPlay).toBe(false);
    expect(localStorage.getItem("readAloudAutoPlay")).toBe("0");
  });

  it("restores auto-play from localStorage on mount", () => {
    localStorage.setItem("readAloudAutoPlay", "1");
    const { result } = renderHook(() => useReadAloud());
    expect(result.current.autoPlay).toBe(true);
  });

  it("feeds a streaming turn sentence-by-sentence, flushing the tail on done", () => {
    const { result } = renderHook(() => useReadAloud());
    // First delta carries one complete sentence + a partial — only the complete one speaks.
    act(() => result.current.feed("1", "The sky is blue. Grass is", false));
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual(["The sky is blue."]);
    expect(result.current.playing).toBe("1");

    // More text arrives; the next complete sentence speaks (no re-speak of the first).
    act(() => result.current.feed("1", "The sky is blue. Grass is green. And", false));
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual([
      "The sky is blue.",
      "Grass is green.",
    ]);

    // Settle flushes the remaining partial sentence.
    act(() => result.current.feed("1", "The sky is blue. Grass is green. And done", true));
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual([
      "The sky is blue.",
      "Grass is green.",
      "And done.", // pause-authoring gives the punctuation-less tail a terminal period
    ]);
  });

  it("clears playing once the final fed chunk finishes", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.feed("1", "All at once.", false)); // begin streaming this turn
    act(() => result.current.feed("1", "All at once.", true)); // settle flushes the sentence
    expect(result.current.playing).toBe("1");
    const last = synth.speak.mock.calls.at(-1)?.[0];
    expect(last.text).toBe("All at once.");
    act(() => last.onend?.());
    expect(result.current.playing).toBeNull();
  });

  it("never auto-starts an already-settled turn", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.feed("1", "Settled already.", true));
    expect(synth.speak).not.toHaveBeenCalled();
    expect(result.current.playing).toBeNull();
  });

  it("does not resume a turn the owner paused mid-stream", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.feed("1", "First sentence.", false));
    expect(result.current.playing).toBe("1");

    // Owner taps pause on the still-streaming turn.
    act(() => result.current.toggle("1", "First sentence."));
    expect(result.current.playing).toBeNull();

    // More text streams in — it must stay silent (suppressed), not resume.
    synth.speak.mockClear();
    act(() => result.current.feed("1", "First sentence. Second sentence.", false));
    expect(synth.speak).not.toHaveBeenCalled();
    expect(result.current.playing).toBeNull();
  });

  it("cuts off the current auto turn when a new turn starts streaming", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.feed("1", "Older turn still talking.", false));
    expect(result.current.playing).toBe("1");

    // The next agent turn begins streaming — cut the old stream, take over.
    synth.cancel.mockClear();
    act(() => result.current.feed("3", "Newer turn.", false));
    expect(synth.cancel).toHaveBeenCalled();
    expect(result.current.playing).toBe("3");
  });

  it("a new streaming turn cuts off a manual playback", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("1", "Manually reading this long answer aloud."));
    expect(result.current.playing).toBe("1");

    // A fresh turn streams in — it wins over the manual playback.
    synth.cancel.mockClear();
    act(() => result.current.feed("3", "New turn.", false));
    expect(synth.cancel).toHaveBeenCalled();
    expect(result.current.playing).toBe("3");
  });
});

describe("useReadAloud piper engine", () => {
  // In this block the box has piper voices and native is absent by default, so `available`
  // reflects the box — awaiting it guarantees the voices have loaded before we toggle.
  beforeEach(() => {
    Reflect.deleteProperty(window, "speechSynthesis");
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "kokoro-am_michael",
      brain_read_aloud_engine: "piper",
    });
    brainVoices.mockResolvedValue(["kokoro-af_heart", "kokoro-am_michael"]);
  });

  it("is available when the setting is on and the box has voices", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
  });

  it("renders a turn through piper in the configured voice and marks it playing", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "**Hello** `world` [link](http://x)"));
    await waitFor(() => expect(audios).toHaveLength(1));
    expect(brainTts).toHaveBeenCalledWith(
      "kokoro-am_michael",
      "Hello world link.",
      undefined,
      1, // markup turn: default speed 1.0× × no prose nudge
      undefined, // markup turn → no trail override
      { pitch: 0, chorus: false, robot: false }, // effects all default (off)
    );
    expect(audios[0]?.played).toBe(true);
    expect(result.current.playing).toBe("a");
  });

  it("uses a voice changed live in the Settings overlay for the next turn", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    // HomeScreen stays mounted, so the change arrives over the read-aloud bus, not a re-fetch.
    act(() => emitReadAloudSettings({ brain_answer_voice: "kokoro-af_heart" }));
    await act(async () => result.current.toggle("a", "hi"));
    await waitFor(() => expect(brainTts).toHaveBeenCalled());
    expect(brainTts.mock.calls[0]?.[0]).toBe("kokoro-af_heart"); // not the mount-time voice
  });

  it("applies the owner's voice effects (speed/pitch/chorus/robot) to piper renders", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    // Saved live in the Settings overlay → arrives over the bus and rides the next render.
    act(() =>
      emitReadAloudSettings({
        brain_answer_speed: 1.5,
        brain_answer_pitch: -4,
        brain_answer_chorus: true,
        brain_answer_robot: true,
      }),
    );
    await act(async () => result.current.toggle("a", "**Bold** `code` [x](http://y)"));
    await waitFor(() => expect(brainTts).toHaveBeenCalled());
    const call = brainTts.mock.calls[0];
    expect(call?.[3]).toBe(1.5); // the chosen speed (markup turn → no prose nudge)
    expect(call?.[5]).toEqual({ pitch: -4, chorus: true, robot: true });
  });

  it("reads a prose turn slower with a trailing beat (automatic audiobook pacing)", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    // Plain prose (no markdown structure) → the prose profile drives speed + inter-clip trail.
    await act(async () => result.current.toggle("p", "The wind rose. She waited by the door."));
    await waitFor(() => expect(brainTts).toHaveBeenCalled());
    const call = brainTts.mock.calls[0];
    expect(call?.[3]).toBe(0.9); // speed
    expect(call?.[4]).toBe(220); // trail ms
  });

  it("streams piper clips per sentence when fed, prefetching then playing them in order", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.feed("1", "One. Two. ", false));
    // Prefetch: both clips RENDER up front (the second overlaps the first's playback for
    // gapless audio), but only the head clip PLAYS until it finishes.
    await waitFor(() => expect(brainTts).toHaveBeenCalledTimes(2));
    expect(brainTts.mock.calls[0]?.[1]).toBe("One.");
    expect(brainTts.mock.calls[0]?.[2]).toBeUndefined(); // first clip carries the lead
    expect(brainTts.mock.calls[1]?.[1]).toBe("Two.");
    expect(brainTts.mock.calls[1]?.[2]).toBe(0); // continuation clip, no lead
    expect(audios).toHaveLength(1);
    expect(result.current.playing).toBe("1");
    // First clip finishes -> the already-rendered "Two." plays immediately (no fetch gap).
    await act(async () => audios[0]?.onended?.());
    await waitFor(() => expect(audios).toHaveLength(2));
    expect(brainTts).toHaveBeenCalledTimes(2); // not re-fetched — the prefetch is reused
  });

  it("clears playing once the final piper clip finishes", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "read me"));
    await waitFor(() => expect(audios).toHaveLength(1));
    expect(result.current.playing).toBe("a");
    await act(async () => {
      audios[0]?.onended?.();
    });
    expect(result.current.playing).toBeNull();
  });

  it("is unavailable when the box has no voices and this device can't speak", async () => {
    brainVoices.mockResolvedValue([]); // no piper, native already absent in this block
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(brainVoices).toHaveBeenCalled());
    await waitFor(() => expect(result.current.available).toBe(false));
  });

  it("stays available on the native fallback when the box has no voices", async () => {
    brainVoices.mockResolvedValue([]);
    stubNative();
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "read me natively"));
    // No piper voices -> falls straight to the device voice.
    expect(brainTts).not.toHaveBeenCalled();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("read me natively.");
    expect(result.current.playing).toBe("a");
  });

  it("falls back to the native voice when a piper render fails", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true)); // voices loaded (no native yet)
    brainTts.mockRejectedValue(new Error("box unreachable"));
    stubNative();
    await act(async () => result.current.toggle("a", "read me"));
    await waitFor(() => expect(synth.speak).toHaveBeenCalledTimes(1));
    expect(brainTts).toHaveBeenCalledTimes(1); // tried piper first
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("read me."); // then the device voice
  });

  it("uses the device voice when the engine is native even with box voices", async () => {
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "kokoro-af_heart",
      brain_read_aloud_engine: "native",
    });
    stubNative();
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "spoken locally"));
    expect(brainTts).not.toHaveBeenCalled(); // native mode never touches the box
    expect(synth.speak).toHaveBeenCalledTimes(1);
  });
});

describe("useReadAloud story-by-story (report player)", () => {
  it("speaks each story from the start index and tracks which one is playing", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["First story.", "Second story.", "Third."], 1));
    // Native voices each story as one utterance, starting at the chosen one.
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual(["Second story.", "Third."]);
    expect(result.current.playing).toBe("r");
    expect(result.current.progress).toEqual({ key: "r", section: 1, fraction: 0 });

    act(() => synth.speak.mock.calls[0]?.[0].onend());
    expect(result.current.progress).toEqual({ key: "r", section: 2, fraction: 0 });

    act(() => synth.speak.mock.calls[1]?.[0].onend());
    // The brief ran out: playback clears but the place pins to the last story, fully done.
    expect(result.current.playing).toBeNull();
    expect(result.current.progress).toEqual({ key: "r", section: 2, fraction: 1 });
  });

  it("keeps its place when paused, and a plain turn doesn't move it", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["One.", "Two."], 1));
    act(() => result.current.toggle("r", ""));
    expect(result.current.playing).toBeNull();
    expect(result.current.progress).toEqual({ key: "r", section: 1, fraction: 0 });

    act(() => result.current.toggle("turn", "Something else."));
    act(() => synth.speak.mock.calls.at(-1)?.[0].onend());
    expect(result.current.progress).toEqual({ key: "r", section: 1, fraction: 0 });
  });

  it("resumes a paused native story from the unfinished sentence, not its top", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["One. Two. Three.", "Four."], 0));
    act(() => synth.speak.mock.calls[0]?.[0].onend());
    act(() => result.current.toggle("r", ""));
    expect(result.current.playing).toBeNull();
    synth.speak.mockClear();

    act(() => result.current.playSections("r", ["One. Two. Three.", "Four."], 0, true));
    expect(result.current.playing).toBe("r");
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual(["Two.", "Three.", "Four."]);
    expect(result.current.progress).toEqual({ key: "r", section: 0, fraction: 1 / 3 });
  });

  it("doesn't overshoot the resume point when cancel() fires end synchronously", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["One. Two. Three."], 0));
    act(() => synth.speak.mock.calls[0]?.[0].onend());
    // Some engines end every queued utterance inside cancel().
    synth.cancel.mockImplementationOnce(() => {
      for (const c of synth.speak.mock.calls.slice(1)) c[0].onend?.();
    });
    act(() => result.current.toggle("r", ""));
    synth.speak.mockClear();
    act(() => result.current.playSections("r", ["One. Two. Three."], 0, true));
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual(["Two.", "Three."]);
  });

  it("ignores a cancelled utterance's late end once the run has resumed", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["One. Two. Three."], 0));
    const stale = synth.speak.mock.calls.map((c) => c[0]);
    act(() => result.current.toggle("r", ""));
    act(() => result.current.playSections("r", ["One. Two. Three."], 0, true));
    // Chrome delivers the cancelled utterances' errors after cancel() returns.
    act(() => {
      for (const u of stale) u.onerror?.();
    });
    expect(result.current.playing).toBe("r");
    expect(result.current.progress).toEqual({ key: "r", section: 0, fraction: 0 });
  });

  it("restarts the story when replayed without resume", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["One. Two.", "Three."], 0));
    act(() => synth.speak.mock.calls[0]?.[0].onend());
    act(() => result.current.toggle("r", ""));
    synth.speak.mockClear();
    act(() => result.current.playSections("r", ["One. Two.", "Three."], 0));
    expect(synth.speak.mock.calls.map((c) => c[0].text)).toEqual(["One.", "Two.", "Three."]);
  });

  it("pauses a piper story in place and resumes the same clip mid-sentence", async () => {
    Reflect.deleteProperty(window, "speechSynthesis");
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "kokoro-am_michael",
      brain_read_aloud_engine: "piper",
    });
    brainVoices.mockResolvedValue(["kokoro-am_michael"]);
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.playSections("r", ["One. Two.", "Three."], 0));
    await waitFor(() => expect(audios).toHaveLength(1));
    const clip = audios[0] as FakeAudio;
    const pause = vi.spyOn(clip, "pause");
    const play = vi.spyOn(clip, "play");

    act(() => result.current.toggle("r", ""));
    expect(result.current.playing).toBeNull();
    expect(pause).toHaveBeenCalled();
    const renders = brainTts.mock.calls.length;

    await act(async () => result.current.playSections("r", ["One. Two.", "Three."], 0, true));
    expect(result.current.playing).toBe("r");
    expect(play).toHaveBeenCalled();
    expect(audios).toHaveLength(1); // the held clip carries on — nothing re-rendered
    expect(brainTts.mock.calls.length).toBe(renders);

    await act(async () => clip.onended?.());
    await waitFor(() => expect(audios).toHaveLength(2));
    expect(result.current.progress).toEqual({ key: "r", section: 0, fraction: 0.5 });
  });

  it("holds the next piper clip while paused between clips", async () => {
    Reflect.deleteProperty(window, "speechSynthesis");
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "kokoro-am_michael",
      brain_read_aloud_engine: "piper",
    });
    brainVoices.mockResolvedValue(["kokoro-am_michael"]);
    let release: (b: Blob) => void = () => {};
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    // The second clip's render is still in flight when the owner pauses.
    brainTts.mockResolvedValueOnce(new Blob(["wav"])).mockImplementationOnce(
      () =>
        new Promise<Blob>((r) => {
          release = r;
        }),
    );
    await act(async () => result.current.playSections("r", ["One. Two."], 0));
    await waitFor(() => expect(audios).toHaveLength(1));
    await act(async () => (audios[0] as FakeAudio).onended?.());
    act(() => result.current.toggle("r", ""));
    await act(async () => release(new Blob(["wav"])));
    expect(audios).toHaveLength(1);

    await act(async () => result.current.playSections("r", ["One. Two."], 0, true));
    await waitFor(() => expect(audios).toHaveLength(2));
    expect(result.current.playing).toBe("r");
  });

  it("stays silent when the box fails while paused, and resumes from that clip", async () => {
    Reflect.deleteProperty(window, "speechSynthesis");
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "kokoro-am_michael",
      brain_read_aloud_engine: "piper",
    });
    brainVoices.mockResolvedValue(["kokoro-am_michael"]);
    let fail: (e: Error) => void = () => {};
    brainTts.mockImplementationOnce(
      () =>
        new Promise<Blob>((_, rej) => {
          fail = rej;
        }),
    );
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.playSections("r", ["One. Two."], 0));
    act(() => result.current.toggle("r", ""));
    stubNative();
    await act(async () => fail(new Error("box down")));
    expect(synth.speak).not.toHaveBeenCalled();
    expect(result.current.playing).toBeNull();

    await act(async () => result.current.playSections("r", ["One. Two."], 0, true));
    expect(result.current.playing).toBe("r");
    await waitFor(() => expect(audios).toHaveLength(1));
    expect(brainTts.mock.calls.at(-2)?.[1]).toBe("One.");
  });

  it("plays nothing when every story from the start index is empty", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.playSections("r", ["One.", "  "], 1));
    expect(synth.speak).not.toHaveBeenCalled();
    expect(result.current.playing).toBeNull();
    expect(result.current.progress).toBeNull();
  });

  it("measures a piper story's progress by its rendered clips", async () => {
    Reflect.deleteProperty(window, "speechSynthesis");
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "kokoro-am_michael",
      brain_read_aloud_engine: "piper",
    });
    brainVoices.mockResolvedValue(["kokoro-am_michael"]);
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.playSections("r", ["One. Two.", "Three."], 0));
    await waitFor(() => expect(audios).toHaveLength(1));
    expect(result.current.progress).toEqual({ key: "r", section: 0, fraction: 0 });
    await act(async () => audios[0]?.onended?.());
    expect(result.current.progress).toEqual({ key: "r", section: 0, fraction: 0.5 });
    await waitFor(() => expect(audios).toHaveLength(2));
    await act(async () => audios[1]?.onended?.());
    expect(result.current.progress).toEqual({ key: "r", section: 1, fraction: 0 });
  });
});
