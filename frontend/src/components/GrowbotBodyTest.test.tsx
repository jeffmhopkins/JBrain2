import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotBodyTestRun } from "../api/client";
import { GrowbotBodyTest } from "./GrowbotBodyTest";

// Body test: started from the Body tab as a skill set's look with follow "test", mirrored from
// the box while it runs on the robot's phone, stopped with a plain stop, and its past runs listed
// with their logs.

const SKILLS = { look: "11111111-1111-4111-8111-111111111111" };

function run(over: Partial<GrowbotBodyTestRun> = {}): GrowbotBodyTestRun {
  const at = new Date().toISOString();
  return {
    number: 7,
    run: "bt-mhz8k2-a1b2c3",
    robot: "gb-3fa91c",
    started_at: at,
    updated_at: at,
    status: "done",
    skills: SKILLS,
    summary: { secs: 291, results: { tilt: { v: "warn", why: "reversed?" } } },
    rows: 2914,
    ...over,
  };
}

function serve(runs: () => GrowbotBodyTestRun[], move = { ok: true, state: "body_test", why: "" }) {
  const moves: unknown[] = [];
  const reads: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/bodytests")) {
      reads.push(url);
      return new Response(JSON.stringify(runs()), {
        headers: { "Content-Type": "application/json" },
      });
    }
    moves.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify(move), { headers: { "Content-Type": "application/json" } });
  });
  return { moves, reads };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function show(over: Partial<Parameters<typeof GrowbotBodyTest>[0]> = {}) {
  const props = { code: "gb-3fa91c", skills: SKILLS, disabled: false, onSaid: vi.fn(), ...over };
  return { props, view: render(<GrowbotBodyTest {...props} />) };
}

describe("GrowbotBodyTest", () => {
  it("starts the test with the skill set and lists past runs with their logs", async () => {
    const { moves, reads } = serve(() => [run(), run({ number: 6, status: "stopped" })]);
    const { props } = show();
    expect(await screen.findByText("#7")).toBeTruthy();
    expect(reads[0]).toContain("robot=gb-3fa91c");
    expect(screen.getByText(/1 to look at/)).toBeTruthy();
    expect(screen.getByText(/stopped at setup/)).toBeTruthy();
    const logs = screen.getAllByRole("link", { name: "log" });
    expect(logs[0]?.getAttribute("href")).toBe("/api/growbot/bodytests/7");
    fireEvent.click(screen.getByRole("button", { name: "Start a body test" }));
    await waitFor(() => expect(moves).toHaveLength(1));
    expect(moves[0]).toEqual({ move: "look", secs: 0, skills: SKILLS, follow: "test" });
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenLastCalledWith({
        text: "Body test started: follow the steps on the robot's phone.",
        bad: false,
      }),
    );
    expect(screen.getByRole("button", { name: "Starting on the robot's phone…" })).toBeTruthy();
  });

  it("mirrors a running test and stops it with a plain stop", async () => {
    const live = run({
      number: 8,
      status: "running",
      summary: {
        phase: "run",
        step_n: 4,
        steps: 8,
        label: "Turn left 90°, right 90°",
        ask: "Stay where you are",
        secs: 102,
        mode: "look",
        heading: 68.4,
        face: { dist: 1.02, bearing: 21 },
        falls: 0,
      },
    });
    const { moves } = serve(() => [live]);
    const { props } = show();
    expect(await screen.findByLabelText("Body test running")).toBeTruthy();
    expect(screen.getByText("Step 4 of 8: Turn left 90°, right 90°")).toBeTruthy();
    expect(screen.getByText("01:42")).toBeTruthy();
    expect(screen.getByText("1.02 m · 21°")).toBeTruthy();
    expect(screen.getByText("Stay where you are")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop test" }));
    await waitFor(() => expect(moves[0]).toEqual({ move: "stop", secs: 0 }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenLastCalledWith({ text: "Stopped the body test.", bad: false }),
    );
  });

  it("lists a camera test as one, never as a body test under way", async () => {
    const cam = run({
      number: 9,
      run: "bt-cam-mhz8k2",
      status: "running",
      summary: { kind: "camera", phase: "run", secs: 65, label: "Stand 2 m away" },
    });
    serve(() => [cam]);
    show();
    expect(await screen.findByText(/camera test · .* · running/)).toBeTruthy();
    expect(screen.queryByLabelText("Body test running")).toBeNull();
    expect(screen.getByRole("button", { name: "Start a body test" })).toBeTruthy();
  });

  it("polls while a test runs, and shows a silent one as lost contact", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const old = new Date(Date.now() - 60_000).toISOString();
    let runs = [
      run({ status: "running", summary: { phase: "setup", label: "Power on the body" } }),
    ];
    const { reads } = serve(() => runs);
    show();
    expect(await screen.findByText("Power on the body")).toBeTruthy();
    runs = [run({ status: "running", updated_at: old, started_at: old, summary: { secs: 30 } })];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    await waitFor(() => expect(screen.getByText(/lost contact/)).toBeTruthy());
    const n = reads.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(reads.length).toBe(n);
    expect(screen.getByRole("button", { name: "Start a body test" })).toBeTruthy();
  });

  it("says why the creature refused, and works on a box that keeps no tests", async () => {
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      if (String(input).includes("/bodytests")) return new Response("{}", { status: 404 });
      return new Response(
        JSON.stringify({ ok: false, state: "", why: "a body test is already running" }),
        { headers: { "Content-Type": "application/json" } },
      );
    });
    const { props } = show();
    fireEvent.click(await screen.findByRole("button", { name: "Start a body test" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenLastCalledWith({
        text: "a body test is already running",
        bad: true,
      }),
    );
    expect(screen.queryByLabelText("Past body tests")).toBeNull();
  });
});
