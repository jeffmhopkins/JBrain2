import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { confidenceColor } from "./AudioTranscript";
import { VideoAnalysis, activeFrameIndex } from "./VideoAnalysis";

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

  it("renders a stream card with no <video> and scrubs captions via the filmstrip", () => {
    // A stream source (analyze_stream) has no playable local video and no served
    // thumbs: the card drops the <video>, frames render as markers, and tapping one
    // still highlights it and surfaces its caption on the now-line.
    const { container } = renderCard({
      videoUrl: undefined,
      frames: [
        { tMs: 0, caption: "Rocket on the mount." },
        { tMs: 2000, caption: "Venting vapor." },
      ],
    });
    expect(container.querySelector("video")).toBeNull();
    expect(container.querySelectorAll(".tv-vid-frame")).toHaveLength(2);
    expect(container.querySelector(".tv-vid-frame-img")).toBeNull();
    expect(screen.getByText("Rocket on the mount.")).toBeInTheDocument(); // now-line
    fireEvent.click(container.querySelectorAll(".tv-vid-frame")[1] as Element);
    expect(screen.getByText("Venting vapor.")).toBeInTheDocument(); // scrubbed via caption
  });

  const YT_ORIGIN = "https://www.youtube-nocookie.com";
  const YT_FRAMES = [
    { tMs: 0, caption: "Opening shot." },
    { tMs: 2000, caption: "Two seconds in." },
  ];

  it("embeds the YouTube player and syncs the clock from its postMessage time", () => {
    const { container } = renderCard({
      videoUrl: undefined,
      youtubeId: "abc123",
      frames: YT_FRAMES,
    });
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.getAttribute("src")).toContain(`${YT_ORIGIN}/embed/abc123`);
    expect(iframe.getAttribute("src")).toContain("enablejsapi=1");
    expect(container.querySelector("video")).toBeNull();

    // A YouTube infoDelivery frame advances the shared clock → the now-line follows.
    fireEvent(
      window,
      new MessageEvent("message", {
        data: JSON.stringify({ event: "infoDelivery", info: { currentTime: 2 } }),
        origin: YT_ORIGIN,
      }),
    );
    expect(screen.getByText("Two seconds in.")).toBeInTheDocument();
  });

  it("ignores time messages from a non-YouTube origin (anti-spoof)", () => {
    renderCard({ videoUrl: undefined, youtubeId: "abc123", frames: YT_FRAMES });
    fireEvent(
      window,
      new MessageEvent("message", {
        data: JSON.stringify({ info: { currentTime: 2 } }),
        origin: "https://evil.example.com",
      }),
    );
    expect(screen.getByText("Opening shot.")).toBeInTheDocument(); // clock did not move
  });

  it("seeks the embedded YouTube player when a filmstrip frame is tapped", () => {
    const { container } = renderCard({
      videoUrl: undefined,
      youtubeId: "abc123",
      frames: YT_FRAMES,
    });
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    const postMessage = vi.fn();
    Object.defineProperty(iframe, "contentWindow", {
      value: { postMessage },
      configurable: true,
    });
    fireEvent.click(container.querySelectorAll(".tv-vid-frame")[1] as Element);
    expect(postMessage).toHaveBeenCalledWith(
      JSON.stringify({ event: "command", func: "seekTo", args: [2, true] }),
      YT_ORIGIN,
    );
  });

  it("shows a LIVE badge and a tappable source chip for a live stream", () => {
    const { container } = renderCard({
      videoUrl: undefined,
      isLive: true,
      sourceUrl: "https://www.youtube.com/live/xyz",
      frames: YT_FRAMES,
    });
    expect(screen.getByText("LIVE")).toBeInTheDocument();
    const link = container.querySelector<HTMLAnchorElement>(".tv-vid-src");
    expect(link?.getAttribute("href")).toBe("https://www.youtube.com/live/xyz");
    expect(link?.getAttribute("rel")).toContain("noreferrer"); // no referrer leak (#9)
    expect(link?.textContent).toContain("youtube.com"); // host label, not the full URL
  });

  it("shows no LIVE badge or source chip for a plain attachment video", () => {
    const { container } = renderCard(); // videoUrl set, no isLive/sourceUrl
    expect(screen.queryByText("LIVE")).toBeNull();
    expect(container.querySelector(".tv-vid-src")).toBeNull();
  });

  it("falls back to a placeholder for a frame without a thumbnail", () => {
    const { container } = renderCard({
      frames: [{ tMs: 0, caption: "No still." }],
    });
    expect(container.querySelector(".tv-vid-frame-ph")).not.toBeNull();
    expect(container.querySelector(".tv-vid-frame-img")).toBeNull();
  });

  it("has no Moments tab — the filmstrip is the only timeline", () => {
    renderCard();
    expect(screen.queryByRole("tab", { name: "Moments" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Summary" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Transcript" })).toBeInTheDocument();
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

  it("notes the transcript source (provider captions) on the Transcript tab", () => {
    renderCard({ transcriptSource: "captions" });
    fireEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    expect(screen.getByText("From the source's own captions")).toBeInTheDocument();
  });

  it("notes a locally-transcribed source, and shows no note when the source is unknown", () => {
    const { rerender } = renderCard({ transcriptSource: "whisper" });
    fireEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    expect(screen.getByText("Transcribed locally")).toBeInTheDocument();

    rerender(
      <VideoAnalysis
        videoUrl="/clip.mp4"
        filename="walkthrough.mp4"
        summary="s"
        frames={FRAMES}
        words={WORDS}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    expect(screen.queryByText("Transcribed locally")).toBeNull();
  });

  it("shows no tab bar when the clip has no speech (summary only), but keeps the filmstrip", () => {
    const { container } = renderCard({ words: [], transcriptText: undefined });
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByRole("tab", { name: "Transcript" })).toBeNull();
    expect(container.querySelectorAll(".tv-vid-frame")).toHaveLength(2); // filmstrip stays
    expect(screen.getByText("A walkthrough of the build pipeline.")).toBeInTheDocument();
  });
});
