import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OwnerDevice } from "../api/client";
import { publicOrigin, setPublicOrigin } from "../publicOrigin";
import { DevicesSettings, POLL_MS, seenText } from "./DevicesSettings";

// What has to hold: a waiting phone is a banner whose two buttons confirm or revoke it, the
// add-a-phone card counts down and shows the box's 409, a phone renames and revokes in two
// taps, the two home-network saves say where to go on a 403, and the list is polled only
// while something is about to change.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function phone(over: Partial<OwnerDevice>): OwnerDevice {
  return {
    id: "p1",
    label: "Pixel 9",
    enrolled_at: "2026-10-01T00:00:00Z",
    last_seen_at: null,
    last_ingress: null,
    revoked: false,
    confirmed: true,
    words: ["amber", "harbor", "willow"],
    ...over,
  };
}

const PENDING = phone({
  id: "p2",
  label: "Pixel 9 Pro",
  confirmed: false,
  words: ["ember", "falcon", "river", "lantern", "quartz"],
});

interface Opts {
  devices?: OwnerDevice[];
  armStatus?: number;
  urlStatus?: number;
  subnetStatus?: number;
  publicUrl?: { url: string; env_url: string };
}

function fakeBox(opts: Opts = {}) {
  const state = { devices: opts.devices ?? [phone({})], lists: 0 };
  const calls: { method: string; path: string; body?: unknown }[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path, body });
    if (path === "/api/owner-devices") {
      state.lists += 1;
      return json(state.devices);
    }
    if (path === "/api/owner-devices/enroll/arm") {
      if (opts.armStatus === 409) {
        return json({ detail: "a phone was just added; compare its words at the box first" }, 409);
      }
      return json({ expires_at: new Date(Date.now() + 120_000).toISOString() });
    }
    if (path === "/api/owner-devices/enroll/subnet") {
      if (method === "PUT") {
        if (opts.subnetStatus === 403) return json({ detail: "change this" }, 403);
        if (opts.subnetStatus === 422) return json({ detail: "not a subnet" }, 422);
        return json({ subnet: body.subnet });
      }
      return json({ subnet: "192.168.1.0/24" });
    }
    if (path === "/api/owner-devices/public-url") {
      if (method === "PUT") {
        if (opts.urlStatus === 403) return json({ detail: "change this" }, 403);
        return json({ url: body.url, env_url: "" });
      }
      return json(opts.publicUrl ?? { url: "", env_url: "https://env.example.net" });
    }
    const m = path.match(/^\/api\/owner-devices\/([^/]+)(\/confirm|\/revoke)?$/);
    if (m) {
      const target = state.devices.find((d) => d.id === m[1]);
      if (target && m[2] === "/confirm") target.confirmed = true;
      if (target && m[2] === "/revoke") target.revoked = true;
      if (target && !m[2]) target.label = body.label;
      return new Response(null, { status: 204 });
    }
    return json({ detail: "unexpected" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { state, calls };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setPublicOrigin(null);
});

describe("DevicesSettings", () => {
  it("shows a waiting phone as a banner with its words, and Confirm confirms it", async () => {
    const { calls } = fakeBox({ devices: [phone({}), { ...PENDING }] });
    render(<DevicesSettings />);
    const banner = await screen.findByRole("region", { name: "Pixel 9 Pro wants in" });
    expect(within(banner).getByText("quartz")).toBeInTheDocument();
    expect(within(banner).getByText(/can't reach your box until you confirm/)).toBeInTheDocument();
    fireEvent.click(within(banner).getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(calls).toContainEqual(
        expect.objectContaining({ method: "POST", path: "/api/owner-devices/p2/confirm" }),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Pixel 9 Pro wants in" })).toBeNull(),
    );
    expect(screen.getByText("Your phones · 2")).toBeInTheDocument();
  });

  it("revokes a waiting phone on Not mine", async () => {
    const { calls } = fakeBox({ devices: [{ ...PENDING }] });
    render(<DevicesSettings />);
    const banner = await screen.findByRole("region", { name: "Pixel 9 Pro wants in" });
    fireEvent.click(within(banner).getByRole("button", { name: "Not mine" }));
    await waitFor(() =>
      expect(calls).toContainEqual(
        expect.objectContaining({ method: "POST", path: "/api/owner-devices/p2/revoke" }),
      ),
    );
    expect(await screen.findByText("Revoked: Pixel 9 Pro")).toBeInTheDocument();
  });

  it("counts down once armed, and Cancel returns the card to idle", async () => {
    fakeBox();
    render(<DevicesSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Show the code on the box" }));
    expect(await screen.findByText("Scan the QR on the box's screen")).toBeInTheDocument();
    expect(screen.getByLabelText(/^1(19|20) seconds left$/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Show the code on the box" })).toBeInTheDocument();
  });

  it("shows the box's 409 detail when arming is refused", async () => {
    fakeBox({ armStatus: 409 });
    render(<DevicesSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Show the code on the box" }));
    expect(
      await screen.findByText("a phone was just added; compare its words at the box first"),
    ).toBeInTheDocument();
  });

  it("lists confirmed phones with last-seen, and says None yet when there are none", async () => {
    fakeBox({
      devices: [
        phone({ last_seen_at: new Date(Date.now() - 120_000).toISOString(), last_ingress: "lan" }),
        phone({ id: "old", label: "Old Pixel 6", revoked: true }),
      ],
    });
    const { unmount } = render(<DevicesSettings />);
    expect(await screen.findByText("last seen 2 min ago · via lan")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText("Revoked: Old Pixel 6")).toBeInTheDocument();
    unmount();
    fakeBox({ devices: [] });
    render(<DevicesSettings />);
    expect(await screen.findByText("None yet.")).toBeInTheDocument();
  });

  it("renames a phone inline, refusing a blank name", async () => {
    const { calls } = fakeBox();
    render(<DevicesSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Rename" }));
    const input = screen.getByLabelText("Phone name");
    fireEvent.change(input, { target: { value: "  " } });
    const row = input.closest("li") as HTMLElement;
    expect(within(row).getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "Quorra's Pixel" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "PATCH",
        path: "/api/owner-devices/p1",
        body: { label: "Quorra's Pixel" },
      }),
    );
    expect(await screen.findByText("Quorra's Pixel")).toBeInTheDocument();
  });

  it("revokes a phone only on the second tap", async () => {
    const { calls } = fakeBox();
    render(<DevicesSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(calls.some((c) => c.path.endsWith("/revoke"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Tap to confirm" }));
    await waitFor(() =>
      expect(calls).toContainEqual(
        expect.objectContaining({ method: "POST", path: "/api/owner-devices/p1/revoke" }),
      ),
    );
    expect(await screen.findByText("None yet.")).toBeInTheDocument();
  });

  it("names the setup address when no public address is set", async () => {
    fakeBox();
    render(<DevicesSettings />);
    expect(
      await screen.findByText("Using https://env.example.net from the box's setup."),
    ).toBeInTheDocument();
  });

  it("saves the public address and updates the link origin", async () => {
    const { calls } = fakeBox();
    render(<DevicesSettings />);
    const input = await screen.findByLabelText("Public address");
    fireEvent.change(input, { target: { value: "https://brain.example.com" } });
    const card = input.closest("section") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText("Saved.")).toBeInTheDocument();
    expect(calls).toContainEqual({
      method: "PUT",
      path: "/api/owner-devices/public-url",
      body: { url: "https://brain.example.com" },
    });
    expect(publicOrigin()).toBe("https://brain.example.com");
  });

  it("says to change the public address from home on a 403", async () => {
    fakeBox({ urlStatus: 403 });
    render(<DevicesSettings />);
    const input = await screen.findByLabelText("Public address");
    const card = input.closest("section") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText("Change this from the home network.")).toBeInTheDocument();
    expect(publicOrigin()).toBe(window.location.origin);
  });

  it("saves the home subnet, and shows a 403 and a 422 in its words", async () => {
    const ok = fakeBox();
    const { unmount } = render(<DevicesSettings />);
    let input = await screen.findByLabelText("Home subnet");
    await waitFor(() => expect(input).toHaveValue("192.168.1.0/24"));
    fireEvent.change(input, { target: { value: "10.0.0.0/8" } });
    let card = input.closest("section") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText("Saved.")).toBeInTheDocument();
    expect(ok.calls).toContainEqual({
      method: "PUT",
      path: "/api/owner-devices/enroll/subnet",
      body: { subnet: "10.0.0.0/8" },
    });
    unmount();

    fakeBox({ subnetStatus: 403 });
    const second = render(<DevicesSettings />);
    input = await screen.findByLabelText("Home subnet");
    card = input.closest("section") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText("Change this from the home network.")).toBeInTheDocument();
    second.unmount();

    fakeBox({ subnetStatus: 422 });
    render(<DevicesSettings />);
    input = await screen.findByLabelText("Home subnet");
    card = input.closest("section") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText("not a subnet")).toBeInTheDocument();
  });

  it("polls the list while a phone waits, stops once it is confirmed, and cleans up", async () => {
    vi.useFakeTimers();
    const { state } = fakeBox({ devices: [{ ...PENDING }] });
    const flush = () => act(async () => vi.advanceTimersByTimeAsync(0));
    const { unmount } = render(<DevicesSettings />);
    await flush();
    expect(state.lists).toBe(1);
    await act(async () => vi.advanceTimersByTimeAsync(POLL_MS));
    expect(state.lists).toBe(2);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await flush();
    const settled = state.lists;
    await act(async () => vi.advanceTimersByTimeAsync(POLL_MS * 3));
    expect(state.lists).toBe(settled);

    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("polls while armed and ends the countdown when a phone scans", async () => {
    vi.useFakeTimers();
    const { state } = fakeBox({ devices: [] });
    const flush = () => act(async () => vi.advanceTimersByTimeAsync(0));
    render(<DevicesSettings />);
    await flush();
    expect(state.lists).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Show the code on the box" }));
    await flush();
    expect(screen.getByText("Scan the QR on the box's screen")).toBeInTheDocument();
    state.devices = [{ ...PENDING }];
    await act(async () => vi.advanceTimersByTimeAsync(POLL_MS));
    expect(state.lists).toBe(2);
    expect(screen.getByRole("region", { name: "Pixel 9 Pro wants in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show the code on the box" })).toBeInTheDocument();
  });

  it("does not poll when nothing is waiting", async () => {
    vi.useFakeTimers();
    const { state } = fakeBox();
    render(<DevicesSettings />);
    await act(async () => vi.advanceTimersByTimeAsync(POLL_MS * 3));
    expect(state.lists).toBe(1);
  });
});

describe("seenText", () => {
  const now = Date.parse("2026-10-19T12:00:00Z");
  it("reads recent times relatively and older ones as a date", () => {
    expect(seenText("2026-10-19T11:59:30Z", now)).toBe("just now");
    expect(seenText("2026-10-19T11:56:00Z", now)).toBe("4 min ago");
    expect(seenText("2026-10-19T09:00:00Z", now)).toBe("3 h ago");
    expect(seenText("2026-10-10T09:00:00Z", now)).toBe(
      new Date("2026-10-10T09:00:00Z").toLocaleString(),
    );
  });
});
