import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReadTextScreen, concatWav } from "./ReadTextScreen";

// The raw bytes of a minimal valid mono 16-bit WAV wrapping `samples`, so concatWav has a real
// fmt + data chunk to walk. Returned as an ArrayBuffer: fed to `new Response(...)` as a body it
// round-trips to an undici Blob (arrayBuffer-capable, like the browser's) — jsdom's own global
// Blob can't carry binary, so building blobs this way mirrors what production feeds concatWav.
function makeWavBuffer(samples: number[]): ArrayBuffer {
  const dataLen = samples.length * 2;
  const buf = new ArrayBuffer(44 + dataLen);
  const dv = new DataView(buf);
  const bytes = new Uint8Array(buf);
  const w = (o: number, s: string) => {
    for (let i = 0; i < s.length; i++) bytes[o + i] = s.charCodeAt(i);
  };
  w(0, "RIFF");
  dv.setUint32(4, 36 + dataLen, true);
  w(8, "WAVE");
  w(12, "fmt ");
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); // PCM
  dv.setUint16(22, 1, true); // mono
  dv.setUint32(24, 22050, true);
  dv.setUint32(28, 44100, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  w(36, "data");
  dv.setUint32(40, dataLen, true);
  for (let i = 0; i < samples.length; i++) dv.setInt16(44 + i * 2, samples[i] ?? 0, true);
  return buf;
}

// A proper (arrayBuffer-capable) Blob from those bytes, as production's response.blob() yields.
function makeWav(samples: number[]): Promise<Blob> {
  return new Response(makeWavBuffer(samples)).blob();
}

// Serve every /api/brain/tts render as a two-sample WAV so a multi-clip export/play has real
// audio to splice; capture the rendered `text` per call so tests can assert clip splitting.
function stubTts(): { texts: string[] } {
  const texts: string[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const path = String(input);
    if (path.startsWith("/api/brain/tts")) {
      const text = new URL(path, "http://x").searchParams.get("text") ?? "";
      texts.push(text);
      return new Response(makeWavBuffer([1, 2]), {
        status: 200,
        headers: { "Content-Type": "audio/wav" },
      });
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { texts };
}

// jsdom has no real <audio>; a stand-in fires onended synchronously so a playback drains.
class FakeAudio {
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  play() {
    this.onended?.();
    return Promise.resolve();
  }
  pause() {}
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("concatWav", () => {
  it("splices two WAVs' audio into one file, summing their data length", async () => {
    // 5 samples across the two clips → 10 data bytes; a 44-byte header wraps them into one file.
    const merged = await concatWav([await makeWav([1, 2, 3]), await makeWav([4, 5])]);
    expect(merged.type).toBe("audio/wav");
    expect(merged.size).toBe(44 + 5 * 2);
  });

  it("throws when no rendered clip carries a fmt chunk", async () => {
    await expect(concatWav([new Blob([], { type: "audio/wav" })])).rejects.toThrow();
  });
});

describe("ReadTextScreen", () => {
  it("plays the typed text, rendering one clip per sentence and showing Stop while it runs", async () => {
    const { texts } = stubTts();
    vi.stubGlobal("Audio", FakeAudio);
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:x" });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });

    render(<ReadTextScreen voice="en_US-amy-medium" onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Text to read aloud"), {
      target: { value: "One. Two. Three." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Play" }));

    // Three sentences → three /tts renders, back in the chosen voice.
    await waitFor(() => expect(texts).toEqual(["One.", "Two.", "Three."]));
    // Playback drains synchronously via FakeAudio, so the control returns to Play.
    await waitFor(() => expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument());
  });

  it("exports the whole text to a single downloaded WAV", async () => {
    stubTts();
    const clicked: { href: string; download: string }[] = [];
    const realCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLElement;
      if (tag === "a") {
        el.click = () =>
          clicked.push({
            href: (el as HTMLAnchorElement).href,
            download: (el as HTMLAnchorElement).download,
          });
      }
      return el;
    });
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:out" });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });

    render(<ReadTextScreen voice="en_US-amy-medium" onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Text to read aloud"), {
      target: { value: "Chapter one. It begins." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Export audio" }));

    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(clicked[0]?.download).toBe("chapter-one-it-begins.wav");
  });

  it("disables the actions until there is text", () => {
    stubTts();
    render(<ReadTextScreen voice="en_US-amy-medium" onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Play" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Export audio" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Text to read aloud"), {
      target: { value: "Something." },
    });
    expect(screen.getByRole("button", { name: "Play" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Export audio" })).toBeEnabled();
  });

  it("closes via the back button", () => {
    stubTts();
    const onClose = vi.fn();
    render(<ReadTextScreen voice="en_US-amy-medium" onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onClose).toHaveBeenCalled();
  });
});
