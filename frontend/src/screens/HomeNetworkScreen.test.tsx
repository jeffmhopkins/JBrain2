import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type HomeNetSummary, api } from "../api/client";
import { HomeNetworkScreen, ago, deviceKind, networkState, share } from "./HomeNetworkScreen";

const LAPTOP = {
  ip: "192.168.1.30",
  name: "Laptop",
  queries: 600,
  recent: 200,
  recent_blocked: 10,
};

function summary(over: Partial<HomeNetSummary> = {}): HomeNetSummary {
  return {
    running: true,
    protection_enabled: true,
    resumes_in_s: 0,
    dns_addresses: ["192.168.1.10", "fe80::1"],
    version: "v0.107.79",
    queries: 1000,
    blocked: 250,
    devices: [
      LAPTOP,
      { ip: "192.168.1.41", name: "Bedroom TV", queries: 400, recent: 200, recent_blocked: 150 },
    ],
    top_blocked: [],
    top_queried: [],
    ...over,
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.spyOn(api, "homeNetwork").mockResolvedValue(summary());
  vi.spyOn(api, "homeNetworkRules").mockResolvedValue({ blocked: [], allowed: [] });
  vi.spyOn(api, "homeNetworkDevice").mockResolvedValue({
    ip: "192.168.1.41",
    name: "Bedroom TV",
    queries: 400,
    recent: 200,
    recent_blocked: 150,
    top_blocked: [{ domain: "lgtvsdp.com", count: 90, list: "Perflyst Smart TV" }],
  });
  vi.spyOn(api, "homeNetworkQueries").mockResolvedValue({
    rows: [
      {
        time: new Date().toISOString(),
        client: "192.168.1.41",
        client_name: "Bedroom TV",
        domain: "netflix.com",
        type: "A",
        blocked: false,
        reason: "NotFilteredNotFound",
        rule: "",
        list_id: 0,
      },
    ],
    oldest: "",
  });
});

afterEach(() => vi.restoreAllMocks());

describe("helpers", () => {
  it("rounds a share and never divides by zero", () => {
    expect(share(1, 3)).toBe(33);
    expect(share(5, 0)).toBe(0);
  });

  it("tells a TV and a phone from their names", () => {
    expect(deviceKind("Bedroom TV")).toBe("tv");
    expect(deviceKind("Roku")).toBe("tv");
    expect(deviceKind("Quorra's tablet")).toBe("phone");
    expect(deviceKind("192.168.1.30")).toBe("other");
  });

  it("reads one device as the router forwarding and none as not set up", () => {
    expect(networkState(summary())).toBe("normal");
    expect(networkState(summary({ devices: [LAPTOP] }))).toBe("router");
    expect(networkState(summary({ devices: [] }))).toBe("setup");
  });

  it("says how long ago", () => {
    const now = Date.parse("2026-11-01T12:00:00Z");
    expect(ago("2026-11-01T11:55:00Z", now)).toBe("5 min ago");
    expect(ago("2026-11-01T09:00:00Z", now)).toBe("3 h ago");
    expect(ago("nonsense", now)).toBe("");
  });
});

describe("HomeNetworkScreen", () => {
  it("ranks devices by how much of their recent traffic was blocked", async () => {
    render(<HomeNetworkScreen />);
    await flush();
    expect(screen.getByText("Blocking ads and trackers")).toBeTruthy();
    const rows = screen.getAllByRole("button").filter((b) => b.className === "hn-row");
    expect(rows.map((r) => r.textContent?.includes("Bedroom TV"))).toEqual([true, false]);
    expect(within(rows[0] as HTMLElement).getByText("75%")).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText(/150 of 200 recent blocked/)).toBeTruthy();
  });

  it("explains a router that forwards every lookup", async () => {
    vi.mocked(api.homeNetwork).mockResolvedValue(summary({ devices: [LAPTOP] }));
    render(<HomeNetworkScreen />);
    await flush();
    expect(screen.getByText("Every lookup comes from one address")).toBeTruthy();
  });

  it("tells the owner what to set when nothing uses it yet", async () => {
    vi.mocked(api.homeNetwork).mockResolvedValue(summary({ devices: [] }));
    render(<HomeNetworkScreen />);
    await flush();
    expect(screen.getByText(/Primary DNS to 192\.168\.1\.10/)).toBeTruthy();
  });

  it("says the DNS is down rather than showing an empty screen", async () => {
    vi.mocked(api.homeNetwork).mockRejectedValue(new ApiError(503, "house DNS unreachable"));
    render(<HomeNetworkScreen />);
    await flush();
    expect(screen.getByText("The house's DNS is not answering")).toBeTruthy();
    expect(screen.getByText(/house DNS unreachable/)).toBeTruthy();
  });

  it("pauses blocking for a while from the switch", async () => {
    const set = vi.spyOn(api, "setHomeNetworkProtection").mockResolvedValue(false);
    render(<HomeNetworkScreen />);
    await flush();
    fireEvent.click(screen.getByRole("switch", { name: "Blocking" }));
    fireEvent.click(screen.getByText("For 10 minutes"));
    await flush();
    expect(set).toHaveBeenCalledWith(false, 10);
    expect(screen.getByText("Blocking paused for 10 minutes")).toBeTruthy();
  });

  it("turns a paused switch straight back on", async () => {
    vi.mocked(api.homeNetwork).mockResolvedValue(
      summary({ protection_enabled: false, resumes_in_s: 300 }),
    );
    const set = vi.spyOn(api, "setHomeNetworkProtection").mockResolvedValue(true);
    render(<HomeNetworkScreen />);
    await flush();
    expect(screen.getByText("Back on by itself in 5 min")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Blocking" }));
    await flush();
    expect(set).toHaveBeenCalledWith(true, 0);
  });

  it("opens a device: who it tried to reach and its recent lookups", async () => {
    render(<HomeNetworkScreen />);
    await flush();
    fireEvent.click(screen.getByText("Bedroom TV"));
    await flush();
    expect(screen.getByText("Who it tried to reach")).toBeTruthy();
    expect(screen.getByText("Caught by Perflyst Smart TV")).toBeTruthy();
    expect(api.homeNetworkQueries).toHaveBeenCalledWith({
      client: "192.168.1.41",
      blocked: true,
      limit: 50,
    });
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await flush();
    expect(api.homeNetworkQueries).toHaveBeenLastCalledWith({
      client: "192.168.1.41",
      blocked: false,
      limit: 50,
    });
    fireEvent.click(screen.getByRole("button", { name: "Back to all devices" }));
    expect(screen.getByText("Devices · most tracking first")).toBeTruthy();
  });

  it("allows a blocked name for every device, then can undo it", async () => {
    const set = vi
      .spyOn(api, "setHomeNetworkRule")
      .mockResolvedValue({ blocked: [], allowed: ["lgtvsdp.com"] });
    render(<HomeNetworkScreen />);
    await flush();
    fireEvent.click(screen.getByText("Bedroom TV"));
    await flush();
    fireEvent.click(screen.getByText("lgtvsdp.com"));
    fireEvent.click(screen.getByText("Allow for every device"));
    await flush();
    expect(set).toHaveBeenCalledWith("lgtvsdp.com", "allow");
    expect(screen.getByText("Allowed lgtvsdp.com for every device")).toBeTruthy();

    fireEvent.click(screen.getByText("lgtvsdp.com"));
    expect(screen.getByText("you allowed it")).toBeTruthy();
    expect(screen.getByText("Block for every device")).toBeTruthy();
    set.mockResolvedValue({ blocked: [], allowed: [] });
    fireEvent.click(screen.getByText("Go back to what the lists say"));
    await flush();
    expect(set).toHaveBeenLastCalledWith("lgtvsdp.com", "clear");
  });

  it("blocks a name that got through", async () => {
    const set = vi
      .spyOn(api, "setHomeNetworkRule")
      .mockResolvedValue({ blocked: ["netflix.com"], allowed: [] });
    render(<HomeNetworkScreen />);
    await flush();
    fireEvent.click(screen.getByText("Bedroom TV"));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await flush();
    fireEvent.click(screen.getByText("netflix.com"));
    fireEvent.click(screen.getByText("Block for every device"));
    await flush();
    expect(set).toHaveBeenCalledWith("netflix.com", "block");
  });

  it("renames a device and shows the new name", async () => {
    const rename = vi.spyOn(api, "nameHomeNetworkDevice").mockResolvedValue();
    render(<HomeNetworkScreen />);
    await flush();
    fireEvent.click(screen.getByText("Bedroom TV"));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    fireEvent.change(screen.getByLabelText("Device name"), { target: { value: "  LG TV " } });
    vi.mocked(api.homeNetwork).mockResolvedValue(
      summary({
        devices: [
          { ip: "192.168.1.41", name: "LG TV", queries: 400, recent: 200, recent_blocked: 150 },
          LAPTOP,
        ],
      }),
    );
    vi.mocked(api.homeNetworkDevice).mockResolvedValue({
      ip: "192.168.1.41",
      name: "LG TV",
      queries: 400,
      recent: 200,
      recent_blocked: 150,
      top_blocked: [],
    });
    fireEvent.click(screen.getByText("Save"));
    await flush();
    await flush();
    expect(rename).toHaveBeenCalledWith("192.168.1.41", "LG TV");
    expect(screen.getByText("Renamed to LG TV")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "LG TV" })).toBeTruthy();
  });

  it("keeps the rename sheet open with the box's reason when it refuses", async () => {
    vi.spyOn(api, "nameHomeNetworkDevice").mockRejectedValue(
      new ApiError(400, "client already exists"),
    );
    render(<HomeNetworkScreen />);
    await flush();
    fireEvent.click(screen.getByText("Bedroom TV"));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    fireEvent.click(screen.getByText("Save"));
    await flush();
    expect(screen.getByText("client already exists")).toBeTruthy();
    expect(screen.getByLabelText("Device name")).toBeTruthy();
  });
});
