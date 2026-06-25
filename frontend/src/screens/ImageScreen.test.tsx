import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GeneratedImageOut } from "../api/client";
import { ImageScreen } from "./ImageScreen";

function img(over: Partial<GeneratedImageOut>): GeneratedImageOut {
  return {
    id: "g1",
    kind: "generate",
    prompt: "a teapot",
    width: 1024,
    height: 1024,
    model: "qwen-image",
    seed: 418207733,
    created_at: new Date().toISOString(),
    ...over,
  };
}

const noop = () => {};

// Drive the screen against a stubbed transport so the gallery list and the
// generate/edit renders are deterministic. The render returns a fixed row so the
// new tile + its meta are assertable.
function stubFetch(initial: GeneratedImageOut[], rendered?: GeneratedImageOut) {
  const m = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input instanceof Request ? input.url : input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (path.endsWith("/api/images/generated") && method === "GET") {
      return new Response(JSON.stringify(initial), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (path.endsWith("/api/images/generate") || path.endsWith("/api/images/edit")) {
      const out = rendered ?? img({ id: "new-render", seed: 555111222 });
      return new Response(JSON.stringify(out), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`Unexpected fetch: ${method} ${path}`);
  });
  vi.stubGlobal("fetch", m);
}

describe("ImageScreen", () => {
  beforeEach(() => {
    // Reduced motion → the render skips the phase timers and reveals as soon as
    // the awaited request resolves, keeping the tests deterministic.
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("switches segments, swapping the Generate and Edit panels", async () => {
    stubFetch([img({})]);
    render(<ImageScreen onClose={noop} />);
    // Generate is the default panel: its prompt label is present.
    expect(screen.getByLabelText("prompt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Edit" }));
    // The edit panel leads with the source dropzone + the edit-instruction field.
    expect(screen.getByLabelText("edit instruction")).toBeInTheDocument();
    expect(screen.getByText("upload an image, or pick from the gallery")).toBeInTheDocument();
  });

  it("locks the steps control when speed is not quality", async () => {
    stubFetch([img({})]);
    render(<ImageScreen onClose={noop} />);
    const steps = screen.getByRole("slider", { name: "steps" });
    // Quality is the default — the slider is live.
    expect(steps).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "fast" }));
    // Off the quality path the slider locks and a fixed-step hint appears.
    expect(screen.getByRole("slider", { name: "steps" })).toBeDisabled();
    expect(screen.getByText(/fixed 4 steps on the fast path/)).toBeInTheDocument();
  });

  it("a generate adds a tile to the gallery and shows the result meta", async () => {
    stubFetch([img({ id: "seed-1" })], img({ id: "new-render", seed: 555111222 }));
    render(<ImageScreen onClose={noop} />);
    // The gallery badge starts at the one seeded render.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Open gallery \(1 renders\)/ }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    // The result meta surfaces dimensions · model · seed once revealed.
    await waitFor(() => expect(screen.getByText(/seed 555111222/)).toBeInTheDocument());
    // The new render bumps the live count to 2.
    expect(screen.getByRole("button", { name: /Open gallery \(2 renders\)/ })).toBeInTheDocument();
  });

  it("use as edit source populates the edit panel from a result", async () => {
    stubFetch([img({ id: "seed-1" })], img({ id: "new-render", seed: 555111222 }));
    render(<ImageScreen onClose={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => screen.getByText("use as edit source"));
    fireEvent.click(screen.getByText("use as edit source"));
    // It flips to Edit with the render set as the source (no longer the dropzone).
    expect(screen.getByLabelText("edit instruction")).toBeInTheDocument();
    expect(screen.getByText("from gallery")).toBeInTheDocument();
    expect(screen.queryByText("upload an image, or pick from the gallery")).not.toBeInTheDocument();
  });

  it("shows the empty-gallery state when there are no renders", async () => {
    stubFetch([]);
    render(<ImageScreen onClose={noop} />);
    fireEvent.click(screen.getByRole("button", { name: /Open gallery/ }));
    expect(
      screen.getByText(/nothing rendered yet — generate an image and it lands here\./),
    ).toBeInTheDocument();
  });

  it("opens a gallery tile in the lightbox with its meta", async () => {
    stubFetch([img({ id: "seed-1", model: "dreamshaper", seed: 12009654 })]);
    render(<ImageScreen onClose={noop} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Open gallery \(1 renders\)/ }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Open gallery \(1 renders\)/ }));
    fireEvent.click(screen.getByRole("button", { name: /generate render 1024×1024/ }));
    // The lightbox shows the kind badge + meta and the use-as-source action.
    await waitFor(() => expect(screen.getByText(/seed 12009654/)).toBeInTheDocument());
  });
});
