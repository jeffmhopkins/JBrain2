import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Sheet } from "./Sheet";

// Swipe-to-dismiss must engage ONLY from the top grab area (handle + title), never from the
// scrollable body — otherwise scrolling down through long content (a plan's step reports) past
// the threshold dismisses the sheet mid-read.
describe("Sheet swipe-to-dismiss", () => {
  function swipeDown(el: Element): void {
    fireEvent.touchStart(el, { touches: [{ clientY: 100 }] });
    fireEvent.touchMove(el, { touches: [{ clientY: 300 }] }); // 200px > SWIPE_DOWN_PX (96)
  }

  it("does NOT dismiss on a swipe that starts in the body", () => {
    const onClose = vi.fn();
    render(
      <Sheet title="Plan" onClose={onClose}>
        <div data-testid="body">a long report the owner scrolls through</div>
      </Sheet>,
    );
    swipeDown(screen.getByTestId("body"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("dismisses on a swipe that starts on the grab handle", () => {
    const onClose = vi.fn();
    render(
      <Sheet title="Plan" onClose={onClose}>
        <div>body</div>
      </Sheet>,
    );
    swipeDown(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("dismisses on a swipe that starts on the title", () => {
    const onClose = vi.fn();
    render(
      <Sheet title="Plan" onClose={onClose}>
        <div>body</div>
      </Sheet>,
    );
    swipeDown(screen.getByRole("heading", { name: "Plan" }));
    expect(onClose).toHaveBeenCalled();
  });
});

// Focusing the panel is an ON-OPEN behaviour. It used to sit in an effect keyed on
// `onClose`, and every caller passes an inline arrow — a new identity on each render —
// so it re-ran on every repaint and pulled focus back out of whatever the owner was
// typing into. On a screen that repaints on a timer (the tuner repaints every second
// off the radio poll) that dismissed the phone's keypad about a second after it
// opened, which reads exactly like the field closing by itself.
describe("Sheet focus", () => {
  it("does not steal focus back on a re-render with a new onClose", () => {
    const { rerender } = render(
      <Sheet title="Tuned station" onClose={() => {}}>
        <input aria-label="Frequency in MHz" />
      </Sheet>,
    );
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    field.focus();
    expect(document.activeElement).toBe(field);

    // Exactly what a parent screen does once a second: same tree, fresh closure.
    rerender(
      <Sheet title="Tuned station" onClose={() => {}}>
        <input aria-label="Frequency in MHz" />
      </Sheet>,
    );

    expect(document.activeElement).toBe(field);
  });
});
