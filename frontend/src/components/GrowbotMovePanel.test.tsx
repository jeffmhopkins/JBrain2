import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotRobot } from "../api/client";
import { GrowbotMovePanel, moveBlocked, stopCreature } from "./GrowbotMovePanel";

// The Body tab's remote: every move goes to the creature wearing the robot, and the panel says
// what it did or why not.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function robot(over: Partial<GrowbotRobot> = {}): GrowbotRobot {
  return {
    code: "gb-3fa91c",
    name: "Quorra",
    created_at: "2026-11-01T09:00:00Z",
    rotated_at: null,
    revoked_at: null,
    last_seen_at: null,
    connected: true,
    creature: true,
    ...over,
  };
}

function serve(reply: Response | (() => Response)) {
  const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    typeof reply === "function" ? reply() : reply.clone(),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function sent(fetch: ReturnType<typeof serve>, i = 0): { url: string; body: unknown } {
  const [url, init] = fetch.mock.calls[i] ?? [];
  return { url: String(url), body: JSON.parse(String(init?.body)) };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GrowbotMovePanel", () => {
  it("runs the picked policy for the picked seconds and says so", async () => {
    const fetch = serve(json({ ok: true, state: "started", why: "" }));
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Spin ↺" }));
    expect(screen.getByRole("button", { name: "Spin ↺" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByLabelText("Seconds"), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "Spin left 8 s" }));
    await screen.findByText("Spinning left for 8 s.");
    expect(sent(fetch)).toEqual({
      url: "/api/growbot/robots/gb-3fa91c/move",
      body: { move: "spin_left", secs: 8 },
    });
  });

  it("keeps the seconds inside the box's bounds", () => {
    serve(json({}));
    render(<GrowbotMovePanel robot={robot()} />);
    const secs = screen.getByLabelText("Seconds");
    fireEvent.change(secs, { target: { value: "99" } });
    expect(screen.getByRole("button", { name: "Walk forward 15 s" })).toBeEnabled();
    fireEvent.change(secs, { target: { value: "0" } });
    expect(screen.getByRole("button", { name: "Walk forward 1 s" })).toBeEnabled();
    // Emptied to retype, it runs the default and shows it again once left.
    fireEvent.change(secs, { target: { value: "" } });
    expect(secs).toHaveValue(null);
    fireEvent.change(secs, { target: { value: "8" } });
    expect(secs).toHaveValue(8);
    fireEvent.change(secs, { target: { value: "" } });
    fireEvent.blur(secs);
    expect(secs).toHaveValue(5);
  });

  it("lets a later Stop's answer stand over a slow walk's", async () => {
    let release: (r: Response) => void = () => undefined;
    const slow = new Promise<Response>((res) => {
      release = res;
    });
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      String(init?.body).includes("forward") ? slow : json({ ok: true, state: "stopped", why: "" }),
    );
    vi.stubGlobal("fetch", fetch);
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Walk forward 5 s" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await screen.findByText("Stopped.");
    release(json({ ok: true, state: "accepted", why: "" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("Stopped.")).toBeInTheDocument();
    expect(screen.queryByText(/Walking forward/)).toBeNull();
  });

  it("shows the creature's own words when it refuses", async () => {
    serve(json({ ok: false, state: "blocked", why: "robot not answering" }));
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Wiggle" }));
    await screen.findByText("robot not answering");
  });

  it("shows the box's refusal", async () => {
    serve(json({ detail: "The creature didn't answer. Is its page still on screen?" }, 504));
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await screen.findByText(/didn't answer/);
  });

  it("is off, and says why, while nobody can drive the robot", () => {
    const fetch = serve(json({}));
    const { rerender } = render(<GrowbotMovePanel robot={robot({ connected: false })} />);
    expect(screen.getByText("Turn the robot on to move it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Walk forward 5 s" })).toBeDisabled();
    rerender(<GrowbotMovePanel robot={robot({ creature: false })} />);
    expect(screen.getByText(/keep the creature on screen/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bow" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("leaves Stop to a shared one when asked", () => {
    serve(json({}));
    render(<GrowbotMovePanel robot={robot()} withStop={false} />);
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });
});

describe("moveBlocked", () => {
  it("names the first thing in the way", () => {
    expect(moveBlocked(robot())).toBeNull();
    expect(moveBlocked(robot({ revoked_at: "2026-11-02T00:00:00Z" }))).toMatch(/revoked/);
    expect(moveBlocked(robot({ connected: false, creature: false }))).toMatch(/Turn the robot on/);
  });
});

describe("stopCreature", () => {
  it("stops a listening creature and never throws", async () => {
    const fetch = serve(() => {
      throw new Error("offline");
    });
    stopCreature(robot());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(sent(fetch).body).toEqual({ move: "stop", secs: 0 });
  });

  it("sends nothing when no creature listens", () => {
    const fetch = serve(json({}));
    stopCreature(robot({ creature: false }));
    expect(fetch).not.toHaveBeenCalled();
  });
});
