import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isLocationCaptureEnabled } from "../location";
import { SettingsScreen } from "./SettingsScreen";

function setup() {
  render(<SettingsScreen deviceLabel="Test device" onLogout={vi.fn()} />);
}

// The screen loads the server-synced settings on mount; a stateful stub
// makes GET/PUT round-trip like the real /api/settings.
function stubSettingsFetch(initial: "full" | "ocr" = "full") {
  const state = { mode: initial };
  const puts: unknown[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    // The calendar-feed section loads its config on mount; default to disabled.
    if (path.startsWith("/api/feed/appointments")) {
      return new Response(JSON.stringify({ enabled: false, token: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (path !== "/api/settings") {
      throw new Error(`Unexpected fetch: ${path}`);
    }
    if ((init?.method ?? "GET").toUpperCase() === "PUT") {
      const body = JSON.parse(String(init?.body)) as { image_analysis_mode?: "full" | "ocr" };
      puts.push(body);
      if (body.image_analysis_mode) state.mode = body.image_analysis_mode;
    }
    return new Response(JSON.stringify({ image_analysis_mode: state.mode }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { puts, state };
}

beforeEach(() => {
  localStorage.clear();
  stubSettingsFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SettingsScreen capture location", () => {
  it("defaults the toggle to on", () => {
    setup();
    const group = screen.getByLabelText("Capture location");
    const on = group.querySelector('[aria-pressed="true"]');
    expect(on).toHaveTextContent("On");
  });

  it("persists off across remounts via localStorage", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Off" }));
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("off");
    expect(isLocationCaptureEnabled()).toBe(false);
  });

  it("persists turning it back on", () => {
    localStorage.setItem("jbrain.captureLocation", "off");
    setup();
    fireEvent.click(screen.getByRole("button", { name: "On" }));
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("on");
    expect(isLocationCaptureEnabled()).toBe(true);
  });
});

describe("SettingsScreen image analysis", () => {
  it("loads the server mode and marks it pressed (full is the default)", async () => {
    setup();
    const group = screen.getByLabelText("Image analysis");
    await waitFor(() =>
      expect(group.querySelector('[aria-pressed="true"]')).toHaveTextContent("full analysis"),
    );
    expect(screen.getByRole("button", { name: "ocr only" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("reflects a server-side ocr-only mode on load", async () => {
    stubSettingsFetch("ocr");
    setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "ocr only" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
  });

  it("saves a pick via PUT /api/settings and round-trips it", async () => {
    const { puts, state } = stubSettingsFetch("full");
    setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "full analysis" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "ocr only" }));
    // Optimistic press, then the PUT lands on the wire.
    expect(screen.getByRole("button", { name: "ocr only" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() => expect(puts).toEqual([{ image_analysis_mode: "ocr" }]));
    expect(state.mode).toBe("ocr");
  });
});

describe("SettingsScreen calendar feed", () => {
  function json(body: unknown) {
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  it("generates a subscribe link and shows the URL", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const path = String(input);
      if (path === "/api/settings") return json({ image_analysis_mode: "full" });
      if (path === "/api/feed/appointments" && (init?.method ?? "GET").toUpperCase() === "GET") {
        return json({ enabled: false, token: null });
      }
      if (path === "/api/feed/appointments/rotate") {
        return json({ enabled: true, token: "secret-tok" });
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    setup();

    // Disabled on load → a Generate button; after generating, the URL appears.
    fireEvent.click(await screen.findByRole("button", { name: "Generate link" }));
    const url = (await screen.findByLabelText("Calendar feed URL")) as HTMLInputElement;
    expect(url.value).toContain("/api/feed/appointments.ics?token=secret-tok");
  });
});

describe("SettingsScreen time zone", () => {
  it("shows the stored owner timezone when the server has one", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const path = String(input);
      if (path.startsWith("/api/feed/appointments")) {
        return new Response(JSON.stringify({ enabled: false, token: null }), { status: 200 });
      }
      return new Response(
        JSON.stringify({ image_analysis_mode: "full", owner_timezone: "America/New_York" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    setup();
    expect(await screen.findByLabelText("Time zone")).toHaveTextContent("America/New_York");
  });
});
