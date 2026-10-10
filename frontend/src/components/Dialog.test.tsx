import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Dialog } from "./Dialog";

function renderDialog() {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <Dialog title="Stop the server?" confirmLabel="Stop" onConfirm={onConfirm} onCancel={onCancel}>
      2 players will be disconnected.
    </Dialog>,
  );
  return { onConfirm, onCancel };
}

describe("Dialog", () => {
  it("names itself by its title, describes the consequence, and focuses Cancel", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Stop the server?" });
    expect(dialog).toHaveAccessibleDescription("2 players will be disconnected.");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("confirms only from the act button", () => {
    const { onConfirm, onCancel } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("cancels on Escape and on a scrim tap", () => {
    const { onCancel, onConfirm } = renderDialog();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    const scrim = screen.getByRole("dialog").parentElement as HTMLElement;
    fireEvent.click(scrim);
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("owns Escape: a layer beneath listening on window doesn't also close", () => {
    const beneath = vi.fn();
    window.addEventListener("keydown", beneath);
    try {
      const { onCancel } = renderDialog();
      fireEvent.keyDown(window, { key: "Escape" });
      expect(onCancel).toHaveBeenCalledOnce();
      expect(beneath).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", beneath);
    }
  });

  it("keeps Tab inside its two buttons", () => {
    renderDialog();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const stop = screen.getByRole("button", { name: "Stop" });
    stop.focus();
    fireEvent.keyDown(stop, { key: "Tab" });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(cancel, { key: "Tab", shiftKey: true });
    expect(stop).toHaveFocus();
  });
});
