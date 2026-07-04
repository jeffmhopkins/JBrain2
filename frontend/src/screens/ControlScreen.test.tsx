import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PetState } from "../api/client";
import { type ControlDeps, ControlScreen } from "./ControlScreen";

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

function makeDeps(): { deps: ControlDeps; sendPetCommand: ReturnType<typeof vi.fn> } {
  const sendPetCommand = vi.fn(async () => petState());
  const deps: ControlDeps = {
    getPet: async () => petState({ mood: "happy" }),
    sendPetCommand,
    async *petStream() {
      yield petState();
    },
  };
  return { deps, sendPetCommand };
}

describe("ControlScreen", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the pet's live status", async () => {
    const { deps } = makeDeps();
    render(<ControlScreen onClose={() => {}} deps={deps} />);
    expect(await screen.findByText(/paired to Wall/)).toBeInTheDocument();
    expect(screen.getByText("happy")).toBeInTheDocument();
  });

  it("sends a care command from a button", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<ControlScreen onClose={() => {}} deps={deps} />);
    await screen.findByText(/paired to Wall/);
    fireEvent.click(screen.getByRole("button", { name: /feed/i }));
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledWith({ action: "feed" }));
  });

  it("sends a move command when the room map is tapped", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<ControlScreen onClose={() => {}} deps={deps} />);
    await screen.findByText(/paired to Wall/);
    fireEvent.pointerDown(screen.getByRole("button", { name: /room map/i }), {
      clientX: 10,
      clientY: 10,
    });
    await waitFor(() =>
      expect(sendPetCommand).toHaveBeenCalledWith(expect.objectContaining({ action: "move" })),
    );
  });

  it("sends a say command from the talk box", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<ControlScreen onClose={() => {}} deps={deps} />);
    await screen.findByText(/paired to Wall/);
    fireEvent.change(screen.getByLabelText("Message to the pet"), {
      target: { value: "hi Blink" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send message/i }));
    await waitFor(() =>
      expect(sendPetCommand).toHaveBeenCalledWith({ action: "say", text: "hi Blink" }),
    );
  });
});
