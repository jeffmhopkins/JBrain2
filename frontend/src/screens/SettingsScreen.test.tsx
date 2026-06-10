import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { isLocationCaptureEnabled } from "../location";
import { SettingsScreen } from "./SettingsScreen";

function setup() {
  render(<SettingsScreen deviceLabel="Test device" onLogout={vi.fn()} />);
}

describe("SettingsScreen capture location", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("defaults the toggle to on", () => {
    setup();
    const group = screen.getByLabelText("Capture location");
    const on = group.querySelector('[aria-pressed="true"]');
    expect(on).toHaveTextContent("On");
  });

  it("persists off across remounts via localStorage", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Off" }));
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("off");
    expect(isLocationCaptureEnabled()).toBe(false);
  });

  it("persists turning it back on", () => {
    localStorage.setItem("jbrain.captureLocation", "off");
    setup();
    fireEvent.click(screen.getByRole("button", { name: "On" }));
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("on");
    expect(isLocationCaptureEnabled()).toBe(true);
  });
});
