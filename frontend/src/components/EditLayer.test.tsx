import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EditLayer } from "./EditLayer";

const NOTE = {
  id: "n1",
  body: "original body",
  domain: "health",
  createdAt: new Date("2026-06-10T09:41:00"),
  attachments: [],
};

const NOOPS = {
  onAddFile: vi.fn(async () => ({
    id: "a1",
    filename: "f.txt",
    mediaType: "text/plain",
    sizeBytes: 1,
    hasExtracts: false,
  })),
  onRemoveAttachment: vi.fn(async () => undefined),
};

describe("EditLayer (focused writer)", () => {
  it("shows the whisper context and live counts, gating done on real change", () => {
    const onSave = vi.fn();
    render(<EditLayer editing={NOTE} onCancel={vi.fn()} onSave={onSave} {...NOOPS} />);
    expect(screen.getByText(/health ·/)).toBeInTheDocument();
    const done = screen.getByRole("button", { name: "done" });
    expect(done).toBeDisabled();
    expect(screen.getByText(/2 words · 13 chars/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Note body"), {
      target: { value: "  corrected body  " },
    });
    expect(screen.getByText(/· unsaved/)).toBeInTheDocument();
    fireEvent.click(done);
    expect(onSave).toHaveBeenCalledWith("corrected body");
  });

  it("never enables done for an empty body", () => {
    render(<EditLayer editing={NOTE} onCancel={vi.fn()} onSave={vi.fn()} {...NOOPS} />);
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "done" })).toBeDisabled();
  });

  it("closes silently when clean; arms a discard confirm when dirty", () => {
    const onCancel = vi.fn();
    render(<EditLayer editing={NOTE} onCancel={onCancel} onSave={vi.fn()} {...NOOPS} />);
    fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(onCancel).toHaveBeenCalledTimes(1); // armed, not closed
    fireEvent.click(screen.getByRole("button", { name: "Discard edits" }));
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("removes an attachment with a tap-again confirm", async () => {
    const onRemoveAttachment = vi.fn(async () => undefined);
    render(
      <EditLayer
        editing={{
          ...NOTE,
          attachments: [
            {
              id: "a9",
              filename: "lab.pdf",
              mediaType: "application/pdf",
              sizeBytes: 9,
              hasExtracts: false,
            },
          ],
        }}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        {...NOOPS}
        onRemoveAttachment={onRemoveAttachment}
      />,
    );
    const chip = screen.getByRole("button", { name: /lab.pdf/ });
    fireEvent.click(chip);
    expect(onRemoveAttachment).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /remove\?/ }));
    expect(onRemoveAttachment).toHaveBeenCalledWith("a9");
    await screen.findByLabelText("Note body"); // settle state updates
  });

  it("typing disarms the discard confirm", () => {
    const onCancel = vi.fn();
    render(<EditLayer editing={NOTE} onCancel={onCancel} onSave={vi.fn()} {...NOOPS} />);
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(screen.getByText("discard edits?")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "changed more" } });
    expect(screen.queryByText("discard edits?")).not.toBeInTheDocument();
  });
});
