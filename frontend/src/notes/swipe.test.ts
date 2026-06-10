import { describe, expect, it } from "vitest";
import { RAIL_WIDTH, beginDrag, endDrag, moveDrag } from "./swipe";

describe("swipe rail drag", () => {
  it("locks horizontal once dx clearly dominates and follows the finger", () => {
    let drag = beginDrag(200, 100, false);
    drag = moveDrag(drag, 180, 102); // dx -20, dy 2 → horizontal
    expect(drag.axis).toBe("h");
    expect(drag.offset).toBe(-20);
    drag = moveDrag(drag, 120, 104);
    expect(drag.offset).toBe(-80);
  });

  it("lets vertical scrolling win, leaving the bubble unmoved", () => {
    let drag = beginDrag(200, 100, false);
    drag = moveDrag(drag, 198, 130); // dy dominates
    expect(drag.axis).toBe("v");
    expect(drag.offset).toBe(0);
  });

  it("ties go vertical so the rail never hijacks a diagonal scroll", () => {
    let drag = beginDrag(200, 100, false);
    drag = moveDrag(drag, 188, 112); // |dx| === |dy|
    expect(drag.axis).toBe("v");
  });

  it("keeps the first locked axis for the rest of the gesture", () => {
    let drag = beginDrag(200, 100, false);
    drag = moveDrag(drag, 170, 100); // locks h
    drag = moveDrag(drag, 170, 200); // later vertical movement is ignored
    expect(drag.axis).toBe("h");
    expect(drag.offset).toBe(-30);
  });

  it("clamps the offset between fully open and closed", () => {
    let drag = beginDrag(400, 100, false);
    drag = moveDrag(drag, 0, 100);
    expect(drag.offset).toBe(-RAIL_WIDTH);
    drag = moveDrag(drag, 800, 100);
    expect(drag.offset).toBe(0);
  });

  it("snaps open past half the rail width, closed before it", () => {
    let drag = beginDrag(300, 100, false);
    drag = moveDrag(drag, 300 - RAIL_WIDTH / 2 - 1, 100);
    expect(endDrag(drag)).toBe(true);

    drag = beginDrag(300, 100, false);
    drag = moveDrag(drag, 300 - RAIL_WIDTH / 2 + 10, 100);
    expect(endDrag(drag)).toBe(false);
  });

  it("starts from the open position and snaps shut on a right drag", () => {
    let drag = beginDrag(100, 100, true);
    expect(drag.offset).toBe(-RAIL_WIDTH);
    drag = moveDrag(drag, 100 + RAIL_WIDTH / 2 + 1, 100);
    expect(endDrag(drag)).toBe(false);
  });

  it("a tap (no horizontal lock) keeps the rail where it was", () => {
    expect(endDrag(beginDrag(100, 100, true))).toBe(true);
    expect(endDrag(beginDrag(100, 100, false))).toBe(false);
  });
});
