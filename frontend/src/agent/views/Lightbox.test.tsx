import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Lightbox } from "./Lightbox";

// jsdom's default viewport is 1024×768, so the flex-centred image's centre sits at
// (512, 384) — the anchor the zoom math is measured against.
function transform(): { x: number; y: number; scale: number } {
  const img = document.querySelector(".fb-lightbox-img") as HTMLElement;
  const m = img.style.transform.match(/translate\((-?[\d.]+)px, (-?[\d.]+)px\) scale\(([\d.]+)\)/);
  if (!m) throw new Error(`unexpected transform: ${img.style.transform}`);
  return { x: Number(m[1]), y: Number(m[2]), scale: Number(m[3]) };
}

describe("Lightbox", () => {
  it("zooms in place when the cursor is at the image centre", () => {
    render(<Lightbox src="/img.png" alt="x" onClose={() => {}} />);
    const overlay = document.querySelector(".fb-lightbox") as HTMLElement;
    fireEvent.wheel(overlay, { deltaY: -100, clientX: 512, clientY: 384 });
    const t = transform();
    expect(t.scale).toBeCloseTo(1.15, 5);
    expect(t.x).toBeCloseTo(0, 5);
    expect(t.y).toBeCloseTo(0, 5);
  });

  it("anchors an off-centre wheel zoom so that point stays put", () => {
    render(<Lightbox src="/img.png" alt="x" onClose={() => {}} />);
    const overlay = document.querySelector(".fb-lightbox") as HTMLElement;
    // 200px right + 100px below centre → the translate shifts by delta*(1 - k),
    // k = 1.15: x = 200·(1−1.15) = −30, y = 100·(1−1.15) = −15. The point under the
    // cursor stays fixed (the bug was anchoring off the viewport origin instead).
    fireEvent.wheel(overlay, { deltaY: -100, clientX: 712, clientY: 484 });
    const t = transform();
    expect(t.scale).toBeCloseTo(1.15, 5);
    expect(t.x).toBeCloseTo(-30, 5);
    expect(t.y).toBeCloseTo(-15, 5);
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(<Lightbox src="/img.png" alt="x" onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
