import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotPolicy } from "../api/client";
import { GrowbotDrive, headingWords } from "./GrowbotDrive";

// Drive: the picked trained walk, started for a moment and then steered a couple of times a
// second, each steer keeping it going.

const CAMERA: GrowbotPolicy = {
  id: "8d1c2f4e-0a3b-4c5d-9e6f-7a8b9c0d1e2f",
  name: "Camera walk",
  source_job: "j-1",
  source_spec: "growbot_camera_walk",
  bytes: 40960,
  created_at: "2026-11-13T10:00:00Z",
  contract: { facing: "camera", speed: true, pitch: true, control_hz: 25 },
};

type Call = { url: string; body: unknown };

function serve(move: unknown = { ok: true, state: "started", why: "" }, steer = 202) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: JSON.parse(String(init?.body)) });
    return url.endsWith("/steer")
      ? new Response(steer === 202 ? null : JSON.stringify({ detail: "Nobody is driving it." }), {
          status: steer,
          headers: { "Content-Type": "application/json" },
        })
      : new Response(JSON.stringify(move), { headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function drive(over: Partial<Parameters<typeof GrowbotDrive>[0]> = {}) {
  const props = {
    code: "gb-3fa91c",
    policy: CAMERA,
    disabled: false,
    halted: 0,
    onSaid: vi.fn(),
    onStop: vi.fn(),
    ...over,
  };
  return { props, view: render(<GrowbotDrive {...props} />) };
}

describe("GrowbotDrive", () => {
  it("starts the walk, then steers it with the sliders until told to stop", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = serve();
    const { props } = drive();
    fireEvent.change(screen.getByLabelText("Heading"), { target: { value: "-30" } });
    expect(screen.getByText("30° left")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "0.5" } });
    fireEvent.change(screen.getByLabelText("Camera height"), { target: { value: "-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Drive" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0]).toEqual({
      url: "/api/growbot/robots/gb-3fa91c/move",
      body: { move: "forward", secs: 2, policy: CAMERA.id },
    });
    // Left on the slider is a positive heading for the creature.
    expect(calls[1]).toEqual({
      url: "/api/growbot/robots/gb-3fa91c/steer",
      body: { heading: 30, speed: 0.5, pitch: -1 },
    });
    expect(props.onSaid).toHaveBeenCalledWith({ text: "Driving with Camera walk.", bad: false });
    fireEvent.change(screen.getByLabelText("Heading"), { target: { value: "45" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(calls.at(-1)?.body).toEqual({ heading: -45, speed: 0.5, pitch: -1 });
    fireEvent.click(screen.getByRole("button", { name: "Stop driving" }));
    expect(props.onStop).toHaveBeenCalledOnce();
    const sent = calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls).toHaveLength(sent);
  });

  it("offers only the commands the walk takes", () => {
    serve();
    drive({ policy: { ...CAMERA, contract: { ...CAMERA.contract, speed: false, pitch: false } } });
    expect(screen.getByLabelText("Heading")).toBeInTheDocument();
    expect(screen.queryByLabelText("Speed")).toBeNull();
    expect(screen.queryByLabelText("Camera height")).toBeNull();
  });

  it("says why when the walk won't start, and when steering is refused", async () => {
    serve({ ok: false, state: "blocked", why: "robot not answering" });
    const { props } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Drive" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenCalledWith({ text: "robot not answering", bad: true }),
    );
    expect(screen.getByRole("button", { name: "Drive" })).toHaveAttribute("aria-pressed", "false");
  });

  it("stops steering when the pad sends a move of its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = serve();
    const { props, view } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Drive" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    view.rerender(<GrowbotDrive {...props} halted={1} />);
    expect(screen.getByRole("button", { name: "Drive" })).toBeInTheDocument();
    const sent = calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls).toHaveLength(sent);
  });

  it("ends the drive when a steer is refused", async () => {
    serve(undefined, 409);
    const { props } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Drive" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenLastCalledWith({ text: expect.any(String), bad: true }),
    );
    expect(screen.getByRole("button", { name: "Drive" })).toBeInTheDocument();
  });
});

describe("headingWords", () => {
  it("names the way from where it began", () => {
    expect(headingWords(0)).toBe("straight on");
    expect(headingWords(-90)).toBe("90° left");
    expect(headingWords(180)).toBe("180° right");
  });
});
