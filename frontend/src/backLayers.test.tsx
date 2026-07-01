import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { closeTopModalLayer, useBackLayer, useModalLayerCount } from "./backLayers";

function Layer({ onClose, label }: { onClose: () => void; label: string }) {
  useBackLayer(onClose);
  return <div>{label}</div>;
}

// A parent that mounts N self-removing layers plus a live count and a "back"
// button, mirroring how App reads the stack: closeTopModalLayer() pops the top,
// whose onClose unmounts it (dropping its registration).
function Harness() {
  const [ids, setIds] = useState([1, 2, 3]);
  return (
    <div>
      <div>count:{useModalLayerCount()}</div>
      {ids.map((id) => (
        <Layer
          key={id}
          label={`L${id}`}
          onClose={() => setIds((prev) => prev.filter((x) => x !== id))}
        />
      ))}
      <button type="button" onClick={() => closeTopModalLayer()}>
        back
      </button>
    </div>
  );
}

describe("backLayers", () => {
  it("closeTopModalLayer is a no-op returning false when nothing is registered", () => {
    expect(closeTopModalLayer()).toBe(false);
  });

  it("counts mounted layers and pops the topmost first (LIFO)", () => {
    render(<Harness />);
    expect(screen.getByText("count:3")).toBeTruthy();

    // Back closes the last-registered layer only.
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByText("count:2")).toBeTruthy();
    expect(screen.queryByText("L3")).toBeNull();
    expect(screen.getByText("L2")).toBeTruthy();

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByText("count:1")).toBeTruthy();
    expect(screen.queryByText("L2")).toBeNull();
    expect(screen.getByText("L1")).toBeTruthy();

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByText("count:0")).toBeTruthy();
  });

  it("drops registrations on unmount so the count returns to zero", () => {
    const { unmount } = render(<Harness />);
    expect(screen.getByText("count:3")).toBeTruthy();
    unmount();
    // Nothing registered once the tree is gone.
    expect(closeTopModalLayer()).toBe(false);
  });
});
