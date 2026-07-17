import { createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ListOut, api } from "../../api/client";
import type { ViewPayload } from "../types";
import { resetLiveLists } from "./liveList";
import { ToolView, isKnownView } from "./registry";

// Leaflet needs a real layout engine; mock the hurricane_card map glue so the Track
// tab's React behaviour (mounting, the legend, the NHC link) is what's under test, and
// the spy lets us assert the real lat/lon geometry handed to Leaflet — never rendered
// tiles (mirrors locationViews.test.tsx / screens/LocationScreen.test.tsx).
const { huMapSpy } = vi.hoisted(() => ({ huMapSpy: vi.fn() }));
vi.mock("./hurricaneMap", () => ({
  renderHurricaneMap: (...args: unknown[]) => {
    huMapSpy(...args);
    return { invalidate: vi.fn(), destroy: vi.fn() };
  },
}));

function payload(over: Partial<ViewPayload>): ViewPayload {
  return { view: "", surface: "inline", data: {}, refs: [], ...over };
}

function listOut(over: Partial<ListOut> = {}): ListOut {
  return { id: "L1", title: "Groceries", domain: "general", archived: false, items: [], ...over };
}

// Keep the shared live-list store (and api spies) from leaking across cases.
afterEach(() => {
  resetLiveLists();
  huMapSpy.mockClear();
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
    // …and each frame's thumbnail src from its blob id under the attachment.
    const thumb = container.querySelector<HTMLImageElement>(".tv-vid-frame-img");
    expect(thumb?.getAttribute("src")).toBe("/api/chat-attachments/att_123/thumb/sha-deadbeef");
    expect(container.querySelector(".tv-vid")).not.toBeNull();
    expect(screen.getByText("meeting.mp4")).toBeInTheDocument();
    expect(screen.getByText("A short standup.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Transcript" })).toBeInTheDocument();
  });

  it("renders a video_analysis card for a stream source with no <video> or thumbs", () => {
    // analyze_stream emits source:"stream" and no attachment_id — the card must not
    // fabricate a broken <video> src or thumbnail fetch; it shows summary + captions.
    const { container } = render(
      <ToolView
        payload={payload({
          view: "video_analysis",
          data: {
            source: "stream",
            media: "video",
            filename: "Starship Live",
            stream_url: "https://youtube.com/live/xyz",
            is_live: true,
            mode: "window",
            summary: "The booster is still on the mount.",
            frames: [{ t_ms: 0, caption: "Rocket on the mount.", thumb_id: "sha-x" }],
            transcript: null,
          },
        })}
      />,
    );
    expect(container.querySelector("video")).toBeNull(); // no playable local video
    expect(container.querySelector(".tv-vid-frame-img")).toBeNull(); // no served thumb
    expect(container.querySelector(".tv-vid-frame")).not.toBeNull(); // frame is a marker
    expect(screen.getByText("Starship Live")).toBeInTheDocument();
    expect(screen.getByText("The booster is still on the mount.")).toBeInTheDocument();
  });

  it("renders a weather_card from data-only slots (hero + hourly strip)", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "weather_card",
          data: {
            place: "Cocoa, Florida, United States",
            as_of: "1:14 PM",
            tz: "EDT",
            now: {
              temp_f: 90,
              feels_f: 102,
              cond: "storm",
              is_day: true,
              label: "Thunderstorms",
              wind_mph: 8,
              wind_dir: "SE",
            },
            hi_f: 92,
            lo_f: 80,
            hours: [
              { label: "1p", temp_f: 90, feels_f: 102, cond: "storm", is_day: true, pop: 20 },
              { label: "12a", temp_f: 80, feels_f: 86, cond: "clear", is_day: false, pop: 0 },
            ],
          },
        })}
      />,
    );
    expect(container.querySelector(".tv-wx")).not.toBeNull();
    expect(container.querySelector(".tv-wx-cap")?.textContent).toContain(
      "Cocoa, Florida, United States",
    );
    expect(container.querySelector(".tv-wx-temp")?.textContent).toBe("90°F");
    expect(screen.getByText("feels 102°")).toBeInTheDocument();
    expect(screen.getByText("H 92°")).toBeInTheDocument();
    // The first hour is relabeled "Now"; later hours keep their clock label.
    expect(screen.getByText("Now")).toBeInTheDocument();
    expect(screen.getByText("12a")).toBeInTheDocument();
    // A zero precip-chance cell is hidden, not shown as "0%".
    const pops = container.querySelectorAll(".tv-wx-pop");
    expect(pops).toHaveLength(2);
    expect(String(pops[0]?.className)).not.toContain("none");
    expect(String(pops[1]?.className)).toContain("none");
    // No URL/markup rides the payload (#9) — glyphs are inline SVG.
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll(".tv-wx-svg").length).toBeGreaterThan(0);
  });

  const huUsData = {
    place: "Tampa, Florida, United States",
    as_of: "Sep 10, 3:00 PM UTC",
    active_count: 2,
    coverage: "us",
    storm: {
      name: "Elena",
      kind: "hurricane",
      cat: "3",
      sustained_mph: 120,
      sustained_level: "extreme",
      gust_mph: 150,
      gust_level: "extreme",
      pressure_mb: 948,
      pressure_level: "high",
      moving: "NNE 14 mph",
    },
    distance_mi: 215,
    bearing: "SSW",
    proximity: "near",
    alert: {
      level: "warning",
      kind: "hurricane",
      event: "Hurricane Warning",
      headline: "Hurricane Warning for Tampa Bay <b>now</b>",
    },
    track: [
      { lat: 25.5, lon: -85.0, label: "Now", cat: "3", past: false },
      { lat: 27.5, lon: -84.0, label: "+24h", cat: "2", past: false },
    ],
    cone: [
      { lat: 25.0, lon: -86.0 },
      { lat: 28.0, lon: -83.0 },
      { lat: 28.0, lon: -86.0 },
    ],
    you: { lat: 27.9475, lon: -82.4584 },
    nhc_url: "https://www.nhc.noaa.gov/graphics_at2.shtml",
    timeline: [
      { label: "9 PM", wind_mph: 35, gust_mph: 50, rain_in: 0.2, peak: false },
      { label: "12 AM", wind_mph: 70, gust_mph: 100, rain_in: 0.4, peak: true },
    ],
    arrival: { ts_force: "Wed 9 PM", hurricane_force: "Thu 2 AM" },
    impact: {
      wind: { mph: 70, gust: 100, level: "high" },
      surge: { band: "Up to 9 ft", level: "high" },
      rain: { in: 8, level: "moderate" },
      timing: { onset: "Wed 9 PM", peak: "Thu 4 AM", clear: "Thu 1 PM" },
    },
  };

  it("renders a hurricane_card with the NWS warning banner, hero, and tabs", () => {
    const { container } = render(
      <ToolView payload={payload({ view: "hurricane_card", data: huUsData })} />,
    );
    expect(container.querySelector(".tv-hu-cap")?.textContent).toContain(
      "Tampa, Florida, United States",
    );
    expect(container.querySelector(".tv-hu-cap")?.textContent).toContain("2 active");
    expect(screen.getByText("Elena")).toBeInTheDocument();
    expect(screen.getByText("Cat 3")).toBeInTheDocument();
    // A real warning shows the rose danger banner (not amber watch).
    const banner = container.querySelector(".tv-hu-alert");
    expect(String(banner?.className)).toContain("warning");
    expect(banner?.textContent).toContain("Hurricane Warning");
    // The upstream headline renders as ESCAPED text — no injected <b> element (#9).
    expect(banner?.querySelector("b")?.textContent).toBe("Hurricane Warning");
    expect(banner?.innerHTML).toContain("&lt;b&gt;now&lt;/b&gt;");
    // All three tabs are offered; Timeline is the default pane.
    const tabs = Array.from(container.querySelectorAll(".tv-hu-tabs button")).map(
      (b) => b.textContent,
    );
    expect(tabs).toEqual(["Timeline", "Track", "Impact"]);
    expect(container.querySelector(".tv-hu-strip")).not.toBeNull();
    expect(container.querySelector(".tv-hu-cell.peak")?.textContent).toContain("100");
    // No injected <img> host: tiles come from the on-box proxy (mounted only on the
    // Track tab) and the only URL is the public NHC link, rendered as an <a>.
    expect(container.querySelector("img")).toBeNull();
  });

  it("switches hurricane_card tabs to the Track map and Impact grid", () => {
    const { container } = render(
      <ToolView payload={payload({ view: "hurricane_card", data: huUsData })} />,
    );
    const [, trackBtn, impactBtn] = container.querySelectorAll(".tv-hu-tabs button");
    fireEvent.click(trackBtn as Element);
    // The Track tab mounts the Leaflet map (mocked) and hands it the REAL lat/lon
    // geometry — the public track + cone and the city-centre `you` pin. Tiles come from
    // the on-box proxy, so there is still no <img> host on the payload.
    expect(container.querySelector(".tv-hu-map")).not.toBeNull();
    const mapArgs = huMapSpy.mock.calls[0];
    if (!mapArgs) throw new Error("renderHurricaneMap was not called");
    const mapData = mapArgs[1] as {
      track: { lat: number; lon: number; cat: string }[];
      cone: { lat: number; lon: number }[];
      you: { lat: number; lon: number } | null;
    };
    expect(mapData.track).toHaveLength(2);
    expect(mapData.track[0]).toMatchObject({ lat: 25.5, lon: -85.0, cat: "3" });
    expect(mapData.cone).toHaveLength(3);
    expect(mapData.you).toMatchObject({ lat: 27.9475, lon: -82.4584 });
    // The card links out to the storm's official NHC page.
    const nhc = container.querySelector(".tv-hu-nhc") as HTMLAnchorElement | null;
    expect(nhc?.getAttribute("href")).toBe("https://www.nhc.noaa.gov/graphics_at2.shtml");
    expect(nhc?.getAttribute("target")).toBe("_blank");
    fireEvent.click(impactBtn as Element);
    expect(container.querySelector(".tv-hu-grid")).not.toBeNull();
    expect(screen.getByText("Up to 9 ft")).toBeInTheDocument();
    // The Impact toggle flips to the storm's own stats, and the gauges are toned from
    // the backend severity tiers (not fixed decoration): Sustained 120 mph reads
    // extreme; Movement is a heading, so it carries no gauge bar.
    fireEvent.click(screen.getByText("Storm stats"));
    const sustained = screen.getByText("Sustained").closest(".tv-hu-icell");
    expect(String(sustained?.className)).toContain("lv-extreme");
    expect(sustained?.querySelector(".tv-hu-gauge")).not.toBeNull();
    const movement = screen.getByText("Movement").closest(".tv-hu-icell");
    expect(String(movement?.className)).toContain("lv-info");
    expect(movement?.querySelector(".tv-hu-gauge")).toBeNull();
  });

  it("degrades a global (out-of-coverage) hurricane_card to hero + Track only", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "hurricane_card",
          data: {
            place: "San Andrés, Colombia",
            coverage: "global",
            storm: {
              name: "Bret",
              kind: "tropical-storm",
              cat: "",
              sustained_mph: 60,
              moving: "W 12 mph",
            },
            distance_mi: 120,
            bearing: "E",
            proximity: "near",
            alert: null,
            track: [{ lat: 13.2, lon: -81.7, label: "Now", cat: "", past: false }],
            cone: [],
            you: { lat: 12.58, lon: -81.7 },
            nhc_url: "",
            timeline: [],
            arrival: { ts_force: null, hurricane_force: null },
            impact: {},
          },
        })}
      />,
    );
    // No alert banner; no category → the kind label is the badge.
    expect(container.querySelector(".tv-hu-alert")).toBeNull();
    expect(screen.getByText("Tropical Storm")).toBeInTheDocument();
    expect(screen.queryByText(/Cat /)).toBeNull();
    // Only the Track tab is offered (Timeline/Impact are empty and hidden), and the
    // Track pane renders its inline SVG map by default (the §4 "hero + Track only" path).
    const tabs = Array.from(container.querySelectorAll(".tv-hu-tabs button")).map(
      (b) => b.textContent,
    );
    expect(tabs).toEqual(["Track"]);
    // The Track pane mounts the (mocked) Leaflet map with the single forecast point.
    expect(container.querySelector(".tv-hu-map")).not.toBeNull();
    const mapArgs = huMapSpy.mock.calls[0];
    if (!mapArgs) throw new Error("renderHurricaneMap was not called");
    expect((mapArgs[1] as { track: unknown[] }).track).toHaveLength(1);
    // A global storm carries no NHC bin slot, so no external link is shown.
    expect(container.querySelector(".tv-hu-nhc")).toBeNull();
    expect(container.querySelector(".tv-hu-foot")?.textContent).toContain("NWS/NHC");
  });

  it("renders a weather_card week view as a daily list, not the hourly strip", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "weather_card",
          data: {
            place: "Portland, Oregon, United States",
            as_of: "9:30 AM",
            tz: "PDT",
            range: "week",
            now: { temp_f: 64, feels_f: 62, cond: "cloudy", is_day: true, label: "Overcast" },
            hi_f: 78,
            lo_f: 55,
            hours: [],
            days: [
              { label: "Today", cond: "cloudy", hi_f: 78, lo_f: 55, pop: 10, wind_mph: 8 },
              { label: "Sat", cond: "clear", hi_f: 80, lo_f: 56, pop: 0, wind_mph: 7 },
              { label: "Sun", cond: "rain", hi_f: 71, lo_f: 52, pop: 60, wind_mph: 12 },
            ],
          },
        })}
      />,
    );
    expect(container.querySelector(".tv-wx-days")).not.toBeNull();
    expect(container.querySelector(".tv-wx-strip")).toBeNull(); // no hourly strip for a week
    const rows = container.querySelectorAll(".tv-wx-day");
    expect(rows).toHaveLength(3);
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("Sun")).toBeInTheDocument();
    expect(screen.getByText("80°")).toBeInTheDocument(); // Saturday's high
    // The dry day hides its precip cell; the wet day shows it.
    const pops = container.querySelectorAll(".tv-wx-dpop");
    expect(String(pops[1]?.className)).toContain("none");
    expect(String(pops[2]?.className)).not.toContain("none");
    expect(container.querySelector("img")).toBeNull(); // inline glyphs only (#9)
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
      power_w: 14.0,
      ...over,
    });
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "server_metrics",
          data: {
            range: "24h",
            resolution: "raw",
            points: [
              point({ load_1m: 0.5 }),
              point({ load_1m: 1.5, fan_rpm_max: 2600, power_w: 31.0 }),
            ],
          },
        })}
      />,
    );
    expect(getByText("Server health · 24h")).toBeInTheDocument();
    expect(getByText("2 30s buckets")).toBeInTheDocument();
    expect(getByText("CPU load")).toBeInTheDocument();
    expect(getByText("APU power")).toBeInTheDocument();
    // Peak readout reflects the higher bucket (load 1.5, fan 2600, power 31W).
    expect(getByText("1.50 peak")).toBeInTheDocument();
    expect(getByText("2600 rpm peak")).toBeInTheDocument();
    expect(getByText("31.0 W peak")).toBeInTheDocument();
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

  it("renders a subagent_synthesis roster with a ran/failed roll-up", () => {
    expect(isKnownView("subagent_synthesis")).toBe(true);
    const { getByText, queryByText } = render(
      <ToolView
        payload={payload({
          view: "subagent_synthesis",
          data: {
            ran: 2,
            failed: 1,
            children: [
              { label: "Pricing", persona: "research", ok: true, summary: "3 tiers" },
              { label: "Security", persona: "research", ok: false, summary: "ERROR: timed out" },
            ],
          },
        })}
      />,
    );
    expect(getByText(/Synthesized from 1 of 2 · 1 failed/)).toBeInTheDocument();
    expect(getByText("Pricing")).toBeInTheDocument();
    // A successful child's summary is collapsed behind its row (no full-comment dump).
    expect(queryByText("3 tiers")).not.toBeInTheDocument();
    fireEvent.click(getByText("Pricing"));
    expect(getByText("3 tiers")).toBeInTheDocument();
    // A failed child auto-expands its error without a tap.
    expect(getByText("ERROR: timed out")).toBeInTheDocument();
  });

  it("renders the partial-synthesis variant when the fan was truncated", () => {
    const { getByText, container } = render(
      <ToolView
        payload={payload({
          view: "subagent_synthesis",
          data: {
            ran: 2,
            failed: 0,
            truncated: true,
            children: [{ label: "Pricing", persona: "research", ok: true, summary: "partial" }],
          },
        })}
      />,
    );
    expect(getByText(/Partial synthesis — research truncated/)).toBeInTheDocument();
    // The fail/truncated frame is signalled by the has-fail class (rose frame via CSS).
    expect(container.querySelector(".tv-syn.has-fail")).not.toBeNull();
  });

  it("deep-links each synthesis row to its sub-agent session when a handler is given", () => {
    const onOpenSession = vi.fn();
    const { getByLabelText, queryByLabelText } = render(
      <ToolView
        onOpenSession={onOpenSession}
        payload={payload({
          view: "subagent_synthesis",
          data: {
            ran: 2,
            failed: 0,
            children: [
              {
                label: "Pricing",
                persona: "research",
                ok: true,
                summary: "x",
                session_id: "sess-k1",
              },
              // No session_id → no link for this row (nothing to open).
              { label: "Security", persona: "research", ok: true, summary: "y" },
            ],
          },
        })}
      />,
    );
    fireEvent.click(getByLabelText("Open Pricing session"));
    expect(onOpenSession).toHaveBeenCalledWith("sess-k1");
    expect(queryByLabelText("Open Security session")).toBeNull();
  });

  it("renders no session links when no onOpenSession handler is provided", () => {
    const { queryByLabelText } = render(
      <ToolView
        payload={payload({
          view: "subagent_synthesis",
          data: {
            ran: 1,
            failed: 0,
            children: [
              {
                label: "Pricing",
                persona: "research",
                ok: true,
                summary: "x",
                session_id: "sess-k1",
              },
            ],
          },
        })}
      />,
    );
    expect(queryByLabelText("Open Pricing session")).toBeNull();
  });

  it("groups a staged feeding-waves roster by wave, with feed edges and a distinct skip", () => {
    const { getByText, getAllByText } = render(
      <ToolView
        payload={payload({
          view: "subagent_synthesis",
          data: {
            ran: 2,
            failed: 0,
            skipped: 1,
            children: [
              {
                label: "fetch-history",
                persona: "research",
                ok: true,
                summary: "commits",
                wave: 0,
                fed_from: [],
              },
              {
                label: "feature-timeline",
                persona: "review",
                ok: true,
                summary: "table",
                wave: 1,
                fed_from: ["fetch-history"],
              },
              {
                label: "process-audit",
                persona: "review",
                ok: false,
                summary: "(skipped — sub-agent budget spent by earlier waves)",
                skipped: true,
                skip_reason: "sub-agent budget spent by earlier waves",
                wave: 1,
                fed_from: ["fetch-history"],
              },
            ],
          },
        })}
      />,
    );
    // Wave dividers, the second naming its feed source (Direction 1).
    expect(getByText(/Wave 1 · research/)).toBeInTheDocument();
    expect(getByText(/Wave 2 · review — fed by wave 1/)).toBeInTheDocument();
    // The feed edge renders as text on each fed consumer (both wave-2 children).
    expect(getAllByText(/← fed by fetch-history/)).toHaveLength(2);
    // A budget-skip is surfaced distinctly (reason inline) and counted in the roll-up.
    expect(getByText(/skipped — sub-agent budget spent by earlier waves/)).toBeInTheDocument();
    expect(getByText(/1 skipped/)).toBeInTheDocument();
  });
});

describe("chart & lab_chart views", () => {
  const chartPayload = payload({
    view: "chart",
    data: {
      domain: "general",
      unit: "lb",
      title: "Body weight",
      y: { min: 170, max: 200, ticks: [175, 185, 195] },
      series: [
        {
          label: "weight",
          points: [
            { x: Date.UTC(2025, 0, 1), y: 190, note: "note:1" },
            { x: Date.UTC(2025, 5, 1), y: 185, note: "note:2" },
            { x: Date.UTC(2026, 0, 1), y: 182, note: "note:3" },
          ],
        },
      ],
    },
  });
  const labPayload = payload({
    view: "lab_chart",
    data: {
      domain: "health",
      unit: "x10^9/L",
      title: "Platelet count",
      y: { min: 80, max: 300, ticks: [100, 200, 300] },
      ref: { lo: 150, hi: 400, label: "reference 150-400" },
      series: [
        {
          label: "platelets",
          points: [
            { x: Date.UTC(2025, 0, 1), y: 210, flag: "normal", note: "note:a" },
            { x: Date.UTC(2025, 3, 1), y: 96, flag: "critical", note: "note:b" },
            { x: Date.UTC(2025, 6, 1), y: 180, flag: "normal", note: "note:c" },
          ],
        },
      ],
    },
  });

  it("registers both chart view names", () => {
    expect(isKnownView("chart")).toBe(true);
    expect(isKnownView("lab_chart")).toBe(true);
  });

  it("renders a generic chart headline + a Stats tab, no reference band", () => {
    const { container } = render(<ToolView payload={chartPayload} />);
    expect(container.querySelector(".tv-cc-now")?.textContent).toBe("182");
    // generic charts have no reference band and no Range tab
    expect(container.querySelector(".tv-plot-band")).toBeNull();
    expect(screen.queryByRole("tab", { name: "Range" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Stats" }));
    expect(screen.getByText("average")).toBeInTheDocument();
  });

  it("renders a lab_chart with a reference band, a toned critical point, and a Range tab", () => {
    const { container } = render(<ToolView payload={labPayload} />);
    expect(container.querySelector(".tv-plot-band")).toBeInTheDocument();
    expect(container.querySelector(".tv-plot-pt.critical")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Range" }));
    expect(container.querySelector(".tv-cc-gauge-track")).toBeInTheDocument();
    expect(container.querySelector(".tv-cc-gauge-mark.critical")).toBeInTheDocument();
  });

  it("scrubs with the keyboard, updating the pinned readout", () => {
    const { container } = render(<ToolView payload={labPayload} />);
    const plot = container.querySelector(".tv-plot-wrap");
    expect(plot).not.toBeNull();
    // readout starts on the last draw (180); ArrowLeft steps back to the critical low (96)
    if (plot) fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(container.querySelector(".tv-cc-rv")?.textContent).toContain("96");
  });

  it("zooms on wheel and reveals the reset control", () => {
    const { container } = render(<ToolView payload={chartPayload} />);
    expect(container.querySelector(".tv-plot-reset")).toBeNull();
    const plot = container.querySelector(".tv-plot-wrap");
    if (plot) fireEvent.wheel(plot, { deltaY: -300 });
    expect(container.querySelector(".tv-plot-reset")).toBeInTheDocument();
  });

  it("shows the Table tab rows with per-draw citations", () => {
    render(<ToolView payload={labPayload} />);
    fireEvent.click(screen.getByRole("tab", { name: "Table" }));
    expect(screen.getByText("note:b")).toBeInTheDocument();
  });

  it("renders a calm empty state when the series has no points", () => {
    render(<ToolView payload={payload({ view: "chart", data: { series: [{ points: [] }] } })} />);
    expect(screen.getByText(/No data to plot/)).toBeInTheDocument();
  });

  it("keeps a positive y-scale for a flat all-equal integer series (no NaN)", () => {
    // Regression: readYScale used to yield min===max -> divide-by-zero -> NaN coords.
    const { container } = render(
      <ToolView
        payload={payload({
          view: "chart",
          data: {
            domain: "general",
            unit: "kg",
            series: [
              {
                points: [
                  { x: Date.UTC(2025, 0, 1), y: 5 },
                  { x: Date.UTC(2025, 1, 1), y: 5 },
                ],
              },
            ],
          },
        })}
      />,
    );
    const line = container.querySelector(".tv-plot-line");
    expect(line).toBeInTheDocument();
    expect(line?.getAttribute("d") ?? "").not.toContain("NaN");
  });

  it("draws an area fill when the chart kind is area", () => {
    const { container } = render(
      <ToolView
        payload={payload({
          view: "chart",
          data: {
            domain: "general",
            unit: "mi",
            kind: "area",
            series: [
              {
                points: [
                  { x: Date.UTC(2025, 0, 1), y: 3 },
                  { x: Date.UTC(2025, 1, 1), y: 8 },
                ],
              },
            ],
          },
        })}
      />,
    );
    const area = container.querySelector(".tv-plot-area");
    expect(area).toBeInTheDocument();
    expect(area?.getAttribute("d") ?? "").toMatch(/Z$/);
  });
});
