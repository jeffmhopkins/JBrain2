import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EditLayer } from "./EditLayer";

describe("EditLayer", () => {
  it("loads the body, saves trimmed changes, and disables Save when unchanged", () => {
    const onSave = vi.fn();
    render(
      <EditLayer
        editing={{ id: "n1", body: "original body" }}
        onCancel={vi.fn()}
        onSave={onSave}
      />,
    );
    const area = screen.getByLabelText("Note body");
    expect(area).toHaveValue("original body");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.change(area, { target: { value: "  corrected body  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith("corrected body");
  });

  it("cancels via the back button and Escape", () => {
    const onCancel = vi.fn();
    render(<EditLayer editing={{ id: "n1", body: "b" }} onCancel={onCancel} onSave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel edit" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("never enables Save for an empty body", () => {
    render(<EditLayer editing={{ id: "n1", body: "b" }} onCancel={vi.fn()} onSave={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Note body"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});
