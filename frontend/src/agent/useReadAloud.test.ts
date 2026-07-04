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

  it("speaks a turn's stripped text, cancelling any prior utterance first", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.speak("**Hello** `world` [link](http://x)"));
    expect(synth.cancel).toHaveBeenCalled();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0]?.[0].text).toBe("Hello world link");
  });

  it("does not speak empty/whitespace-only content", () => {
    const { result } = renderHook(() => useReadAloud());
    act(() => result.current.speak("   \n  "));
    expect(synth.speak).not.toHaveBeenCalled();
  });

  it("persists the toggle and stops in-flight speech when turned off mid-stream", async () => {
    const { result } = renderHook(() => useReadAloud());
    await waitFor(() => expect(result.current.available).toBe(true));

    act(() => result.current.toggle()); // on
    expect(result.current.on).toBe(true);
    expect(localStorage.getItem("readAloudPlayback")).toBe("1");

    synth.cancel.mockClear();
    act(() => result.current.toggle()); // off mid-stream -> cancel now
    expect(result.current.on).toBe(false);
    expect(synth.cancel).toHaveBeenCalled();
    expect(localStorage.getItem("readAloudPlayback")).toBe("0");
  });
});
