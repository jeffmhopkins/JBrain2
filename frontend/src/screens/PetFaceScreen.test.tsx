import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { PetState } from "../api/client";
import { type PetFaceDeps, PetFaceScreen } from "./PetFaceScreen";

// Canvas is jsdom-untestable, so the renderer is mocked and `getContext` stubbed — the
// petScene/leafletMap convention. What is under test here is the API contract and the
// interaction model; the drawing itself is covered by the pure modules in `src/pet/`.
vi.mock("../pet/draw", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pet/draw")>()),
  drawScene: vi.fn(),
}));

const realGetContext = HTMLCanvasElement.prototype.getContext;
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ({})) as unknown as typeof realGetContext;
});
afterAll(() => {
  HTMLCanvasElement.prototype.getContext = realGetContext;
});

function petState(over: Partial<PetState> = {}): PetState {
  return {
    name: "Blink",
    domain: "general",
    mood: "playful",
    emotion: "curious",
    speech: null,
    asleep: false,
    pos_x: 0,
    pos_z: 0,
    target_x: 0,
    target_z: 0,
    facing: 0,
    action: "idle",
    color: null,
    script: [],
    carrying: null,
    lights_on: true,
    objects: {},
    ...over,
  };
}

/** A stream with no frames. Written as an empty loop rather than an empty body so it still
 *  contains a `yield` (biome's useYield) while producing nothing — which is the real case on a
 *  box where a proxy eats SSE and the screen has only the snapshot to work from. */
async function* noFrames(): AsyncGenerator<PetState> {
  for (const s of [] as PetState[]) yield s;
}

async function* failingStream(): AsyncGenerator<PetState> {
  for (const s of [] as PetState[]) yield s;
  throw new Error("offline");
}

function makeDeps(first: PetState = petState()): {
  deps: PetFaceDeps;
  sendPetCommand: ReturnType<typeof vi.fn>;
  getPet: ReturnType<typeof vi.fn>;
} {
  const sendPetCommand = vi.fn(async () => first);
  const getPet = vi.fn(async () => first);
  const deps: PetFaceDeps = {
    getPet,
    sendPetCommand,
    petStream: () => noFrames(),
  };
  return { deps, sendPetCommand, getPet };
}

describe("PetFaceScreen", () => {
  it("renders the panel at the hardware's native size", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const canvas = screen.getByLabelText("Pet face preview panel") as HTMLCanvasElement;
    expect(canvas.width).toBe(368);
    expect(canvas.height).toBe(448);
  });

  it("shows the server's state, including the emotion the wall ignores", async () => {
    const { deps } = makeDeps(petState({ emotion: "sleepy", color: "red" }));
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    await waitFor(() => expect(screen.getByText(/emotion sleepy/)).toBeTruthy());
    expect(screen.getByText(/colour red/)).toBeTruthy();
  });

  // The settled interaction model: one whole-screen target, one gesture, no thresholds. A
  // press and release is a poke, and it must reach the server so the wall stays in sync.
  it("sends a poke on press-and-release of the panel", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const canvas = screen.getByLabelText("Pet face preview panel");
    fireEvent.pointerDown(canvas, { clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledWith({ action: "wiggle" }));
  });

  it("does not fire a second time on a stray pointerup", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const canvas = screen.getByLabelText("Pet face preview panel");
    fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledTimes(1));
  });

  it("routes a spoken phrase through the server, not the client", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.change(screen.getByLabelText("Say to the pet"), { target: { value: "turn red" } });
    fireEvent.click(screen.getByText("Say"));
    await waitFor(() =>
      expect(sendPetCommand).toHaveBeenCalledWith({ action: "say", text: "turn red" }),
    );
  });

  it("sends a quick action as a real pet command", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.click(screen.getByText("peekaboo"));
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledWith({ action: "hide" }));
  });

  it("toggles a face override off again when pressed twice", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const silly = screen.getByText("silly");
    fireEvent.click(silly);
    expect(silly.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(silly);
    expect(silly.getAttribute("aria-pressed")).toBe("false");
  });

  it("switches the panel to true physical size", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.click(screen.getByText("True size"));
    expect(screen.getByText(/29\.0 × 35\.3 mm/)).toBeTruthy();
  });

  it("survives the pet endpoint being down", async () => {
    const deps: PetFaceDeps = {
      getPet: vi.fn(async () => {
        throw new Error("offline");
      }),
      sendPetCommand: vi.fn(async () => petState()),
      petStream: () => failingStream(),
    };
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    await waitFor(() => expect(screen.getByText(/connecting/)).toBeTruthy());
  });

  // A browser can refuse a 2D context (too many live ones, a lost GPU). The frame loop must
  // survive it rather than take the screen down, so the guard is asserted, not assumed.
  it("keeps working when the canvas refuses a 2D context", async () => {
    const spy = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => {
      throw new Error("no context");
    });
    try {
      const { deps, sendPetCommand } = makeDeps();
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      const canvas = screen.getByLabelText("Pet face preview panel");
      fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerUp(canvas, { pointerId: 1 });
      await waitFor(() => expect(sendPetCommand).toHaveBeenCalled());
    } finally {
      spy.mockRestore();
    }
  });

  it("closes", () => {
    const { deps } = makeDeps();
    const onClose = vi.fn();
    render(<PetFaceScreen onClose={onClose} deps={deps} />);
    fireEvent.click(screen.getByText("Back"));
    expect(onClose).toHaveBeenCalled();
  });
});
