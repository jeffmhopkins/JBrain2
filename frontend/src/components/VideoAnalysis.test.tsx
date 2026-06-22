import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { confidenceColor } from "./AudioTranscript";
import { VideoAnalysis, activeFrameIndex, buildMoments } from "./VideoAnalysis";

const FRAMES = [
  { tMs: 0, caption: "A title card." },
  { tMs: 4000, caption: "A pipeline diagram." },
];
const WORDS = [
  { text: "Hello", startMs: 0, endMs: 500, confidence: 0.95 },
  { text: "world", startMs: 4200, endMs: 4800, confidence: 0.3 },
];

function renderCard(over: Partial<Parameters<typeof VideoAnalysis>[0]> = {}) {
  return render(
    <VideoAnalysis
      videoUrl="/clip.mp4"
      filename="walkthrough.mp4"
      summary="A walkthrough of the build pipeline."
      durationMs={8000}
      frames={FRAMES}
      words={WORDS}
      {...over}
    />,
  );
}

describe("buildMoments", () => {
  it("pairs each frame with the words spoken in its window", () => {
    expect(buildMoments(FRAMES, WORDS)).toEqual([
      { tMs: 0, caption: "A title card.", said: "Hello" },
      { tMs: 4000, caption: "A pipeline diagram.", said: "world" },
    ]);
  });
});

describe("activeFrameIndex", () => {
  it("is the latest frame at or before the clock", () => {
    expect(activeFrameIndex(FRAMES, 0)).toBe(0);
    expect(activeFrameIndex(FRAMES, 3999)).toBe(0);
    expect(activeFrameIndex(FRAMES, 4000)).toBe(1);
    expect(activeFrameIndex([], 100)).toBe(-1);
  });
});

describe("VideoAnalysis", () => {
  it("renders a native-controls <video> with the built src and the header", () => {
    const { container } = renderCard();
    const video = container.querySelector("video") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe("/clip.mp4");
    expect(video.getAttribute("controls")).not.toBeNull();
    expect(screen.getByText("walkthrough.mp4")).toBeInTheDocument();
    expect(screen.getByText("2 frames")).toBeInTheDocument();
  });

  it("shows the summary on the default tab and the now-line for the first frame", () => {
    renderCard();
    expect(screen.getByText("A walkthrough of the build pipeline.")).toBeInTheDocument();
    // The now-line shows the frame active at t=0.
    expect(screen.getByText("A title card.")).toBeInTheDocument();
  });

  it("renders a marker rail tick per frame (driven by the duration)", () => {
    const { container } = renderCard();
    expect(container.querySelectorAll(".tv-vid-tick")).toHaveLength(2);
  });

  it("switching to Moments shows the caption + said feed and seeks on tap", () => {
    const { container } = renderCard();
    fireEvent.click(screen.getByRole("tab", { name: "Moments" }));
    expect(screen.getByText("A pipeline diagram.")).toBeInTheDocument();
    expect(screen.getByText("“world”")).toBeInTheDocument();
    const video = container.querySelector("video") as HTMLVideoElement;
    fireEvent.click(screen.getByText("A pipeline diagram."));
    expect(video.currentTime).toBeCloseTo(4); // 4000ms
  });

  it("switching to Transcript reuses the karaoke reader (confidence colors + seek)", () => {
    const { container } = renderCard();
    fireEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    const word = screen.getByRole("button", { name: "world" });
    expect(word.style.color).toBe(confidenceColor(0.3));
    const video = container.querySelector("video") as HTMLVideoElement;
    fireEvent.click(word);
    expect(video.currentTime).toBeCloseTo(4.2); // 4200ms
  });

  it("omits the Transcript tab when the clip has no speech", () => {
    renderCard({ words: [], transcriptText: undefined });
    expect(screen.queryByRole("tab", { name: "Transcript" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Moments" })).toBeInTheDocument();
  });

  it("shows no tab bar when only the summary is present", () => {
    renderCard({ frames: [], words: [] });
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByText("A walkthrough of the build pipeline.")).toBeInTheDocument();
  });
});
