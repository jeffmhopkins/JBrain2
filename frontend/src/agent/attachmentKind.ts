// The compact-chip taxonomy for chat attachments (docs/mocks/chat-attach-b-chips.html):
// a media type maps to one of three accent classes — image (steel), pdf (rose),
// or text (green) — shared by the composer's staged chips and the bubble chips so
// both read identically.

export type AttachmentKind = "img" | "pdf" | "txt";

/** Classify a media type into the chip's accent class. Anything that isn't an
 * image or a PDF falls back to the neutral "txt" treatment (text/markdown/csv/json). */
export function attachmentKind(mediaType: string): AttachmentKind {
  if (mediaType.startsWith("image/")) return "img";
  if (mediaType === "application/pdf") return "pdf";
  return "txt";
}
