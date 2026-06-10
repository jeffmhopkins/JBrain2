import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MoveDomainSheet } from "./MoveDomainSheet";

function setup(
  target: { id: string; domain: string; destination: string | null } = {
    id: "n1",
    domain: "general",
    destination: null,
  },
) {
  const onMove = vi.fn();
  const onClose = vi.fn();
  render(<MoveDomainSheet target={target} onMove={onMove} onClose={onClose} />);
  return { onMove, onClose };
}

describe("MoveDomainSheet", () => {
  it("lists the four domains with the current one selected", () => {
    setup();
    expect(screen.getByRole("button", { name: "General" })).toHaveAttribute("aria-pressed", "true");
    for (const name of ["Medical", "Financial", "Location"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("moving to health with a destination sends both fields", () => {
    const { onMove } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Medical" }));
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "Labs" } });
    fireEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(onMove).toHaveBeenCalledWith("health", "Labs");
  });

  it("destination select only appears for health/finance", () => {
    setup();
    expect(screen.queryByLabelText("Destination")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Financial" }));
    expect(screen.getByLabelText("Destination")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Location" }));
    expect(screen.queryByLabelText("Destination")).not.toBeInTheDocument();
  });

  it("moving to a destination-less domain sends an explicit null destination", () => {
    const { onMove } = setup({ id: "n1", domain: "finance", destination: "Receipts" });
    fireEvent.click(screen.getByRole("button", { name: "General" }));
    fireEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(onMove).toHaveBeenCalledWith("general", null);
  });

  it("keeps the existing destination when it survives the domain change", () => {
    const { onMove } = setup({ id: "n1", domain: "health", destination: "Labs" });
    fireEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(onMove).toHaveBeenCalledWith("health", "Labs");
  });

  it("Escape closes the sheet", () => {
    const { onClose } = setup();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
