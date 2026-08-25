import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentModelSheet } from "./AgentModelSheet";

// The sheet reads the on-box models (and agent.turn's effective effort — the
// "(default)" marker) from the live client; stub it to the two-model box the tests
// exercise (one loaded reasoning model, one not loaded).
vi.mock("../api/client", () => ({
  api: {
    getLlmSettings: vi.fn(async () => ({
      local_models: [
        { id: "gpt-oss-120b", label: "GPT-OSS 120B", loaded: true, supports_reasoning: true },
        { id: "qwen3-vl-30b", label: "Qwen3-VL 30B", loaded: false, supports_reasoning: false },
      ],
      tasks: [{ id: "agent.turn", reasoning_effort: "medium" }],
    })),
  },
}));

describe("AgentModelSheet", () => {
  it("lists the loaded models and picks one for the conversation", async () => {
    const onChoose = vi.fn();
    const onClose = vi.fn();
    render(<AgentModelSheet selected={null} onChoose={onChoose} onClose={onClose} />);

    // Only the loaded model is offered (plus Automatic); the unloaded one is hidden.
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());
    expect(screen.getByText("Automatic")).toBeInTheDocument();
    expect(screen.queryByText("Qwen3-VL 30B")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("GPT-OSS 120B"));
    expect(onChoose).toHaveBeenCalledWith({ id: "gpt-oss-120b", label: "GPT-OSS 120B" });
    expect(onClose).toHaveBeenCalled();
  });

  it("clears back to the default via the Automatic row", async () => {
    const onChoose = vi.fn();
    const onClose = vi.fn();
    render(
      <AgentModelSheet
        selected={{ id: "gpt-oss-120b", label: "GPT-OSS 120B" }}
        onChoose={onChoose}
        onClose={onClose}
      />,
    );
    await waitFor(() => expect(screen.getByText("Automatic")).toBeInTheDocument());
    // The active pick reads as pressed.
    expect(screen.getByText("GPT-OSS 120B").closest("button")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByText("Automatic"));
    expect(onChoose).toHaveBeenCalledWith(null);
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the current pick visible even after it's unloaded", async () => {
    render(
      <AgentModelSheet
        // A pick that isn't in the loaded set (unloaded since chosen) still shows so
        // it can be seen and cleared.
        selected={{ id: "some-other", label: "Some Other" }}
        onChoose={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByText("Some Other")).toBeInTheDocument());
    expect(screen.getByText("not loaded")).toBeInTheDocument();
  });

  it("marks the route's default level and selects it absent an override", async () => {
    render(<AgentModelSheet selected={null} onChoose={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());

    // agent.turn's effective effort (medium) carries the "(default)" marker and
    // reads as the pressed pill while no level is armed; there is no "Auto" pill.
    expect(screen.queryByRole("button", { name: "Auto" })).not.toBeInTheDocument();
    const def = screen.getByRole("button", { name: /Medium/ });
    expect(def).toHaveTextContent("(default)");
    expect(def).toHaveAttribute("aria-pressed", "true");
  });

  it("carries an armed reasoning level onto the model pick", async () => {
    const onChoose = vi.fn();
    const onClose = vi.fn();
    render(<AgentModelSheet selected={null} onChoose={onChoose} onClose={onClose} />);
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());

    // A reasoning model is offerable → the level pills show. Arming a level does
    // not close the sheet (there is no pick to apply it to yet).
    fireEvent.click(screen.getByRole("button", { name: "High" }));
    expect(onChoose).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("GPT-OSS 120B"));
    expect(onChoose).toHaveBeenCalledWith({
      id: "gpt-oss-120b",
      label: "GPT-OSS 120B",
      effort: "high",
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("re-applies a level change live to the active reasoning pick", async () => {
    const onChoose = vi.fn();
    const onClose = vi.fn();
    render(
      <AgentModelSheet
        selected={{ id: "gpt-oss-120b", label: "GPT-OSS 120B", effort: "high" }}
        onChoose={onChoose}
        onClose={onClose}
      />,
    );
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());
    // The current level reads as pressed.
    expect(screen.getByRole("button", { name: "High" })).toHaveAttribute("aria-pressed", "true");

    // Tapping a level applies to the active pick without closing; the
    // "(default)"-marked pill clears the override so the route's own effort applies.
    fireEvent.click(screen.getByRole("button", { name: "Low" }));
    expect(onChoose).toHaveBeenCalledWith({
      id: "gpt-oss-120b",
      label: "GPT-OSS 120B",
      effort: "low",
    });
    fireEvent.click(screen.getByRole("button", { name: /Medium/ }));
    expect(onChoose).toHaveBeenLastCalledWith({ id: "gpt-oss-120b", label: "GPT-OSS 120B" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not carry a level onto a non-reasoning model", async () => {
    const onChoose = vi.fn();
    render(
      <AgentModelSheet
        // The vision model is the pick this time, so a non-reasoning row is active.
        selected={{ id: "qwen3-vl-30b", label: "Qwen3-VL 30B" }}
        onChoose={onChoose}
        onClose={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen3-VL 30B")).toBeInTheDocument());
    // Arming a level does nothing to the non-reasoning pick…
    fireEvent.click(screen.getByRole("button", { name: "High" }));
    expect(onChoose).not.toHaveBeenCalled();
    // …and re-picking the row leaves the level off the pick.
    fireEvent.click(screen.getByText("Qwen3-VL 30B"));
    expect(onChoose).toHaveBeenCalledWith({ id: "qwen3-vl-30b", label: "Qwen3-VL 30B" });
  });
});
