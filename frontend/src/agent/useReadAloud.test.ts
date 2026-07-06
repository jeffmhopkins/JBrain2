import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { speakableText, useReadAloud } from "./useReadAloud";

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
