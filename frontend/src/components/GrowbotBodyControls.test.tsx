import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotCal, GrowbotRobot } from "../api/client";
import { GB_CAL_DEFAULTS, GrowbotBodyControls } from "./GrowbotBodyControls";

// The Body tab's Legs pane is upstream's tester leg check, saved on the box per robot: every
// change saves, and each move goes to the chip so it can be judged by eye.

const QUORRA: GrowbotRobot = {
  code: "gb-3fa91c",
  name: "Quorra",
  created_at: "2026-11-01T09:00:00Z",
  rotated_at: null,
  revoked_at: null,
  last_seen_at: null,
  connected: true,
};

interface Call {
  method: string;
  path: string;
  body: unknown;
}

function serve(cal: GrowbotCal = GB_CAL_DEFAULTS, saved = false, actStatus = 200) {
  const calls: Call[] = [];
  let now = { cal, saved };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method, path, body });
      if (path.endsWith("/cal") && method === "PUT") {
        now = { cal: body as GrowbotCal, saved: true };
        return Response.json(now);
      }
      if (path.endsWith("/cal")) return Response.json(now);
      if (path.endsWith("/act")) {
        return actStatus === 200
          ? Response.json({ sent: true })
          : Response.json({ detail: "the robot is not on the relay" }, { status: actStatus });
      }
      return new Response(null, { status: 404 });
    }),
  );
  return calls;
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT").map((c) => c.body);
const acts = (calls: Call[]) => calls.filter((c) => c.path.endsWith("/act")).map((c) => c.body);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("GrowbotBodyControls", () => {
  it("shows Legs alone without a Move pane, and says when nothing was saved yet", async () => {
    serve();
    render(<GrowbotBodyControls robot={QUORRA} />);
    expect(await screen.findByText(/Not set from here yet/)).toBeTruthy();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByRole("heading", { name: "Controls for Quorra" })).toBeTruthy();
  });

  it("checks which leg is which with upstream's moves", async () => {
    const calls = serve();
    render(<GrowbotBodyControls robot={QUORRA} />);
    fireEvent.click(await screen.findByRole("button", { name: "Move left leg" }));
    await screen.findByText("Moved the left leg.");
    fireEvent.click(screen.getByRole("button", { name: "Move right leg" }));
    await screen.findByText("Moved the right leg.");
    expect(acts(calls)).toEqual([
      {
        kind: "act",
        steps: [
          { l: 125, r: 90, ms: 450 },
          { l: 90, r: 90, ms: 400 },
        ],
      },
      {
        kind: "act",
        steps: [
          { l: 90, r: 125, ms: 450 },
          { l: 90, r: 90, ms: 400 },
        ],
      },
    ]);
  });

  it("saves a trim once the hand stops and stands the legs at it", async () => {
    const calls = serve();
    render(<GrowbotBodyControls robot={QUORRA} />);
    const slider = await screen.findByLabelText("Left trim");
    vi.useFakeTimers();
    fireEvent.change(slider, { target: { value: "3" } });
    fireEvent.change(slider, { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Right trim down one degree" }));
    expect(puts(calls)).toEqual([]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    vi.useRealTimers();
    await screen.findByText("Saved. Legs at rest, trim left 5°, right -1°.");
    expect(puts(calls)).toEqual([{ ...GB_CAL_DEFAULTS, L_OFF: 5, R_OFF: -1 }]);
    expect(acts(calls)).toEqual([{ kind: "act", steps: [{ l: 95, r: 89, ms: 300 }] }]);
    expect(screen.getByText(/Saved on the box for this robot/)).toBeTruthy();
  });

  it("keeps a trim inside upstream's ±30°", async () => {
    serve({ ...GB_CAL_DEFAULTS, L_OFF: 30 }, true);
    render(<GrowbotBodyControls robot={QUORRA} />);
    fireEvent.click(await screen.findByRole("button", { name: "Left trim up one degree" }));
    expect((screen.getByLabelText("Left trim") as HTMLInputElement).value).toBe("30");
  });

  it("saves the swap and the signs at once, and sweeps by the leg's sign", async () => {
    const calls = serve();
    render(<GrowbotBodyControls robot={QUORRA} />);
    fireEvent.click(await screen.findByLabelText(/Swap left and right/));
    await screen.findByText("Left/right swap on. Test each leg again.");
    fireEvent.click(screen.getByText(/Advanced/));
    fireEvent.click(screen.getByRole("button", { name: "Left leg −1" }));
    await screen.findByText("Left leg sign is now +1.");
    fireEvent.click(screen.getByRole("button", { name: "IMU pitch −" }));
    await screen.findByText("IMU pitch sign flipped.");
    expect(puts(calls).at(-1)).toEqual({
      ...GB_CAL_DEFAULTS,
      SWAP: 1,
      L_SIGN: 1,
      IMU_SIGN: [1, 1, 1],
    });
    fireEvent.click(screen.getByRole("button", { name: "Sweep left forward" }));
    await screen.findByText("Swept the left leg forward.");
    expect(acts(calls).at(-1)).toEqual({
      kind: "act",
      steps: [
        { l: 120, r: 90, ms: 500 },
        { l: 90, r: 90, ms: 400 },
      ],
    });
  });

  it("resets to the stock body only on a second tap", async () => {
    const calls = serve({ ...GB_CAL_DEFAULTS, L_OFF: 7, SWAP: 1 }, true);
    render(<GrowbotBodyControls robot={QUORRA} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reset calibration" }));
    expect(puts(calls)).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Tap again to reset" }));
    await screen.findByText("Calibration reset to the stock body's.");
    expect(puts(calls)).toEqual([GB_CAL_DEFAULTS]);
  });

  it("lets a robot that is off be calibrated but not moved", async () => {
    serve();
    render(<GrowbotBodyControls robot={{ ...QUORRA, connected: false }} />);
    expect(await screen.findByText("Turn the robot on to move it.")).toBeTruthy();
    for (const name of ["Move left leg", "Sweep right forward", "Stop"]) {
      expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
    }
    expect((screen.getByLabelText("Left trim") as HTMLInputElement).disabled).toBe(false);
  });

  it("says why a move did not go", async () => {
    serve(GB_CAL_DEFAULTS, false, 409);
    render(<GrowbotBodyControls robot={QUORRA} />);
    fireEvent.click(await screen.findByRole("button", { name: "Move left leg" }));
    await waitFor(() => expect(document.querySelector(".gb-bad-t")).toBeTruthy());
  });

  it("stops the chip and tells the Move pane too", async () => {
    const calls = serve();
    const onStop = vi.fn();
    render(<GrowbotBodyControls robot={QUORRA} move={<p>move pane</p>} onStop={onStop} />);
    expect(screen.getByText("move pane")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledOnce();
    await screen.findByText("Stopped. The legs are limp.");
    expect(acts(calls)).toEqual([{ kind: "stop" }]);
    fireEvent.click(screen.getByRole("tab", { name: "Legs" }));
    expect(await screen.findByRole("button", { name: "Move left leg" })).toBeTruthy();
  });

  it("shows nothing for a revoked robot", () => {
    serve();
    const { container } = render(
      <GrowbotBodyControls robot={{ ...QUORRA, revoked_at: "2026-11-11T00:00:00Z" }} />,
    );
    expect(container.innerHTML).toBe("");
  });
});
