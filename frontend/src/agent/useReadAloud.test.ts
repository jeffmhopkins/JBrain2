import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { chunkForTts, speakableText, useReadAloud } from "./useReadAloud";

vi.mock("../api/client", () => ({
  api: { getSettings: vi.fn(), brainVoices: vi.fn(), brainTts: vi.fn() },
}));
const getSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>;
const brainVoices = api.brainVoices as unknown as ReturnType<typeof vi.fn>;
const brainTts = api.brainTts as unknown as ReturnType<typeof vi.fn>;

// A stand-in <audio> that records its src and lets a test fire onended/onerror. Each new
// Audio() is captured so a test can drive the chunk-chaining playback.
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

// The browser's native voice — off by default (jsdom has none), turned on per-test via
// stubNative() so the piper tests exercise the piper path without a native fallback.
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

beforeEach(() => {
  audios.length = 0;
  synth.speak.mockClear();
  synth.cancel.mockClear();
  Reflect.deleteProperty(window, "speechSynthesis"); // native absent unless a test opts in
  getSettings.mockReset().mockResolvedValue({
    brain_read_aloud: true,
    brain_answer_voice: "en_US-libritts_r-medium#3922",
    brain_read_aloud_engine: "piper",
  });
  brainVoices.mockReset().mockResolvedValue(["en_US-amy-medium", "en_US-libritts_r-medium#3922"]);
  brainTts.mockReset().mockResolvedValue(new Blob(["wav"], { type: "audio/wav" }));
  Object.defineProperty(window, "Audio", { configurable: true, value: FakeAudio });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:x" });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
});

describe("speakableText", () => {
  it("strips markdown down to plain prose", () => {
    const out = speakableText(
      "# Heading\n\n- item **bold**\n\n`code`\n\n> quote [^1] [x](http://y)",
    );
    expect(out).toContain("Heading");
    expect(out).toContain("item bold");
    expect(out).toContain("quote");
    expect(out).toContain("x"); // a link keeps its text
    expect(out).not.toMatch(/[#>*`[\]]/); // no markdown syntax remains
    expect(out).not.toContain("http"); // the link URL is dropped
  });
});

describe("chunkForTts", () => {
  it("splits long prose on sentence boundaries under the cap", () => {
    const text = `${"a".repeat(60)}. ${"b".repeat(60)}. ${"c".repeat(60)}.`;
    const chunks = chunkForTts(text, 100);
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(100);
    expect(chunks.join(" ")).toContain("aaaa");
    expect(chunks.join(" ")).toContain("cccc");
  });

  it("hard-splits a single sentence longer than the cap", () => {
    const chunks = chunkForTts("x".repeat(250), 100);
    expect(chunks.every((c) => c.length <= 100)).toBe(true);
    expect(chunks.join("").length).toBe(250);
  });
});

describe("useReadAloud", () => {
  it("is available only when the setting is on and the box has voices", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
  });

  it("is unavailable when the setting is off", async () => {
    getSettings.mockResolvedValue({
      brain_read_aloud: false,
      brain_answer_voice: "en_US-amy-medium",
    });
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(getSettings).toHaveBeenCalled());
    await waitFor(() => expect(result.current.available).toBe(false));
  });

  it("is unavailable when the box has no voices and this device can't speak", async () => {
    brainVoices.mockResolvedValue([]); // no piper, and stubNative() not called -> no native
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(brainVoices).toHaveBeenCalled());
    await waitFor(() => expect(result.current.available).toBe(false));
  });

  it("stays available on the native fallback when the box has no voices", async () => {
    stubNative();
    brainVoices.mockResolvedValue([]);
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "read me natively"));
    // No piper voices -> falls straight to the device voice.
    expect(brainTts).not.toHaveBeenCalled();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("read me natively");
    expect(result.current.playing).toBe("a");
  });

  it("falls back to the native voice when a piper render fails", async () => {
    stubNative();
    brainTts.mockRejectedValue(new Error("box unreachable"));
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "read me"));
    expect(brainTts).toHaveBeenCalledTimes(1); // tried piper first
    expect(synth.speak).toHaveBeenCalledTimes(1); // then the device voice
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("read me");
  });

  it("always uses the device voice when the engine is native", async () => {
    stubNative();
    getSettings.mockResolvedValue({
      brain_read_aloud: true,
      brain_answer_voice: "en_US-amy-medium",
      brain_read_aloud_engine: "native",
    });
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "spoken locally"));
    expect(brainTts).not.toHaveBeenCalled(); // never touches the box in native mode
    expect(synth.speak).toHaveBeenCalledTimes(1);
  });

  it("renders a turn through piper in the configured voice and marks it playing", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => {
      result.current.toggle("a", "**Hello** `world` [link](http://x)");
    });
    expect(brainTts).toHaveBeenCalledWith(
      "en_US-libritts_r-medium#3922",
      "Hello world link",
      undefined,
    );
    expect(audios).toHaveLength(1);
    expect(audios[0]?.played).toBe(true);
    expect(result.current.playing).toBe("a");
  });

  it("does not play empty/whitespace-only content", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => {
      result.current.toggle("a", "   \n  ");
    });
    expect(brainTts).not.toHaveBeenCalled();
    expect(result.current.playing).toBeNull();
  });

  it("pauses the turn already playing when toggled again", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "read me"));
    expect(result.current.playing).toBe("a");
    act(() => result.current.toggle("a", "read me")); // tap again -> pause
    expect(result.current.playing).toBeNull();
  });

  it("switches playback to another turn, stopping the first", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "first"));
    expect(result.current.playing).toBe("a");
    await act(async () => result.current.toggle("b", "second"));
    expect(result.current.playing).toBe("b");
    expect(brainTts).toHaveBeenCalledTimes(2);
  });

  it("clears the playing state once the final clip finishes on its own", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
    await act(async () => result.current.toggle("a", "read me"));
    expect(result.current.playing).toBe("a");
    await act(async () => {
      audios[0]?.onended?.(); // single clip finishes
    });
    expect(result.current.playing).toBeNull();
  });
});
