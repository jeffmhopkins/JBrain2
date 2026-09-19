import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { PetState } from "../api/client";
import { drawScene } from "../pet/draw";
import { clearCardPx } from "../pet/scale";
import { speak } from "./speech";

// jsdom has no speechSynthesis; the pet's voice is mocked so the reply can be asserted.
vi.mock("./speech", () => ({ speak: vi.fn(), ttsAvailable: () => true }));
import { type PetFaceDeps, PetFaceScreen } from "./PetFaceScreen";

// Canvas is jsdom-untestable, so the renderer is mocked and `getContext` stubbed — the
// petScene/leafletMap convention. What is under test here is the API contract and the
// interaction model; the drawing itself is covered by the pure modules in `src/pet/`.
vi.mock("../pet/draw", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pet/draw")>()),
  drawScene: vi.fn(),
}));

const realGetContext = HTMLCanvasElement.prototype.getContext;
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ({})) as unknown as typeof realGetContext;
});
afterAll(() => {
  HTMLCanvasElement.prototype.getContext = realGetContext;
});

function petState(over: Partial<PetState> = {}): PetState {
  return {
    name: "Blink",
    domain: "general",
    mood: "playful",
    emotion: "curious",
    speech: null,
    asleep: false,
    pos_x: 0,
    pos_z: 0,
    target_x: 0,
    target_z: 0,
    facing: 0,
    action: "idle",
    color: null,
    script: [],
    carrying: null,
    lights_on: true,
    objects: {},
    ...over,
  };
}

/** A stream with no frames. Written as an empty loop rather than an empty body so it still
 *  contains a `yield` (biome's useYield) while producing nothing — which is the real case on a
 *  box where a proxy eats SSE and the screen has only the snapshot to work from. */
async function* noFrames(): AsyncGenerator<PetState> {
  for (const s of [] as PetState[]) yield s;
}

async function* failingStream(): AsyncGenerator<PetState> {
  for (const s of [] as PetState[]) yield s;
  throw new Error("offline");
}

function makeDeps(first: PetState = petState()): {
  deps: PetFaceDeps;
  sendPetCommand: ReturnType<typeof vi.fn>;
  getPet: ReturnType<typeof vi.fn>;
} {
  const sendPetCommand = vi.fn(async () => first);
  const getPet = vi.fn(async () => first);
  const deps: PetFaceDeps = {
    getPet,
    sendPetCommand,
    petStream: () => noFrames(),
  };
  return { deps, sendPetCommand, getPet };
}

describe("PetFaceScreen", () => {
  it("renders the panel at the hardware's native size", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const canvas = screen.getByLabelText("Pet face preview panel") as HTMLCanvasElement;
    expect(canvas.width).toBe(368);
    expect(canvas.height).toBe(448);
  });

  it("shows the server's state, including the emotion the wall ignores", async () => {
    const { deps } = makeDeps(petState({ emotion: "sleepy", color: "red" }));
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    await waitFor(() => expect(screen.getByText(/emotion sleepy/)).toBeTruthy());
    expect(screen.getByText(/colour red/)).toBeTruthy();
  });

  // The settled interaction model: one whole-screen target, one gesture, no thresholds. A
  // press and release is a poke, and it must reach the server so the wall stays in sync.
  it("sends a poke on press-and-release of the panel", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const canvas = screen.getByLabelText("Pet face preview panel");
    fireEvent.pointerDown(canvas, { clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledWith({ action: "wiggle" }));
  });

  it("does not fire a second time on a stray pointerup", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const canvas = screen.getByLabelText("Pet face preview panel");
    fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledTimes(1));
  });

  it("routes a spoken phrase through the server, not the client", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.change(screen.getByLabelText("Say to the pet"), { target: { value: "turn red" } });
    fireEvent.click(screen.getByText("Say"));
    await waitFor(() =>
      expect(sendPetCommand).toHaveBeenCalledWith({ action: "say", text: "turn red" }),
    );
  });

  it("sends a quick action as a real pet command", async () => {
    const { deps, sendPetCommand } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.click(screen.getByText("peekaboo"));
    await waitFor(() => expect(sendPetCommand).toHaveBeenCalledWith({ action: "hide" }));
  });

  it("toggles a face override off again when pressed twice", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    const silly = screen.getByText("silly");
    fireEvent.click(silly);
    expect(silly.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(silly);
    expect(silly.getAttribute("aria-pressed")).toBe("false");
  });

  // The screen calibration persists by design, so order must not decide what a test sees.
  beforeEach(() => clearCardPx());

  // The panel is 29.02 x 35.33 mm. Showing that believably is the preview's whole job, and
  // CSS cannot do it: `1in` is pinned to 96px whatever the screen is. These pin the three
  // modes to what they claim, because a size that is quietly wrong is worse than no size.
  function panel(): HTMLCanvasElement {
    return screen.getByLabelText("Pet face preview panel") as HTMLCanvasElement;
  }

  it("asks to be calibrated rather than guessing at actual size", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.click(screen.getByText("Actual size"));
    expect(screen.getByText(/not calibrated/)).toBeTruthy();
    // And it holds the working size rather than falling back to the CSS-mm lie.
    expect(panel().style.width).toBe("368px");
  });

  it("sizes the panel to the measured millimetres once calibrated", () => {
    const { deps } = makeDeps();
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    // A dense phone: a bank card (85.6 mm) measured at 520 CSS px is ~154 CSS ppi.
    fireEvent.change(screen.getByLabelText(/Card width in pixels/), {
      target: { value: "520" },
    });
    fireEvent.click(screen.getByText("Actual size"));
    const want = (29.02 * 520) / 85.6;
    expect(Number.parseFloat(panel().style.width)).toBeCloseTo(want, 3);
    expect(
      screen.getByText(/actual size — 29\.0 × 35\.3 mm, your screen measured at 154/),
    ).toBeTruthy();
    // Well under what the shipped `width: 29.02mm` rule drew, which is the bug.
    expect(want).toBeGreaterThan((29.02 * 96) / 25.4);
  });

  it("remembers the calibration, so it is measured once and not every visit", () => {
    const { deps } = makeDeps();
    const first = render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.change(screen.getByLabelText(/Card width in pixels/), {
      target: { value: "460" },
    });
    first.unmount();

    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    fireEvent.click(screen.getByText("Actual size"));
    expect(Number.parseFloat(panel().style.width)).toBeCloseTo((29.02 * 460) / 85.6, 3);
  });

  it("1:1 pixels means DEVICE pixels — the shipped mode was 1:1 CSS pixels", () => {
    const { deps } = makeDeps();
    const real = window.devicePixelRatio;
    Object.defineProperty(window, "devicePixelRatio", { value: 3, configurable: true });
    try {
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      fireEvent.click(screen.getByText("1:1 pixels"));
      expect(Number.parseFloat(panel().style.width)).toBeCloseTo(368 / 3, 3);
    } finally {
      Object.defineProperty(window, "devicePixelRatio", { value: real, configurable: true });
    }
  });

  it("survives the pet endpoint being down", async () => {
    const deps: PetFaceDeps = {
      getPet: vi.fn(async () => {
        throw new Error("offline");
      }),
      sendPetCommand: vi.fn(async () => petState()),
      petStream: () => failingStream(),
    };
    render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
    await waitFor(() => expect(screen.getByText(/connecting/)).toBeTruthy());
  });

  // A browser can refuse a 2D context (too many live ones, a lost GPU). The frame loop must
  // survive it rather than take the screen down, so the guard is asserted, not assumed.
  it("keeps working when the canvas refuses a 2D context", async () => {
    const spy = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => {
      throw new Error("no context");
    });
    try {
      const { deps, sendPetCommand } = makeDeps();
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      const canvas = screen.getByLabelText("Pet face preview panel");
      fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerUp(canvas, { pointerId: 1 });
      await waitFor(() => expect(sendPetCommand).toHaveBeenCalled());
    } finally {
      spy.mockRestore();
    }
  });

  // REGRESSION. The effects poll used to be created inside the async block, after the first
  // `getPet()` awaited — so unmounting before that resolved ran the cleanup while the timer
  // handle was still undefined, and the continuation then started an interval nothing could
  // clear. It leaked a 1 Hz request for the life of the page, and it hung CI: eleven orphaned
  // intervals kept the test runner's event loop alive for 50 minutes.
  it("leaves no polling interval behind when unmounted before the first fetch resolves", async () => {
    vi.useFakeTimers();
    try {
      let settle: (s: PetState) => void = () => {};
      const getPet = vi.fn(
        () =>
          new Promise<PetState>((r) => {
            settle = r;
          }),
      );
      const { unmount } = render(
        <PetFaceScreen
          onClose={vi.fn()}
          deps={{
            getPet,
            sendPetCommand: vi.fn(async () => petState()),
            petStream: () => noFrames(),
          }}
        />,
      );
      unmount(); // back out before the snapshot lands
      settle(petState()); // ...then let it resolve
      await Promise.resolve();
      const afterUnmount = getPet.mock.calls.length;
      vi.advanceTimersByTime(10_000);
      expect(getPet.mock.calls.length).toBe(afterUnmount);
    } finally {
      vi.useRealTimers();
    }
  });

  // THE BUG THIS SURFACE SHIPPED WITH. A command reached the box and the box answered with a
  // script, and the panel played none of it — it read only `script[0].emotion` for the face.
  // Typing "dance" did nothing visible, which is what "commands don't work" looked like.
  //
  // The observable is what reaches the RENDERER, not the DOM: the caption and the pose live on
  // the canvas, so asserting on `drawScene`'s scene is asserting on what the panel actually
  // draws. (An earlier version of this test looked for the text "dance" in the document and
  // passed for the wrong reason — it was finding the quick button of the same name.)
  describe("plays the script the box returns", () => {
    const danceScript: PetState["script"] = [
      { action: "dance", duration_ms: 5000, emotion: "excited" },
      { action: "sit", duration_ms: 5000 },
    ];
    const captions = () =>
      vi
        .mocked(drawScene)
        .mock.calls.map(([, scene]) => scene.caption)
        .filter(Boolean);

    beforeEach(() => vi.mocked(drawScene).mockClear());

    it("performs a script that arrives in a command response", async () => {
      const sendPetCommand = vi.fn(async () => petState({ script: danceScript }));
      const deps: PetFaceDeps = {
        getPet: vi.fn(async () => petState()),
        sendPetCommand,
        petStream: () => noFrames(),
      };
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      fireEvent.change(screen.getByLabelText("Say to the pet"), { target: { value: "dance" } });
      fireEvent.click(screen.getByText("Say"));
      await waitFor(() => expect(captions()).toContain("dance"));
    });

    it("performs a script that arrives on the stream, not only one it asked for", async () => {
      // Another surface — the phone Control screen, the wall, an automation — commanded the pet.
      async function* oneFrame(): AsyncGenerator<PetState> {
        yield petState({ script: danceScript });
      }
      const deps: PetFaceDeps = {
        getPet: vi.fn(async () => petState()),
        sendPetCommand: vi.fn(async () => petState()),
        petStream: () => oneFrame(),
      };
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      await waitFor(() => expect(captions()).toContain("dance"));
    });

    // A snapshot carries whatever script was last run. Performing it on open would make the pet
    // do something nobody just asked for, every single time the screen is opened.
    it("does not perform the snapshot's existing script on open", async () => {
      const deps: PetFaceDeps = {
        getPet: vi.fn(async () => petState({ script: danceScript })),
        sendPetCommand: vi.fn(async () => petState()),
        petStream: () => noFrames(),
      };
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      await waitFor(() => expect(screen.getByText(/2 step/)).toBeTruthy());
      await waitFor(() => expect(vi.mocked(drawScene).mock.calls.length).toBeGreaterThan(1));
      expect(captions()).not.toContain("dance");
    });
  });

  // "tell me a joke" is not a keyword, so the box takes the LLM path and answers with SPEECH
  // plus a small emote. The joke is the reply — and the audience cannot read, so a reply that
  // is neither spoken nor shown has not been delivered. This screen said nothing at all.
  describe("the pet's reply", () => {
    beforeEach(() => vi.mocked(speak).mockClear());

    it("speaks what the box says", async () => {
      const deps: PetFaceDeps = {
        getPet: vi.fn(async () => petState()),
        sendPetCommand: vi.fn(async () =>
          petState({ speech: "Why did the robot cross the road?" }),
        ),
        petStream: () => noFrames(),
      };
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      fireEvent.change(screen.getByLabelText("Say to the pet"), {
        target: { value: "tell me a joke" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Say" }));
      await waitFor(() => expect(speak).toHaveBeenCalledWith("Why did the robot cross the road?"));
    });

    // A reply rides every subsequent stream frame. Re-speaking it each time would make the pet
    // repeat itself indefinitely.
    it("says a given line once, however many frames carry it", async () => {
      async function* twice(): AsyncGenerator<PetState> {
        yield petState({ speech: "Beep boop!" });
        yield petState({ speech: "Beep boop!" });
      }
      const deps: PetFaceDeps = {
        getPet: vi.fn(async () => petState()),
        sendPetCommand: vi.fn(async () => petState()),
        petStream: () => twice(),
      };
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      await waitFor(() => expect(speak).toHaveBeenCalledTimes(1));
    });

    // The snapshot carries the last thing the pet ever said. Speaking it on open would have the
    // pet greet you with a joke from last Tuesday every time the screen is opened.
    it("does not speak the snapshot's old reply on open", async () => {
      const deps: PetFaceDeps = {
        getPet: vi.fn(async () => petState({ speech: "an old line" })),
        sendPetCommand: vi.fn(async () => petState()),
        petStream: () => noFrames(),
      };
      render(<PetFaceScreen onClose={vi.fn()} deps={deps} />);
      await waitFor(() => expect(screen.getByText(/emotion/)).toBeTruthy());
      expect(speak).not.toHaveBeenCalled();
    });
  });

  it("closes", () => {
    const { deps } = makeDeps();
    const onClose = vi.fn();
    render(<PetFaceScreen onClose={onClose} deps={deps} />);
    fireEvent.click(screen.getByText("Back"));
    expect(onClose).toHaveBeenCalled();
  });
});
