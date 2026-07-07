import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentModelSheet } from "./AgentModelSheet";

// The sheet reads the on-box models from the live client; stub it to the two-model
// box the tests exercise (one loaded, one not).
vi.mock("../api/client", () => ({
  api: {
    getLlmSettings: vi.fn(async () => ({
      local_models: [
        { id: "gpt-oss-120b", label: "GPT-OSS 120B", loaded: true },
        { id: "qwen3-vl-30b", label: "Qwen3-VL 30B", loaded: false },
      ],
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
});
