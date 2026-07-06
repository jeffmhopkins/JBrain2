import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { chunkSentences, speakableText, useReadAloud } from "./useReadAloud";

vi.mock("../api/client", () => ({ api: { getSettings: vi.fn() } }));
const getSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>;

const synth = { speak: vi.fn(), cancel: vi.fn() };

beforeEach(() => {
  getSettings.mockReset().mockResolvedValue({ brain_read_aloud: true });
  synth.speak.mockClear();
  synth.cancel.mockClear();
  Object.defineProperty(window, "speechSynthesis", { configurable: true, value: synth });
  // Minimal utterance stand-in — records the text the hook hands to the engine.
  class FakeUtterance {
    text: string;
    constructor(t: string) {
      this.text = t;
    }
  }
  Object.defineProperty(window, "SpeechSynthesisUtterance", {
    configurable: true,
    value: FakeUtterance,
  });
  localStorage.clear();
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

describe("useReadAloud", () => {
  it("is available only when the setting is on and the browser can speak", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));
  });

  it("is unavailable when the setting is off", async () => {
    getSettings.mockResolvedValue({ brain_read_aloud: false });
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(getSettings).toHaveBeenCalled());
    expect(result.current.available).toBe(false);
  });

  it("plays a turn's stripped text and marks it playing, cancelling any prior utterance", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.toggle("a", "**Hello** `world` [link](http://x)"));
    expect(synth.cancel).toHaveBeenCalled();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("Hello world link");
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

describe("chunkSentences", () => {
  it("splits complete sentences off the front, leaving a trailing partial", () => {
    const { chunks, consumed } = chunkSentences("Hello world. How are yo", false);
    expect(chunks).toEqual(["Hello world."]);
    expect("Hello world. How are yo".slice(consumed)).toBe("How are yo");
  });

  it("keeps a partial final sentence until flushed", () => {
    expect(chunkSentences("no terminator yet", false).chunks).toEqual([]);
    expect(chunkSentences("no terminator yet", true).chunks).toEqual(["no terminator yet"]);
  });

  it("does not split a decimal (terminator with no following space)", () => {
    expect(chunkSentences("pi is 3.14 exactly", true).chunks).toEqual(["pi is 3.14 exactly"]);
  });

  it("breaks on newlines and multiple terminators", () => {
    expect(chunkSentences("One!\nTwo? Three.", true).chunks).toEqual(["One!", "Two?", "Three."]);
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
      "And done",
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
});
