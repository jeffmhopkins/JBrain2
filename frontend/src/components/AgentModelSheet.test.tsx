import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentModelSheet } from "./AgentModelSheet";

// The sheet reads the on-box models (and agent.turn's effective effort — the
// "(default)" marker) from the live client; stub it to the two-model box the tests
// exercise (one loaded reasoning model, one not loaded). agent.turn reasons at medium,
// so the effort control is offered even on the default route.
vi.mock("../api/client", () => ({
  api: {
    getLlmSettings: vi.fn(async () => ({
      local_models: [
        { id: "gpt-oss-120b", label: "GPT-OSS 120B", loaded: true, supports_reasoning: true },
        { id: "qwen3-vl-30b", label: "Qwen3-VL 30B", loaded: false, supports_reasoning: false },
      ],
      tasks: [{ id: "agent.turn", reasoning_effort: "medium" }],
      reasoning_default: "low",
    })),
  },
}));

const noop = () => {};

describe("AgentModelSheet", () => {
  it("lists the loaded models and picks one for the conversation", async () => {
    const onChooseModel = vi.fn();
    const onClose = vi.fn();
    render(
      <AgentModelSheet
        model={null}
        effort={null}
        onChooseModel={onChooseModel}
        onChooseEffort={noop}
        onClose={onClose}
      />,
    );

    // Only the loaded model is offered (plus Automatic); the unloaded one is hidden.
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());
    expect(screen.getByText("Automatic")).toBeInTheDocument();
    expect(screen.queryByText("Qwen3-VL 30B")).not.toBeInTheDocument();

    // The model pick no longer bundles a reasoning level — that's a separate override.
    fireEvent.click(screen.getByText("GPT-OSS 120B"));
    expect(onChooseModel).toHaveBeenCalledWith({ id: "gpt-oss-120b", label: "GPT-OSS 120B" });
    expect(onClose).toHaveBeenCalled();
  });

  it("clears back to the default via the Automatic row", async () => {
    const onChooseModel = vi.fn();
    const onClose = vi.fn();
    render(
      <AgentModelSheet
        model={{ id: "gpt-oss-120b", label: "GPT-OSS 120B" }}
        effort={null}
        onChooseModel={onChooseModel}
        onChooseEffort={noop}
        onClose={onClose}
      />,
    );
    await waitFor(() => expect(screen.getByText("Automatic")).toBeInTheDocument());
    expect(screen.getByText("GPT-OSS 120B").closest("button")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByText("Automatic"));
    expect(onChooseModel).toHaveBeenCalledWith(null);
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the current pick visible even after it's unloaded", async () => {
    render(
      <AgentModelSheet
        model={{ id: "some-other", label: "Some Other" }}
        effort={null}
        onChooseModel={noop}
        onChooseEffort={noop}
        onClose={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("Some Other")).toBeInTheDocument());
    expect(screen.getByText("not loaded")).toBeInTheDocument();
  });

  it("marks the route's default level and selects it absent an override", async () => {
    render(
      <AgentModelSheet
        model={null}
        effort={null}
        onChooseModel={noop}
        onChooseEffort={noop}
        onClose={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());

    // agent.turn's effective effort (medium) carries the "(default)" marker and reads as
    // the pressed pill while no override is set; there is no "Auto" pill.
    expect(screen.queryByRole("button", { name: "Auto" })).not.toBeInTheDocument();
    const def = screen.getByRole("button", { name: /Medium/ });
    expect(def).toHaveTextContent("(default)");
    expect(def).toHaveAttribute("aria-pressed", "true");
  });

  it("sets a reasoning level on the DEFAULT route without a model and without closing", async () => {
    // The bug this fixes: on Automatic (model === null) a reasoning tap used to be
    // dropped. Now it persists via onChooseEffort, independent of any model pick.
    const onChooseModel = vi.fn();
    const onChooseEffort = vi.fn();
    const onClose = vi.fn();
    render(
      <AgentModelSheet
        model={null}
        effort={null}
        onChooseModel={onChooseModel}
        onChooseEffort={onChooseEffort}
        onClose={onClose}
      />,
    );
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "High" }));
    expect(onChooseEffort).toHaveBeenCalledWith("high");
    expect(onChooseModel).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("reflects the active effort and clears it via the (default) pill", async () => {
    const onChooseEffort = vi.fn();
    render(
      <AgentModelSheet
        model={null}
        effort="high"
        onChooseModel={noop}
        onChooseEffort={onChooseEffort}
        onClose={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument());
    // The active override reads as pressed.
    expect(screen.getByRole("button", { name: "High" })).toHaveAttribute("aria-pressed", "true");

    // Tapping a different level applies it; tapping the "(default)"-marked pill clears
    // the override so the route's own effort applies again.
    fireEvent.click(screen.getByRole("button", { name: "Low" }));
    expect(onChooseEffort).toHaveBeenCalledWith("low");
    fireEvent.click(screen.getByRole("button", { name: /Medium/ }));
    expect(onChooseEffort).toHaveBeenLastCalledWith(null);
  });
});
