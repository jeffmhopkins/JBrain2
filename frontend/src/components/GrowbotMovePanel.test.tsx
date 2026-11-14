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

const QUICK = {
  id: "8d1c2f4e-0a3b-4c5d-9e6f-7a8b9c0d1e2f",
  name: "Quick trot",
  source_job: "j-1",
  source_spec: "growbot_heading_walk",
  bytes: 40960,
  created_at: "2026-11-13T10:00:00Z",
  contract: { facing: "nose", speed: false, pitch: false, control_hz: 25 },
};

// The policy library the panel reads on mount answers on its own, so `fetch` holds the moves.
function serve(reply: Response | (() => Response), library: unknown[] = []) {
  const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    typeof reply === "function" ? reply() : reply.clone(),
  );
  return withLibrary(fetch, library);
}

function withLibrary<F extends (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
  fetch: F,
  library: unknown[] = [],
): F {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) =>
    String(input).endsWith("/growbot/policies") && !init?.method
      ? json(library)
      : fetch(input, init),
  );
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
  it("runs a pad direction for the picked seconds and says so", async () => {
    const fetch = serve(json({ ok: true, state: "started", why: "" }));
    render(<GrowbotMovePanel robot={robot()} />);
    const ten = screen.getByRole("button", { name: "10 s" });
    fireEvent.click(ten);
    expect(ten).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Spin left 10 s" }));
    await screen.findByText("Spinning left for 10 s.");
    expect(sent(fetch)).toEqual({
      url: "/api/growbot/robots/gb-3fa91c/move",
      body: { move: "spin_left", secs: 10 },
    });
    // The running direction is lit, with a bar draining for its seconds.
    expect(screen.getByRole("button", { name: "Spin left 10 s" })).toHaveClass("on");
    expect(document.querySelector<HTMLElement>(".gb-move-drain")?.style.animationDuration).toBe(
      "10s",
    );
  });

  it("starts at upstream's 5 s and keeps every choice inside the box's bounds", () => {
    serve(json({}));
    render(<GrowbotMovePanel robot={robot()} />);
    expect(screen.getByRole("button", { name: "Walk forward 5 s" })).toBeEnabled();
    for (const s of ["2 s", "5 s", "10 s", "15 s"]) {
      fireEvent.click(screen.getByRole("button", { name: s }));
      expect(screen.getByRole("button", { name: `Walk backward ${s}` })).toBeEnabled();
    }
  });

  it("lets a later Stop's answer stand over a slow walk's", async () => {
    let release: (r: Response) => void = () => undefined;
    const slow = new Promise<Response>((res) => {
      release = res;
    });
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      String(init?.body).includes("forward") ? slow : json({ ok: true, state: "stopped", why: "" }),
    );
    withLibrary(fetch);
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Walk forward 5 s" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await screen.findByText("Stopped.");
    release(json({ ok: true, state: "accepted", why: "" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("Stopped.")).toBeInTheDocument();
    expect(screen.queryByText(/Walking forward/)).toBeNull();
    expect(document.querySelector(".gb-move-drain")).toBeNull();
  });

  it("hands Stop to the card's shared one, and shows its answer", async () => {
    const fetch = serve(json({ ok: true, state: "started", why: "" }));
    const onStop = vi.fn(async () => ({
      text: "Stopped. The legs are limp.",
      bad: false,
    }));
    render(<GrowbotMovePanel robot={robot()} onStop={onStop} />);
    fireEvent.click(screen.getByRole("button", { name: "Walk forward 5 s" }));
    await screen.findByText("Walking forward for 5 s.");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledOnce();
    await screen.findByText("Stopped. The legs are limp.");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".gb-move-drain")).toBeNull();
  });

  it("runs the live app's tricks: the tall stance and the escape spins for the picked seconds", async () => {
    const fetch = serve(json({ ok: true, state: "accepted", why: "" }));
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Stand tall" }));
    await screen.findByText("Standing tall.");
    expect(sent(fetch).body).toEqual({ move: "stand_tall", secs: 0 });
    fireEvent.click(screen.getByRole("button", { name: "10 s" }));
    fireEvent.click(screen.getByRole("button", { name: "Escape spin right 10 s" }));
    await screen.findByText("Escape-spinning right for 10 s.");
    expect(sent(fetch, 1).body).toEqual({
      move: "spin_escape_right",
      secs: 10,
    });
  });

  it("offers the soul's own saved gestures, played by name", async () => {
    const fetch = serve(json({ ok: true, state: "accepted", why: "" }));
    render(<GrowbotMovePanel robot={robot({ gestures: ["bow", "happy dance", "wiggle"] })} />);
    // The stock three already have their buttons; only the soul's own get new ones.
    expect(screen.getAllByRole("button", { name: "Bow" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "happy dance" }));
    await screen.findByText("Playing happy dance.");
    expect(sent(fetch).body).toEqual({
      move: "gesture",
      secs: 0,
      name: "happy dance",
    });
  });

  it("shows no saved-gestures row when the soul has none of its own", () => {
    serve(json({}));
    render(<GrowbotMovePanel robot={robot({ gestures: ["wiggle", "bow", "sway"] })} />);
    expect(screen.queryByText("Its saved gestures")).toBeNull();
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
    const shared = async () => ({ text: "", bad: false });
    const { rerender } = render(
      <GrowbotMovePanel robot={robot({ connected: false })} onStop={shared} />,
    );
    expect(screen.getByText("Turn the robot on to move it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Walk forward 5 s" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    rerender(<GrowbotMovePanel robot={robot({ creature: false })} />);
    expect(screen.getByText(/keep the creature on screen/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bow" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    // The shared Stop still stops the chip of a robot that is on with no creature.
    rerender(<GrowbotMovePanel robot={robot({ creature: false })} onStop={shared} />);
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("drives a trained walk from the library, steering by heading", async () => {
    localStorage.clear();
    const fetch = serve(json({ ok: true, state: "started", why: "" }), [QUICK]);
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Quick trot" }));
    expect(screen.getByRole("button", { name: "Quick trot" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText(/nose first, heading only/)).toBeInTheDocument();
    // Heading only: down turns it around rather than backing away.
    expect(screen.getByRole("button", { name: "Turn around 5 s" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Turn left 5 s" }));
    await screen.findByText("Turning left with Quick trot for 5 s.");
    expect(sent(fetch).body).toEqual({
      move: "spin_left",
      secs: 5,
      policy: QUICK.id,
    });
    // Tricks stay the creature's own.
    fireEvent.click(screen.getByRole("button", { name: "Wiggle" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(sent(fetch, 1).body).toEqual({ move: "wiggle", secs: 0 });
  });

  it("turns with a turn policy and runs a trick policy, hiding the ones switched off", async () => {
    localStorage.clear();
    const SPIN = {
      ...QUICK,
      id: "s-1",
      name: "Tight spin",
      source_spec: "spin",
      motion: "turn",
    };
    const LOOK = {
      ...QUICK,
      id: "l-1",
      name: "Look ahead",
      source_spec: "look",
      motion: "trick",
    };
    const GONE = {
      ...QUICK,
      id: "g-1",
      name: "Old look",
      source_spec: "old",
      motion: "trick",
      enabled: false,
    };
    const fetch = serve(json({ ok: true, state: "started", why: "" }), [SPIN, LOOK, GONE]);
    render(<GrowbotMovePanel robot={robot()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Tight spin" }));
    expect(localStorage.getItem("gb-turn-with:gb-3fa91c")).toBe("s-1");
    // The turn takes the left and right keys; forward stays the creature's own.
    expect(screen.getByRole("button", { name: "Walk forward 5 s" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Turn left 5 s" }));
    await screen.findByText("Turning left with Tight spin for 5 s.");
    expect(sent(fetch).body).toEqual({
      move: "spin_left",
      secs: 5,
      policy: "s-1",
    });

    fireEvent.click(screen.getByRole("button", { name: "Look ahead 5 s" }));
    await screen.findByText("Running Look ahead for 5 s.");
    expect(sent(fetch, 1).body).toEqual({
      move: "look",
      secs: 5,
      policy: "l-1",
    });
    expect(screen.queryByRole("button", { name: /Old look/ })).toBeNull();
  });

  it("remembers the walk per robot and falls back when it's gone", async () => {
    localStorage.clear();
    localStorage.setItem("gb-walk-with:gb-3fa91c", QUICK.id);
    serve(json({}), [QUICK]);
    const { unmount } = render(<GrowbotMovePanel robot={robot()} />);
    await screen.findByRole("button", { name: "Walk ahead 5 s" });
    unmount();
    serve(json({}), []);
    render(<GrowbotMovePanel robot={robot()} />);
    expect(screen.getByRole("button", { name: "Its own walks" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Walk forward 5 s" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Add a trained walk…" })).toBeInTheDocument();
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
