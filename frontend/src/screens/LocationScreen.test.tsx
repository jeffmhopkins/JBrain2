import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DeviceSummary, ProvisionedDevice, TimelineEntry } from "../api/client";
import { type LocationDeps, LocationScreen, relativeTime, sentence } from "./LocationScreen";

function device(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    id: "d1",
    label: "Jeff's phone",
    created_at: "2026-06-01T00:00:00+00:00",
    revoked: false,
    last_seen: new Date(Date.now() - 5 * 60_000).toISOString(),
    battery_pct: 72,
    connection: "wifi",
    fix_count: 140,
    ...over,
  };
}

function provisioned(over: Partial<ProvisionedDevice> = {}): ProvisionedDevice {
  return {
    device: { id: "new", label: "Tablet", created_at: "2026-06-18T00:00:00+00:00", revoked: false },
    key: "SECRET-KEY-123",
    ...over,
  };
}

function entry(over: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    occurred_at: new Date(Date.now() - 35 * 60_000).toISOString(),
    subject_id: "d1",
    transition: "exit",
    place_entity_id: "p1",
    place_name: "Office",
    ...over,
  };
}

function deps(over: Partial<LocationDeps> = {}): LocationDeps {
  return {
    listDevices: vi.fn(async () => [device()]),
    provisionDevice: vi.fn(async () => provisioned()),
    rotateDevice: vi.fn(async () => "ROTATED-KEY-456"),
    revokeDevice: vi.fn(async () => {}),
    listTimeline: vi.fn(async () => [entry()]),
    ...over,
  };
}

describe("LocationScreen", () => {
  it("lands on Devices and renders a device card with its status", async () => {
    render(<LocationScreen deps={deps()} />);
    await screen.findByText("Jeff's phone");
    const meta = screen.getByText(/last seen/);
    expect(meta.textContent).toMatch(/72% battery/);
    expect(meta.textContent).toMatch(/wifi/);
    expect(meta.textContent).toMatch(/140 fixes/);
  });

  it("shows the empty state when there are no devices", async () => {
    render(<LocationScreen deps={deps({ listDevices: vi.fn(async () => []) })} />);
    expect(await screen.findByText(/no devices yet/)).toBeInTheDocument();
  });

  it("placeholders the Map tab until its wave", async () => {
    render(<LocationScreen deps={deps()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    expect(screen.getByText(/map arrives in a later wave/i)).toBeInTheDocument();
  });

  it("renders the timeline as natural sentences naming the device", async () => {
    const d = deps({
      listTimeline: vi.fn(async () => [
        entry({ subject_id: "d1", transition: "exit", place_name: "Office" }),
        entry({ subject_id: "d2", transition: "enter", place_name: "Mom's house" }),
      ]),
      listDevices: vi.fn(async () => [
        device({ id: "d1", label: "Jeff's phone" }),
        device({ id: "d2", label: "Celine's phone" }),
      ]),
    });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(await screen.findByText("Jeff's phone left Office")).toBeInTheDocument();
    expect(screen.getByText("Celine's phone arrived at Mom's house")).toBeInTheDocument();
  });

  it("shows the timeline empty state when there are no crossings", async () => {
    const d = deps({ listTimeline: vi.fn(async () => []) });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(await screen.findByText(/no movement yet/i)).toBeInTheDocument();
  });

  it("provisions a device and reveals its key once with OwnTracks config", async () => {
    const d = deps({ listDevices: vi.fn(async () => []) });
    render(<LocationScreen deps={d} />);
    await screen.findByText("＋ Add device");
    fireEvent.click(screen.getByText("＋ Add device"));

    fireEvent.change(screen.getByLabelText("Device name"), { target: { value: "Tablet" } });
    fireEvent.click(screen.getByRole("button", { name: "Add device" }));

    await waitFor(() => expect(d.provisionDevice).toHaveBeenCalledWith("Tablet"));
    // The key is shown once (as the Key line + the OwnTracks password), alongside
    // the endpoint URL and the once-only warning.
    expect((await screen.findAllByText("SECRET-KEY-123")).length).toBeGreaterThan(0);
    expect(screen.getByText(/api\/owntracks/)).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
  });

  it("rotates a key and reveals the new one", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    await screen.findByText("Jeff's phone");
    fireEvent.click(screen.getByRole("button", { name: "Rotate key" }));
    await waitFor(() => expect(d.rotateDevice).toHaveBeenCalledWith("d1"));
    expect((await screen.findAllByText("ROTATED-KEY-456")).length).toBeGreaterThan(0);
  });

  it("revokes only after a confirm", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    await screen.findByText("Jeff's phone");
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    // The confirm sheet — revoke fires only on its destructive button.
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/can no longer post fixes/i)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(d.revokeDevice).toHaveBeenCalledWith("d1"));
  });

  it("hides key actions for a revoked device", async () => {
    render(
      <LocationScreen
        deps={deps({ listDevices: vi.fn(async () => [device({ revoked: true })]) })}
      />,
    );
    await screen.findByText("Jeff's phone");
    expect(screen.getByText("revoked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rotate key" })).toBeNull();
  });
});

describe("sentence", () => {
  const labels = new Map([["d1", "Jeff's phone"]]);
  it("maps a crossing to a plain verb sentence", () => {
    expect(sentence(entry({ transition: "exit", place_name: "Office" }), labels)).toBe(
      "Jeff's phone left Office",
    );
    expect(sentence(entry({ transition: "enter", place_name: "Home" }), labels)).toBe(
      "Jeff's phone arrived at Home",
    );
  });
  it("falls back to a generic who when the device is unknown", () => {
    expect(sentence(entry({ subject_id: "gone", transition: "enter" }), labels)).toBe(
      "A device arrived at Office",
    );
  });
});

describe("relativeTime", () => {
  it("buckets recent timestamps", () => {
    expect(relativeTime(new Date(Date.now() - 30_000).toISOString())).toBe("just now");
    expect(relativeTime(new Date(Date.now() - 5 * 60_000).toISOString())).toBe("5m ago");
    expect(relativeTime(new Date(Date.now() - 3 * 3_600_000).toISOString())).toBe("3h ago");
    expect(relativeTime(new Date(Date.now() - 2 * 86_400_000).toISOString())).toBe("2d ago");
    expect(relativeTime("not-a-date")).toBe("unknown");
  });
});
