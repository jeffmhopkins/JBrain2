// The audio-transcript card: confidence gradient on each word, word-tap seeks
// the audio, the spoken word highlights. jsdom has no media engine, so we drive
// playback position by setting the <audio> currentTime + firing timeupdate.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AudioTranscript, confidenceColor, transcriptWords } from "./AudioTranscript";

const WORDS = [
  { text: "Hello", startMs: 0, endMs: 500, confidence: 0.95 },
  { text: "world", startMs: 500, endMs: 1100, confidence: 0.3 },
];

describe("confidenceColor", () => {
  it("runs rose (low) → amber (mid) → green (high)", () => {
    expect(confidenceColor(0)).toBe("rgb(207, 138, 143)");
    expect(confidenceColor(0.5)).toBe("rgb(201, 163, 106)");
    expect(confidenceColor(1)).toBe("rgb(143, 188, 154)");
    // clamps out-of-range
    expect(confidenceColor(2)).toBe(confidenceColor(1));
  });
});

describe("transcriptWords", () => {
  it("maps the API snake_case shape and drops garbage", () => {
    expect(
      transcriptWords([
        { text: "hi", start_ms: 0, end_ms: 200, confidence: 0.8 },
        { text: 42 }, // not a string -> dropped
        "nope", // not an object -> dropped
      ]),
    ).toEqual([{ text: "hi", startMs: 0, endMs: 200, confidence: 0.8 }]);
    expect(transcriptWords(null)).toEqual([]);
  });
});

describe("AudioTranscript", () => {
  it("renders each word as a button colored by confidence", () => {
    const { container } = render(
      <AudioTranscript audioUrl="/audio.wav" filename="memo.wav" words={WORDS} durationMs={1100} />,
    );
    // Move past the end so no word is the "now" highlight and both show color.
    const audio = container.querySelector("audio") as HTMLAudioElement;
    audio.currentTime = 5;
    fireEvent.timeUpdate(audio);
    // High confidence is greener, low is rosier — they must differ.
    expect(screen.getByRole("button", { name: "Hello" }).style.color).toBe(confidenceColor(0.95));
    expect(screen.getByRole("button", { name: "world" }).style.color).toBe(confidenceColor(0.3));
    expect(confidenceColor(0.95)).not.toBe(confidenceColor(0.3));
  });

  it("seeks the audio when a word is tapped and highlights the spoken word", () => {
    const { container } = render(
      <AudioTranscript audioUrl="/audio.wav" filename="memo.wav" words={WORDS} durationMs={1100} />,
    );
    const audio = container.querySelector("audio") as HTMLAudioElement;
    const world = screen.getByRole("button", { name: "world" });

    fireEvent.click(world);
    expect(audio.currentTime).toBeCloseTo(0.5); // 500ms
    // The clicked (now-playing) word gets the highlight; its inline color clears.
    expect(world.className).toContain("now");
    expect(world.style.color).toBe("");
  });

  it("tracks the spoken word as playback advances", () => {
    const { container } = render(
      <AudioTranscript audioUrl="/audio.wav" filename="memo.wav" words={WORDS} durationMs={1100} />,
    );
    const audio = container.querySelector("audio") as HTMLAudioElement;
    audio.currentTime = 0.7; // inside "world" [500,1100)
    fireEvent.timeUpdate(audio);
    expect(screen.getByRole("button", { name: "world" }).className).toContain("now");
    expect(screen.getByRole("button", { name: "Hello" }).className).not.toContain("now");
  });

  it("falls back to plain text when there are no words", () => {
    render(<AudioTranscript audioUrl="/a.wav" filename="m.wav" words={[]} text="plain body" />);
    expect(screen.getByText("plain body")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "plain" })).toBeNull();
  });

  it("renders a native-controls <video> for video media, with the same karaoke words", () => {
    const { container } = render(
      <AudioTranscript
        audioUrl="/clip.mp4"
        filename="clip.mp4"
        media="video"
        words={WORDS}
        durationMs={1100}
      />,
    );
    const video = container.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("controls")).not.toBeNull();
    expect(container.querySelector("audio")).toBeNull(); // no hidden <audio> for video
    // The transcript words still render and the same media element drives seeking.
    fireEvent.click(screen.getByRole("button", { name: "world" }));
    expect(video.currentTime).toBeCloseTo(0.5);
  });
});
