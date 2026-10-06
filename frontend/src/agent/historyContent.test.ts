import { describe, expect, it } from "vitest";
import { userMessage } from "./transcript";
import { historyContent } from "./useFullBrain";

// CACHE CONTRACT with backend attachment_content.decorated_history_text: the server renders an
// attaching turn exactly like this, so the follow-up's prefix matches and nothing re-encodes.
describe("historyContent", () => {
  const file = (id: string, filename: string, media_type: string) => ({
    id,
    filename,
    media_type,
    size_bytes: 1,
  });

  it("decorates images and videos in attachment order under one marker", () => {
    const m = userMessage("what am I holding?", [
      file("v1", "clip.mp4", "video/mp4"),
      file("d1", "notes.pdf", "application/pdf"),
      file("i1", "a.jpg", "image/jpeg"),
    ]);
    expect(historyContent(m)).toBe(
      "what am I holding?\n\n[Images the owner attached this turn — " +
        "source_attachment_id=v1 (clip.mp4); source_attachment_id=i1 (a.jpg)]",
    );
  });

  it("leaves a turn with no image or video as its text", () => {
    const m = userMessage("hi", [file("d1", "notes.pdf", "application/pdf")]);
    expect(historyContent(m)).toBe("hi");
  });
});
