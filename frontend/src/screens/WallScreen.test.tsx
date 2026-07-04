import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PetState } from "../api/client";
import { type WallDeps, WallScreen } from "./WallScreen";

// The WebGL scene is jsdom-untestable, so mock the render-glue module and capture the
// handlers the screen wires (poke / click-to-walk) to assert they issue commands —
// the same convention as leafletMap in LocationScreen.test.
const { sceneUpdate, sceneDestroy, handlers } = vi.hoisted(() => ({
  sceneUpdate: vi.fn(),
  sceneDestroy: vi.fn(),
  handlers: {
    current: null as null | { onPoke: () => void; onFloor: (x: number, z: number) => void },
  },
}));
vi.mock("./petScene", () => ({
  createPetScene: (
    _gl: HTMLCanvasElement,
    _bloom: HTMLCanvasElement,
    h: { onPoke: () => void; onFloor: (x: number, z: number) => void },
  ) => {
    handlers.current = h;
    return { update: sceneUpdate, destroy: sceneDestroy };
  },
}));

function petState(over: Partial<PetState> = {}): PetState {
  return {
    name: "Blink",
    domain: "general",
    food: 78,
    energy: 82,
    fun: 70,
    love: 74,
    mood: "happy",
    emotion: "happy",
    speech: null,
    asleep: false,
    pos_x: 0,
    pos_z: 0,
    target_x: 0,
    target_z: 0,
    facing: 0,
    action: "idle",
    ...over,
  };
}

function makeDeps(): { deps: WallDeps; sendPetCommand: ReturnType<typeof vi.fn> } {
  const sendPetCommand = vi.fn(async () => petState({ action: "eat" }));
  const deps: WallDeps = {
    getPet: async () => petState(),
    sendPetCommand,
    // eslint-disable-next-line require-yield
    async *petStream() {
      yield petState({ mood: "happy" });
    },
  };
  return { deps, sendPetCommand };
}

describe("WallScreen", () => {
  beforeEach(() => {
    sceneUpdate.mockClear();
    sceneDestroy.mockClear();
    handlers.current = null;
  });

  it("renders the pet's live status from the stream", async () => {
    const { deps } = makeDeps();
    render(<WallScreen onClose={() => {}} deps={deps} />);
    expect(await screen.findByText("BLINK")).toBeInTheDocument();
    // The streamed state is pushed into the scene.
    await waitFor(() => expect(sceneUpdate).toHaveBeenCalled());
  });

  it("sends a care command when a control is tapped", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<WallScreen onClose={() => {}} deps={deps} />);
    await screen.findByText("BLINK");
    fireEvent.click(screen.getByRole("button", { name: /feed/i }));
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledWith({ action: "feed" }));
  });

  it("turns scene input into move / poke commands", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<WallScreen onClose={() => {}} deps={deps} />);
    await screen.findByText("BLINK");
    handlers.current?.onFloor(0.5, -0.3);
    handlers.current?.onPoke();
    await waitFor(() => {
      expect(sendPetCommand).toHaveBeenCalledWith({ action: "move", x: 0.5, z: -0.3 });
      expect(sendPetCommand).toHaveBeenCalledWith({ action: "poke" });
    });
  });

  it("tears the scene down on unmount", async () => {
    const { deps } = makeDeps();
    const { unmount } = render(<WallScreen onClose={() => {}} deps={deps} />);
    await screen.findByText("BLINK");
    unmount();
    expect(sceneDestroy).toHaveBeenCalled();
  });
});
