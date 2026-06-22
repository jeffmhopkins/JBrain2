import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { confidenceColor } from "./AudioTranscript";
import { VideoAnalysis, activeFrameIndex, buildMoments } from "./VideoAnalysis";

const FRAMES = [
  { tMs: 0, caption: "A title card.", thumbUrl: "/api/chat-attachments/att/thumb/sha0" },
  { tMs: 4000, caption: "A pipeline diagram.", thumbUrl: "/api/chat-attachments/att/thumb/sha1" },
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
      frames={FRAMES}
      words={WORDS}
      {...over}
    />,
  );
}

describe("buildMoments", () => {
  it("pairs each frame with its thumbnail and the words spoken in its window", () => {
    expect(buildMoments(FRAMES, WORDS)).toEqual([
      { tMs: 0, caption: "A title card.", thumbUrl: FRAMES[0]?.thumbUrl, said: "Hello" },
      { tMs: 4000, caption: "A pipeline diagram.", thumbUrl: FRAMES[1]?.thumbUrl, said: "world" },
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
    expect(screen.getByText("A title card.")).toBeInTheDocument(); // the now-line
  });

  it("renders the filmstrip with a thumbnail per frame and seeks on tap", () => {
    const { container } = renderCard();
    const frameButtons = container.querySelectorAll(".tv-vid-frame");
    expect(frameButtons).toHaveLength(2);
    const imgs = container.querySelectorAll<HTMLImageElement>(".tv-vid-frame-img");
    expect(imgs[1]?.getAttribute("src")).toBe("/api/chat-attachments/att/thumb/sha1");
    const video = container.querySelector("video") as HTMLVideoElement;
    fireEvent.click(frameButtons[1] as Element);
    expect(video.currentTime).toBeCloseTo(4); // 4000ms
  });

  it("falls back to a placeholder for a frame without a thumbnail", () => {
    const { container } = renderCard({
      frames: [{ tMs: 0, caption: "No still." }],
    });
    expect(container.querySelector(".tv-vid-frame-ph")).not.toBeNull();
    expect(container.querySelector(".tv-vid-frame-img")).toBeNull();
  });

  it("switching to Moments shows the caption + said feed and seeks on tap", () => {
    const { container } = renderCard();
    fireEvent.click(screen.getByRole("tab", { name: "Moments" }));
    expect(screen.getByText("A pipeline diagram.")).toBeInTheDocument();
    expect(screen.getByText("“world”")).toBeInTheDocument();
    expect(container.querySelectorAll(".tv-vid-moment-thumb")).toHaveLength(2);
    const video = container.querySelector("video") as HTMLVideoElement;
    fireEvent.click(screen.getByText("A pipeline diagram."));
    expect(video.currentTime).toBeCloseTo(4);
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
