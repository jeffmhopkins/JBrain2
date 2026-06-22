import { createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ListOut, api } from "../../api/client";
import type { ViewPayload } from "../types";
import { resetLiveLists } from "./liveList";
import { ToolView, isKnownView } from "./registry";

function payload(over: Partial<ViewPayload>): ViewPayload {
  return { view: "", surface: "inline", data: {}, refs: [], ...over };
}

function listOut(over: Partial<ListOut> = {}): ListOut {
  return { id: "L1", title: "Groceries", domain: "general", archived: false, items: [], ...over };
}

// Keep the shared live-list store (and api spies) from leaking across cases.
afterEach(() => {
  resetLiveLists();
  vi.restoreAllMocks();
});

describe("ToolView registry", () => {
  it("renders nothing for an unknown component name (the invariant)", () => {
    const { container } = render(<ToolView payload={payload({ view: "evil_widget" })} />);
    expect(container.firstChild).toBeNull();
    expect(isKnownView("evil_widget")).toBe(false);
  });

  it("renders a stat_block from data-only slots", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "stat_block",
          data: { label: "LDL", value: "118", unit: "mg/dL", tone: "warn" },
        })}
      />,
    );
    expect(getByText("LDL")).toBeInTheDocument();
    expect(getByText("118")).toBeInTheDocument();
    expect(getByText("mg/dL")).toBeInTheDocument();
  });

  it("renders a data_table with header and rows", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "data_table",
          data: { columns: ["date", "value"], rows: [["2026-01-01", "5.4"]] },
        })}
      />,
    );
    expect(getByText("date")).toBeInTheDocument();
    expect(getByText("2026-01-01")).toBeInTheDocument();
    expect(getByText("5.4")).toBeInTheDocument();
  });

  it("renders citation chips from refs, pointer-not-copy", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "citation_card",
          data: { title: "Sources" },
          refs: [
            { kind: "note", note_id: "n1", label: "lab note" },
            { kind: "entity", entity_id: "e1", label: "Dr. Lin", domain: "health" },
          ],
        })}
      />,
    );
    expect(getByText("Sources")).toBeInTheDocument();
    expect(getByText("lab note")).toBeInTheDocument();
    expect(getByText("Dr. Lin")).toBeInTheDocument();
  });

  function listCard(items: { id: string; body: string; checked: boolean }[]) {
    return payload({
      view: "list_card",
      data: { list_id: "L1", title: "Groceries", items },
    });
  }

  it("renders a list_card checklist with checked state", () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline")); // keep the snapshot
    const { getByText } = render(
      <ToolView
        payload={listCard([
          { id: "a", body: "eggs", checked: false },
          { id: "b", body: "milk", checked: true },
        ])}
      />,
    );
    expect(getByText("Groceries")).toBeInTheDocument();
    expect(getByText("eggs")).toBeInTheDocument();
    // The checked item carries the checked row class (theme draws the tick).
    expect(getByText("milk").closest(".tv-list-row")).toHaveClass("checked");
  });

  it("tapping a list_card checkbox toggles the item via the API", async () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline"));
    const setChecked = vi.spyOn(api, "setListItemChecked").mockResolvedValue();
    const { getByLabelText, getByText } = render(
      <ToolView payload={listCard([{ id: "a", body: "eggs", checked: false }])} />,
    );
    fireEvent.click(getByLabelText("Check eggs"));
    // Optimistic: the row flips immediately, and the write is sent.
    expect(getByText("eggs").closest(".tv-list-row")).toHaveClass("checked");
    await waitFor(() => expect(setChecked).toHaveBeenCalledWith("a", true));
    expect(getByLabelText("Uncheck eggs")).toBeInTheDocument();
  });

  it("reverts a list_card toggle when the write fails", async () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "setListItemChecked").mockRejectedValue(new Error("boom"));
    const { getByLabelText, getByText } = render(
      <ToolView payload={listCard([{ id: "a", body: "eggs", checked: false }])} />,
    );
    fireEvent.click(getByLabelText("Check eggs"));
    // It flips optimistically, then snaps back once the write rejects.
    await waitFor(() =>
      expect(getByText("eggs").closest(".tv-list-row")).not.toHaveClass("checked"),
    );
  });

  it("replaces the snapshot with live list state", async () => {
    // The card's payload says milk is open, but the live list has it checked.
    vi.spyOn(api, "getList").mockResolvedValue(
      listOut({ items: [{ id: "b", body: "milk", checked: true }] }),
    );
    const { getByText } = render(
      <ToolView payload={listCard([{ id: "b", body: "milk", checked: false }])} />,
    );
    await waitFor(() => expect(getByText("milk").closest(".tv-list-row")).toHaveClass("checked"));
  });

  it("keeps two cards of the same list in sync on a toggle", async () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "setListItemChecked").mockResolvedValue();
    const card = listCard([{ id: "a", body: "eggs", checked: false }]);
    // Two cards of list L1 in the same transcript.
    const { container } = render(
      <>
        <ToolView payload={card} />
        <ToolView payload={card} />
      </>,
    );
    const rows = () => [...container.querySelectorAll(".tv-list-row")];
    expect(rows()).toHaveLength(2);
    // Toggle the first card; the second must follow (shared live store).
    fireEvent.click(rows()[0]?.querySelector("button") as HTMLElement);
    await waitFor(() => expect(rows().every((r) => r.classList.contains("checked"))).toBe(true));
  });

  it("renders an appointment_card with status flag, location, repeat, attendees", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "appointment_card",
          data: {
            id: "A1",
            title: "Dentist",
            start: "2026-06-15T14:00:00+00:00",
            status: "tentative",
            location: "123 Main St",
            recurring: true,
            attendees: ["Dr. Nguyen"],
          },
        })}
      />,
    );
    expect(getByText("Dentist")).toBeInTheDocument();
    expect(getByText("123 Main St")).toBeInTheDocument();
    expect(getByText("repeats")).toBeInTheDocument();
    expect(getByText("with Dr. Nguyen")).toBeInTheDocument();
    // Status is a flag enum the theme colors, never a model-authored color.
    expect(getByText("tentative")).toHaveClass("flag-tentative");
  });

  it("renders a generate generated_image with the by-id src and sizing", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: {
            image_id: "img_7fa1",
            kind: "generate",
            prompt: "A watercolor lighthouse at dusk",
            width: 768,
            height: 1024,
            model: "qwen-image",
          },
        })}
      />,
    );
    const img = container.querySelector("img") as HTMLImageElement;
    // Data-only: the component BUILDS the src from image_id (no model URL).
    expect(img.getAttribute("src")).toBe("/api/images/generated/img_7fa1");
    expect(img).toHaveAttribute("alt", "A watercolor lighthouse at dusk");
    // Sized from width/height to avoid layout shift.
    expect(img).toHaveAttribute("width", "768");
    expect(img).toHaveAttribute("height", "1024");
    const frame = container.querySelector(".tv-genimg-frame") as HTMLElement;
    expect(frame.style.aspectRatio).toBe("768 / 1024");
    // A generate has no before/after compare.
    expect(container.querySelector(".tv-genimg-cmp")).toBeNull();
  });

  it("renders a video_analysis card, building the chat-attachment src by id", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "video_analysis",
          data: {
            attachment_id: "att_123",
            source: "chat",
            media: "video",
            filename: "meeting.mp4",
            summary: "A short standup.",
            duration_ms: 8000,
            frames: [{ t_ms: 0, caption: "A title card.", thumb_id: "sha-deadbeef" }],
            transcript: {
              text: "Hello team",
              words: [{ text: "Hello", start_ms: 0, end_ms: 500, confidence: 0.95 }],
            },
          },
        })}
      />,
    );
    const video = container.querySelector("video") as HTMLVideoElement;
    // Data-only: the component BUILDS the src from attachment_id + source (no URL).
    expect(video.getAttribute("src")).toBe("/api/chat-attachments/att_123");
    expect(container.querySelector(".tv-vid")).not.toBeNull();
    expect(screen.getByText("meeting.mp4")).toBeInTheDocument();
    expect(screen.getByText("A short standup.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Transcript" })).toBeInTheDocument();
  });

  it("shows the seed on the card so the owner can reuse it", () => {
    const withSeed = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: { image_id: "img_1", kind: "generate", width: 768, height: 768, seed: 4242 },
        })}
      />,
    );
    expect(withSeed.getByText("768 × 768 · seed 4242")).toBeInTheDocument();
    withSeed.unmount();
    // Absent seed simply omits it (older images carry no seed in the view).
    const noSeed = render(
      <ToolView
        payload={payload({ view: "generated_image", data: { image_id: "x", kind: "generate" } })}
      />,
    );
    expect(noSeed.queryByText(/seed/)).not.toBeInTheDocument();
    expect(noSeed.container.querySelector(".tv-genimg-cap")?.textContent).toBe("512 × 512");
  });

  it("a generate image drops the kind pill and offers a full-screen expand", () => {
    const { container, getByLabelText } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: {
            image_id: "img_7fa1",
            kind: "generate",
            prompt: "a fox",
            width: 768,
            height: 768,
          },
        })}
      />,
    );
    // The "generated" pill is gone; the frame is a button that opens the viewer.
    expect(container.querySelector(".tv-genimg-kind")).toBeNull();
    expect(getByLabelText("Expand image to full screen").tagName).toBe("BUTTON");
  });

  it("expanding a generate image opens a full-screen viewer, dismissable", () => {
    const { getByLabelText } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: {
            image_id: "img_7fa1",
            kind: "generate",
            prompt: "a fox",
            width: 768,
            height: 768,
          },
        })}
      />,
    );
    expect(document.querySelector(".fb-lightbox")).toBeNull();
    fireEvent.click(getByLabelText("Expand image to full screen"));
    const viewer = document.querySelector(".fb-lightbox");
    expect(viewer).not.toBeNull();
    // The viewer shows the by-id full image (never a model-authored URL).
    expect(viewer?.querySelector("img")?.getAttribute("src")).toBe(
      "/api/images/generated/img_7fa1",
    );
    fireEvent.click(getByLabelText("Close image"));
    expect(document.querySelector(".fb-lightbox")).toBeNull();
  });

  it("holds the live preview as a placeholder until the full image loads", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: {
            image_id: "img_7fa1",
            kind: "generate",
            width: 768,
            height: 768,
            placeholder_data_uri: "data:image/jpeg;base64,AAA",
          },
        })}
      />,
    );
    const ph = container.querySelector(".tv-genimg-ph") as HTMLImageElement;
    expect(ph.getAttribute("src")).toBe("data:image/jpeg;base64,AAA");
    // Once the full image decodes, the placeholder is dropped.
    fireEvent.load(container.querySelector(".tv-genimg-img") as HTMLImageElement);
    expect(container.querySelector(".tv-genimg-ph")).toBeNull();
  });

  it("renders an edit generated_image as a before/after compare with the source src", () => {
    const { container, getByText } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: {
            image_id: "img_9c30",
            kind: "edit",
            prompt: "Make the sky stormy",
            width: 768,
            height: 1024,
            model: "qwen-image-edit",
          },
        })}
      />,
    );
    const imgs = [...container.querySelectorAll("img")] as HTMLImageElement[];
    const srcs = imgs.map((i) => i.getAttribute("src"));
    // Before = the source bytes resolved by id; after = the result, both by id.
    expect(srcs).toContain("/api/images/generated/img_9c30/source");
    expect(srcs).toContain("/api/images/generated/img_9c30");
    expect(getByText("BEFORE")).toBeInTheDocument();
    expect(getByText("AFTER")).toBeInTheDocument();
    expect(getByText("768 × 1024 · qwen-image-edit")).toBeInTheDocument();
    // No corner kind pill — the BEFORE/AFTER labels already say it's an edit.
    expect(container.querySelector(".tv-genimg-kind")).toBeNull();
  });

  it("the edit toggle switches between Compare (slider) and a zoomable Edited view", () => {
    const { container, getByLabelText } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: { image_id: "img_9c30", kind: "edit", width: 768, height: 1024 },
        })}
      />,
    );
    // The toggle is icon-only (text labels overflowed); the modes carry aria-labels.
    expect(container.querySelector(".tv-genimg-cmp")).not.toBeNull();
    expect(container.querySelector(".tv-genimg-frame")).toBeNull();
    expect(getByLabelText("Compare")).toHaveAttribute("aria-pressed", "true");

    // "Edited" drops the slider for a single clickable frame of the result, which
    // opens the full-screen viewer (the same as a generated image).
    fireEvent.click(getByLabelText("Edited"));
    expect(container.querySelector(".tv-genimg-cmp")).toBeNull();
    const frame = container.querySelector(".tv-genimg-frame") as HTMLElement;
    expect(frame.tagName).toBe("BUTTON");
    expect(frame.querySelector("img")?.getAttribute("src")).toBe("/api/images/generated/img_9c30");
    fireEvent.click(frame);
    expect(document.querySelector(".fb-lightbox")).not.toBeNull();

    // …and back to the slider.
    fireEvent.click(getByLabelText("Compare"));
    expect(container.querySelector(".tv-genimg-cmp")).not.toBeNull();
  });

  it("dragging the compare moves the wipe (pointer events)", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "generated_image",
          data: { image_id: "img_9c30", kind: "edit", width: 768, height: 1024 },
        })}
      />,
    );
    const cmp = container.querySelector(".tv-genimg-cmp") as HTMLElement;
    // jsdom has no layout, so stub the measured rect the drag handler reads.
    cmp.getBoundingClientRect = () =>
      ({ left: 0, width: 200, top: 0, height: 200, right: 200, bottom: 200 }) as DOMRect;
    cmp.setPointerCapture = () => {};
    // jsdom's synthetic PointerEvent drops clientX from the init dict, so build
    // each event and define the coordinate the drag handler reads on it.
    function pointer(make: (el: Element) => Event, clientX: number): void {
      const ev = make(cmp);
      Object.defineProperty(ev, "clientX", { value: clientX });
      fireEvent(cmp, ev);
    }
    pointer((el) => createEvent.pointerDown(el, { pointerId: 1 }), 50);
    // 50 / 200 → 25%.
    expect(cmp.style.getPropertyValue("--pos")).toBe("25%");
    pointer((el) => createEvent.pointerMove(el, { pointerId: 1 }), 150);
    expect(cmp.style.getPropertyValue("--pos")).toBe("75%");
  });

  it("renders a server_metrics view as a labeled sparkline stack", () => {
    const point = (over: Record<string, unknown>) => ({
      t: "2026-06-22T00:00:00Z",
      load_1m: 0.5,
      mem_used_bytes: 60 * 2 ** 30,
      mem_total_bytes: 128 * 2 ** 30,
      disk_used_bytes: 500 * 2 ** 30,
      disk_total_bytes: 2000 * 2 ** 30,
      gpu_busy_percent: 40,
      fan_rpm_max: 2100,
      ...over,
    });
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "server_metrics",
          data: {
            range: "24h",
            resolution: "raw",
            points: [point({ load_1m: 0.5 }), point({ load_1m: 1.5, fan_rpm_max: 2600 })],
          },
        })}
      />,
    );
    expect(getByText("Server health · 24h")).toBeInTheDocument();
    expect(getByText("2 30s buckets")).toBeInTheDocument();
    expect(getByText("CPU load")).toBeInTheDocument();
    // Peak readout reflects the higher bucket (load 1.5, fan 2600).
    expect(getByText("1.50 peak")).toBeInTheDocument();
    expect(getByText("2600 rpm peak")).toBeInTheDocument();
  });

  it("server_metrics with no points states it, no crash", () => {
    const { getByText } = render(
      <ToolView payload={payload({ view: "server_metrics", data: { points: [] } })} />,
    );
    expect(getByText("No host-metrics samples recorded.")).toBeInTheDocument();
  });

  it("tolerates missing/extra slots without crashing", () => {
    const { container } = render(<ToolView payload={payload({ view: "data_table" })} />);
    expect(container.querySelector("table")).toBeInTheDocument();
    // A list_card with no items renders the empty row, not a crash.
    const empty = render(<ToolView payload={payload({ view: "list_card" })} />);
    expect(empty.getByText("empty")).toBeInTheDocument();
    // An appointment_card with only a view name falls back to a default title and
    // a confirmed status, no crash.
    const bare = render(<ToolView payload={payload({ view: "appointment_card" })} />);
    expect(bare.getByText("Appointment")).toBeInTheDocument();
    expect(bare.getByText("confirmed")).toHaveClass("flag-confirmed");
    // A generated_image with no slots still renders an image (a square frame, a
    // default alt) and defaults to the generate layout, no crash.
    const img = render(<ToolView payload={payload({ view: "generated_image" })} />);
    const el = img.container.querySelector("img") as HTMLImageElement;
    expect(el).toHaveAttribute("alt", "Generated image");
    expect((img.container.querySelector(".tv-genimg-frame") as HTMLElement).style.aspectRatio).toBe(
      "512 / 512",
    );
  });
});
