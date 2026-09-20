import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EndpointsScreen } from "./EndpointsScreen";

// On a box with no terminal this screen is the owner's only window onto a panel plugged
// into the box's USB port, so what it says when nothing is there matters as much as what
// it says when something is.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PANEL = {
  device: "/dev/ttyACM0",
  label: "Espressif ESP32-S3 (native USB)",
  is_espressif: true,
};

function mock(ports: unknown[], extra?: (path: string) => Response | null) {
  return async (input: RequestInfo | URL): Promise<Response> => {
    const path = String(input);
    const custom = extra?.(path);
    if (custom) return custom;
    if (path === "/api/endpoint/ports") return json({ ports, flasher: true });
    if (path === "/api/endpoint/firmware")
      return json({ version: "0.2.0", url: "https://box/api/endpoint/firmware/bin" });
    return new Response(null, { status: 404 });
  };
}

describe("EndpointsScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("distinguishes 'nothing plugged in' from a broken flasher", async () => {
    fetchMock.mockImplementation(mock([]));
    render(<EndpointsScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/Nothing on USB/)).toBeTruthy();
  });

  it("says which setting is blank rather than showing an error", async () => {
    // A 503 is a configuration answer, not a fault, and the two want different words.
    fetchMock.mockImplementation(
      mock([], (path) =>
        path === "/api/endpoint/ports"
          ? json({ detail: "No panel flasher on this box — JBRAIN_ENDPOINT_URL is empty." }, 503)
          : null,
      ),
    );
    render(<EndpointsScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/No panel flasher on this box/)).toBeTruthy();
    expect(screen.getByText("JBRAIN_ENDPOINT_URL")).toBeTruthy();
  });

  it("preselects a lone panel, so the obvious case needs no choice", async () => {
    fetchMock.mockImplementation(mock([PANEL]));
    render(<EndpointsScreen onClose={vi.fn()} />);
    const radio = (await screen.findByRole("radio")) as HTMLInputElement;
    await waitFor(() => expect(radio.checked).toBe(true));
  });

  it("does NOT preselect when both twins' panels are connected", async () => {
    // Choosing for them here is how a bootloader lands on the wrong child's unit.
    fetchMock.mockImplementation(mock([PANEL, { ...PANEL, device: "/dev/ttyACM1" }]));
    render(<EndpointsScreen onClose={vi.fn()} />);
    const radios = (await screen.findAllByRole("radio")) as HTMLInputElement[];
    expect(radios).toHaveLength(2);
    expect(radios.every((r) => !r.checked)).toBe(true);
    expect(screen.getByText(/Two panels are connected/)).toBeTruthy();
  });

  it("still lists ports when the firmware status call fails", async () => {
    // Introduced twice by the same reflex (fetching the two together), and both times it
    // hid the port list — the one thing the owner cannot find out any other way.
    fetchMock.mockImplementation(
      mock([PANEL], (path) =>
        path === "/api/endpoint/firmware" ? new Response(null, { status: 500 }) : null,
      ),
    );
    render(<EndpointsScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/ESP32-S3/)).toBeTruthy();
  });

  it("will not flash without a network, and says why", async () => {
    // Firmware is deliberately NOT a precondition — it ships with the box's own checkout —
    // but a panel flashed with no network can never be updated again, and it has no cable
    // attached to it once it is in a bedroom.
    fetchMock.mockImplementation(mock([PANEL]));
    render(<EndpointsScreen onClose={vi.fn()} />);
    const button = (await screen.findByRole("button", {
      name: "Flash panel",
    })) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(true));
    expect(screen.getByText(/can never be updated again/)).toBeTruthy();
  });

  it("offers neither a file picker nor a sync button — the firmware is already here", async () => {
    // Both controls were errands standing between a plugged-in board and the button next
    // to it: an upload the owner had to perform, then a fetch they had to remember to
    // press. The firmware arrives with the box's own update, so there is nothing to do.
    fetchMock.mockImplementation(mock([PANEL]));
    const { container } = render(<EndpointsScreen onClose={vi.fn()} />);
    await screen.findByText(/ESP32-S3/);
    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(screen.queryByRole("button", { name: /sync|update firmware/i })).toBeNull();
    expect(await screen.findByText(/Firmware 0\.2\.0, already on the box/)).toBeTruthy();
  });

  it("says to run Ops -> Update when the checkout carries no firmware", async () => {
    // The owner has no terminal (CLAUDE.md #10), so the only useful thing to say here is
    // the one thing they can actually do from the PWA.
    fetchMock.mockImplementation(
      mock([PANEL], (path) =>
        path === "/api/endpoint/firmware" ? new Response(null, { status: 503 }) : null,
      ),
    );
    render(<EndpointsScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/Ops → Update/)).toBeTruthy();
  });

  it("enables the flash once a port and a network are given", async () => {
    fetchMock.mockImplementation(mock([PANEL]));
    render(<EndpointsScreen onClose={vi.fn()} />);
    await screen.findByText(/ESP32-S3/);
    fireEvent.change(screen.getByLabelText(/Network name/), { target: { value: "house" } });
    const button = (await screen.findByRole("button", {
      name: "Flash panel",
    })) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
  });

  it("offers erase as the recovery path, relabelling the action", async () => {
    fetchMock.mockImplementation(mock([PANEL]));
    render(<EndpointsScreen onClose={vi.fn()} />);
    await screen.findByText(/ESP32-S3/);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(await screen.findByRole("button", { name: "Erase and flash" })).toBeTruthy();
    expect(screen.getByText(/hold BOOT/)).toBeTruthy();
  });
});
