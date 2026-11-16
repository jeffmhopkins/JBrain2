import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotPolicy, GrowbotSkill } from "../api/client";
import { GrowbotDrive, headingWords, skillWords } from "./GrowbotDrive";
import { skillSet } from "./GrowbotPoliciesSheet";

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
      ? new Response(steer === 202 ? null : JSON.stringify({ detail: "The walk ended." }), {
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
  it("starts the walk, then steers it with the dial until told to stop", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = serve();
    const { props } = drive();
    const dial = screen.getByRole("slider", { name: "Heading and speed" });
    for (let i = 0; i < 6; i++) fireEvent.keyDown(dial, { key: "ArrowLeft" });
    expect(dial).toHaveAttribute("aria-valuetext", "30° left, speed 0.5");
    fireEvent.change(screen.getByLabelText("Camera height"), { target: { value: "-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Start driving" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    const id = (calls[0]?.body as { drive: string }).drive;
    expect(id).toMatch(/^[0-9a-f]{16}$/);
    expect(calls[0]).toEqual({
      url: "/api/growbot/robots/gb-3fa91c/move",
      body: { move: "forward", secs: 2, policy: CAMERA.id, drive: id },
    });
    // The steering names the walk its move started; left on the slider is a positive heading.
    expect(calls[1]).toEqual({
      url: "/api/growbot/robots/gb-3fa91c/steer",
      body: { drive: id, heading: 30, speed: 0.5, pitch: -1 },
    });
    expect(props.onSaid).toHaveBeenCalledWith({ text: "Driving with Camera walk.", bad: false });
    for (let i = 0; i < 15; i++) fireEvent.keyDown(dial, { key: "ArrowRight" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(calls.at(-1)?.body).toEqual({ drive: id, heading: -45, speed: 0.5, pitch: -1 });
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
    // A heading-only walk's arrow is all direction: no length, and no reverse to switch to.
    const dial = screen.getByRole("slider", { name: "Heading" });
    // Drawn full length from the start, as it is sent.
    expect(dial.querySelector(".gb-dial-tip")).toHaveAttribute("cy", "16");
    fireEvent.keyDown(dial, { key: "ArrowDown" });
    expect(dial).toHaveAttribute("aria-valuetext", "straight");
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByLabelText("Camera height")).toBeNull();
  });

  it("says why when the walk won't start, and when steering is refused", async () => {
    serve({ ok: false, state: "blocked", why: "robot not answering" });
    const { props } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Start driving" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenCalledWith({ text: "robot not answering", bad: true }),
    );
    expect(screen.getByRole("button", { name: "Start driving" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("stops steering when the pad sends a move of its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = serve();
    const { props, view } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Start driving" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    view.rerender(<GrowbotDrive {...props} halted={1} />);
    expect(screen.getByRole("button", { name: "Start driving" })).toBeInTheDocument();
    const sent = calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls).toHaveLength(sent);
  });

  it("never steers from a panel that went away while the walk was starting", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let answer: (r: Response) => void = () => undefined;
    const calls: string[] = [];
    vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Promise<Response>((res) => {
        answer = res;
      });
    });
    const { view } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Start driving" }));
    view.unmount();
    answer(new Response(JSON.stringify({ ok: true, state: "started", why: "" })));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls).toEqual(["/api/growbot/robots/gb-3fa91c/move"]);
  });

  it("ends the drive, as news rather than a fault, when its walk has ended on the phone", async () => {
    serve(undefined, 410);
    const { props } = drive();
    fireEvent.click(screen.getByRole("button", { name: "Start driving" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenLastCalledWith({ text: "The walk ended.", bad: false }),
    );
    expect(screen.getByRole("button", { name: "Start driving" })).toBeInTheDocument();
  });
});

describe("headingWords", () => {
  it("names the way from where it began", () => {
    expect(headingWords(0)).toBe("straight");
    expect(headingWords(-90)).toBe("90° left");
    expect(headingWords(180)).toBe("180° right");
  });

  it("drives a skill set by default, starting it standing, and can switch to the walk", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = serve();
    const set = { look: "11111111-1111-4111-8111-111111111111", back: CAMERA.id };
    const heading: GrowbotPolicy = {
      ...CAMERA,
      contract: { ...CAMERA.contract, speed: false, pitch: false },
    };
    const { props } = drive({ policy: heading, skills: set });
    expect(screen.getByText(/^Steering the skill set \(look and back\)\./)).toBeInTheDocument();
    // A skill set takes speed and camera height, whatever the picked walk does.
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Start driving" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    const id = (calls[0]?.body as { drive: string }).drive;
    expect(calls[0]?.body).toEqual({ move: "look", secs: 2, drive: id, skills: set });
    expect(calls[1]?.body).toEqual({ drive: id, heading: 0, speed: -0.5, pitch: 1 });
    expect(props.onSaid).toHaveBeenCalledWith({
      text: "Driving the skill set (look and back).",
      bad: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "Stop driving" }));
    fireEvent.click(screen.getByRole("button", { name: `Steer ${CAMERA.name} instead` }));
    expect(screen.getByText(/^Steering Camera walk\./)).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Heading" })).toBeInTheDocument();
  });
});

describe("GrowbotSteerDial", () => {
  it("reads a drag as the arrow's angle and length, all the way round", () => {
    serve();
    drive();
    const dial = screen.getByRole("slider", { name: "Heading and speed" });
    // The dial's box is its viewBox at 1:1, so the centre is (120, 120) and full length 104.
    dial.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 240 }) as DOMRect;
    fireEvent.pointerDown(dial, { clientX: 120 + 52, clientY: 120, pointerId: 1 });
    expect(dial).toHaveAttribute("aria-valuetext", "90° right, speed 0.5");
    fireEvent.pointerMove(dial, { clientX: 120, clientY: 120 - 200, pointerId: 1 });
    expect(dial).toHaveAttribute("aria-valuetext", "straight, speed 1.0");
    // Behind it turns it round; just left of behind is 175° left.
    fireEvent.pointerMove(dial, { clientX: 120, clientY: 120 + 104, pointerId: 1 });
    expect(dial).toHaveAttribute("aria-valuetext", "180° right, speed 1.0");
    fireEvent.pointerMove(dial, { clientX: 120 - 9, clientY: 120 + 104, pointerId: 1 });
    expect(dial).toHaveAttribute("aria-valuetext", "175° left, speed 1.0");
    fireEvent.pointerUp(dial, { pointerId: 1 });
    fireEvent.pointerMove(dial, { clientX: 120, clientY: 120, pointerId: 1 });
    expect(dial.getAttribute("aria-valuetext")).toMatch(/^175° left/);
    // Left keeps turning it anticlockwise, round through straight behind.
    fireEvent.keyDown(dial, { key: "ArrowLeft" });
    fireEvent.keyDown(dial, { key: "ArrowLeft" });
    expect(dial.getAttribute("aria-valuetext")).toMatch(/^175° right/);
    fireEvent.keyDown(dial, { key: "Home" });
    expect(dial.getAttribute("aria-valuetext")).toMatch(/^straight/);
  });
});

describe("skillSet", () => {
  const p = (id: string, modes: GrowbotSkill[], over = {}): GrowbotPolicy => ({
    ...CAMERA,
    id,
    contract: { ...CAMERA.contract, modes },
    ...over,
  });

  it("takes the active, else newest, enabled policy for each skill, and needs two", () => {
    const look = p("a", ["look"]);
    const old = p("b", ["forward"], { created_at: "2026-11-01T00:00:00Z" });
    const fresh = p("c", ["forward"], { created_at: "2026-11-14T00:00:00Z" });
    const off = p("d", ["back"], { enabled: false });
    expect(skillSet([look, old, fresh, off])).toEqual({ look: "a", forward: "c" });
    expect(skillSet([look, { ...old, active: true }, fresh])).toEqual({ look: "a", forward: "b" });
    expect(skillSet([look, off])).toBeNull();
    // Back goes to the nose walk even when a newer camera walk can reverse.
    const nose = p("e", ["back"], {
      contract: { ...CAMERA.contract, facing: "nose", modes: ["back"] },
    });
    const master = p("f", ["look", "forward", "back"], { created_at: "2026-11-15T00:00:00Z" });
    expect(skillSet([nose, master])).toEqual({ look: "f", forward: "f", back: "e" });
    // An older box sends no modes: no skill set.
    expect(skillSet([CAMERA])).toBeNull();
    expect(skillWords({ look: "a", forward: "b", back: "c" })).toBe("look, forward and back");
  });
});
