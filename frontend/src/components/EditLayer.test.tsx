import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EditLayer } from "./EditLayer";

const NOTE = {
  id: "n1",
  body: "original body",
  domain: "health",
  createdAt: new Date("2026-06-10T09:41:00"),
};

describe("EditLayer (focused writer)", () => {
  it("shows the whisper context and live counts, gating done on real change", () => {
    const onSave = vi.fn();
    render(<EditLayer editing={NOTE} onCancel={vi.fn()} onSave={onSave} />);
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
    render(<EditLayer editing={NOTE} onCancel={vi.fn()} onSave={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "done" })).toBeDisabled();
  });

  it("closes silently when clean; arms a discard confirm when dirty", () => {
    const onCancel = vi.fn();
    render(<EditLayer editing={NOTE} onCancel={onCancel} onSave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(onCancel).toHaveBeenCalledTimes(1); // armed, not closed
    fireEvent.click(screen.getByRole("button", { name: "Discard edits" }));
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("typing disarms the discard confirm", () => {
    const onCancel = vi.fn();
    render(<EditLayer editing={NOTE} onCancel={onCancel} onSave={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(screen.getByText("discard edits?")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "changed more" } });
    expect(screen.queryByText("discard edits?")).not.toBeInTheDocument();
  });
});
