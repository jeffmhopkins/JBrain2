import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type CastNotice, type CastTv, api } from "../api/client";
import { CastScreen, forgetCastDraft } from "./CastScreen";

const TVS: CastTv[] = [
  {
    id: "a",
    name: "Big TV",
    model: null,
    state: "idle",
    why: "Ambient",
    castable: true,
    reading: { app_id: null, display_name: null, connected: true },
  },
  {
    id: "b",
    name: "Parents",
    model: null,
    state: "standby",
    why: "off",
    castable: true,
    reading: { app_id: null, display_name: null, connected: true },
  },
  {
    id: "c",
    name: "Twins room",
    model: null,
    state: "busy",
    why: "Netflix · playing",
    castable: false,
    reading: { app_id: "Netflix", display_name: "Netflix", connected: true },
  },
];

function sent(over: Partial<CastNotice> = {}): CastNotice {
  return {
    id: "n1",
    sender: "owner",
    title: "Dinner's ready",
    body: "",
    seconds: 30,
    created_at: new Date().toISOString(),
    stopped_at: null,
    tvs: [
      { tv_id: "a", name: "Big TV", outcome: "shown", why: "Ambient" },
      { tv_id: "b", name: "Parents", outcome: "woke", why: "was off" },
    ],
    ...over,
  };
}

/** jsdom has no 2D canvas: a context that records the words it is asked to draw and where,
 *  measures them at half the font size a letter, and an export that reads as a JPEG. */
const drawn: string[] = [];
const drawnAt: number[][] = [];
const turned: number[] = [];
/** The font size each line was drawn at. */
const measuredAt: number[] = [];
function fakeCanvas() {
  let px = 10;
  const ctx = new Proxy(
    {
      measureText: (t: string) => ({ width: t.length * px * 0.5 }),
      rotate: (a: number) => turned.push(a),
      fillText: (t: string, x: number, y: number) => {
        drawn.push(t);
        measuredAt.push(px);
        drawnAt.push([x, y]);
      },
    } as Record<string, unknown>,
    {
      get: (o, k) => (k in o ? o[k as string] : () => {}),
      set: (_o, k, v) => {
        if (k === "font") px = Number(/(\d+)px/.exec(String(v))?.[1] ?? px);
        return true;
      },
    },
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
    "data:image/jpeg;base64,RlJBTUU=",
  );
}

beforeEach(() => {
  forgetCastDraft();
  drawn.length = 0;
  drawnAt.length = 0;
  turned.length = 0;
  measuredAt.length = 0;
  vi.spyOn(api, "castTvs").mockResolvedValue({ tvs: TVS });
  fakeCanvas();
});
const nativeDecode = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "decode");
afterEach(() => {
  if (nativeDecode) Object.defineProperty(HTMLImageElement.prototype, "decode", nativeDecode);
  else Reflect.deleteProperty(HTMLImageElement.prototype, "decode");
  vi.restoreAllMocks();
});

/** Open Send and put words on the screen the way the owner does: Add text, type, add. */
async function addText(words: string) {
  if (!screen.queryByRole("button", { name: "Add text" })) {
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
  }
  fireEvent.click(screen.getByRole("button", { name: "Add text" }));
  fireEvent.change(screen.getByLabelText(/^Words/), { target: { value: words } });
  fireEvent.click(screen.getByRole("button", { name: "Add to screen" }));
}

describe("Cast", () => {
  it("keeps what was composed when the card is left and reopened", async () => {
    const first = render(<CastScreen />);
    await addText("Dinner");
    expect(screen.queryByText("Add text or a picture to start")).not.toBeInTheDocument();
    first.unmount();
    render(<CastScreen />);
    // Back on Send, with the words still on the canvas.
    expect(screen.getByRole("button", { name: "Add text" })).toBeInTheDocument();
    expect(screen.queryByText("Add text or a picture to start")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^Cast to/ })).toBeEnabled();
  });

  it("swallows touch so a downward drag never closes the card", async () => {
    const onParentTouchStart = vi.fn();
    const onParentTouchMove = vi.fn();
    render(
      // Stands in for App's swipe-armed subscreen wrapper.
      <div onTouchStart={onParentTouchStart} onTouchMove={onParentTouchMove}>
        <CastScreen />
      </div>,
    );
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    const canvas = screen.getByText("Add text or a picture to start");
    fireEvent.touchStart(canvas as HTMLElement, { touches: [{ clientX: 0, clientY: 0 }] });
    fireEvent.touchMove(canvas as HTMLElement, { touches: [{ clientX: 0, clientY: 200 }] });
    expect(onParentTouchStart).not.toHaveBeenCalled();
    expect(onParentTouchMove).not.toHaveBeenCalled();
  });

  it("lists each TV with what it is doing, in words", async () => {
    render(<CastScreen />);
    expect(await screen.findByText("3 found on the network")).toBeInTheDocument();
    expect(screen.getByText("Big TV")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Off · will turn on")).toBeInTheDocument();
    expect(screen.getByText("In use")).toBeInTheDocument();
    expect(screen.getByText("Netflix · playing")).toBeInTheDocument();
  });

  it("says when the box can't reach its TV finder", async () => {
    vi.spyOn(api, "castTvs").mockRejectedValue(new ApiError(503, "no sidecar"));
    render(<CastScreen />);
    expect(
      await screen.findByText("The box can't reach its TV finder right now."),
    ).toBeInTheDocument();
  });

  it("sends to the TVs that would take it, then shows how each took it", async () => {
    const send = vi.spyOn(api, "castSend").mockResolvedValue({ sent: sent() });
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));

    // The ready and off TVs are picked to start with; the busy one is not. The cast button
    // counts them and opens the list, the way a video app picks a TV to cast to.
    const picker = screen.getByRole("button", { name: "TVs: Big TV, Parents" });
    expect(picker).toHaveTextContent("2");
    fireEvent.click(picker);
    const picks = within(screen.getByRole("group", { name: "TVs" }));
    expect(picks.getByRole("button", { name: /Big TV/ })).toHaveAttribute("aria-pressed", "true");
    expect(picks.getByRole("button", { name: /Twins room/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    const cast = screen.getByRole("button", { name: /Cast to 2 TVs/ });
    expect(cast).toBeDisabled();

    await addText("Dinner's ready");
    // The words are drawn on the phone, as the TV will get them, and held to be placed.
    expect(drawn).toContain("Dinner's ready");
    expect(screen.getByRole("group", { name: "Selected text" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Unit"), { target: { value: "min" } });
    fireEvent.change(screen.getByLabelText("On screen for"), { target: { value: "1" } });

    // Picking a TV that is in use says so before sending.
    fireEvent.click(screen.getByRole("button", { name: "TVs: Big TV, Parents" }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "TVs" })).getByRole("button", {
        name: /Twins room/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.getByRole("button", { name: "TVs: Big TV, Parents + 1 more" })).toBeVisible();
    expect(
      screen.getByText("Twins room is netflix · playing and will be skipped."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 of 3 TVs/ }));

    await screen.findByText("Cast to 2 of 2");
    expect(send).toHaveBeenCalledWith({
      tv_ids: ["a", "b", "c"],
      title: "Dinner's ready",
      frame_b64: "RlJBTUU=",
      seconds: 60,
    });
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("On screen")).toBeInTheDocument();
    expect(sheet.getByText("Turned on")).toBeInTheDocument();
  });

  it("says a refusal in words", async () => {
    vi.spyOn(api, "castSend").mockResolvedValue({
      refused: { status: 415, reason: "bad_picture" },
    });
    render(<CastScreen />);
    await addText("Look");
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 TVs/ }));
    expect(await screen.findByText("That picture couldn't be read.")).toBeInTheDocument();
  });

  it("says when the box can't reach the TVs to send", async () => {
    vi.spyOn(api, "castSend").mockRejectedValue(new ApiError(503, "cast sidecar unreachable"));
    render(<CastScreen />);
    await addText("Look");
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 TVs/ }));
    expect(await screen.findByText("The box can't reach the TVs right now.")).toBeInTheDocument();
  });

  it("needs something on the screen before it can cast, and keeps words to what the box takes", async () => {
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeDisabled();
    expect(screen.getByText("Add text or a picture to start")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add text" }));
    const words = screen.getByLabelText(/^Words/) as HTMLInputElement;
    fireEvent.change(words, { target: { value: "x".repeat(200) } });
    expect(words.value).toHaveLength(120);
    fireEvent.click(screen.getByRole("button", { name: "Add to screen" }));
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeEnabled();
  });

  it("takes a typed time in seconds or minutes, and holds the cast outside 5 s to 5 min", async () => {
    render(<CastScreen />);
    await addText("Hi");
    const amount = screen.getByLabelText("On screen for");
    expect(amount).toHaveValue(30);
    // Switching the unit keeps the same time.
    fireEvent.change(screen.getByLabelText("Unit"), { target: { value: "min" } });
    expect(amount).toHaveValue(0.5);
    fireEvent.change(amount, { target: { value: "10" } });
    expect(screen.getByText(/5 seconds to 5 minutes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeDisabled();
    fireEvent.change(amount, { target: { value: "" } });
    expect(screen.getByText("Give it a time.")).toBeInTheDocument();
    fireEvent.change(amount, { target: { value: "2" } });
    expect(screen.queryByText(/5 seconds to 5 minutes/)).toBeNull();
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeEnabled();
  });

  it("adding nothing in the words sheet adds nothing", async () => {
    render(<CastScreen />);
    await addText("   ");
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeDisabled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shrinks words that would run off the screen, and says so", async () => {
    render(<CastScreen />);
    await addText("Dinner");
    expect(screen.queryByText(/so every word fits/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Edit/ }));
    fireEvent.change(screen.getByLabelText(/^Words/), {
      target: { value: "word ".repeat(24).trim() },
    });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByText(/^Shrunk to \d+ px so every word fits/)).toBeInTheDocument();
    expect(drawn.slice(-4).join(" ").split(" ").filter(Boolean)).toHaveLength(24);
  });

  it("drags the words to move them, and pinches or pulls their corner to resize them", async () => {
    render(<CastScreen />);
    await addText("Hi");
    const canvas = screen.getByLabelText("What the TV will show");
    // The canvas is 320 CSS px wide here: 6 TV px to one, so the words sit at (160, 90).
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 160, clientY: 90 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 170, clientY: 90 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    expect(drawnAt.at(-1)?.[0]).toBe(1020);
    const width = () => measuredAt.at(-1);
    expect(width()).toBe(140);

    // Two fingers pinch: the words grow with the spread and don't also move.
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 170, clientY: 90, timeStamp: 1000 });
    fireEvent.pointerDown(canvas, { pointerId: 2, clientX: 190, clientY: 90 });
    fireEvent.pointerMove(canvas, { pointerId: 2, clientX: 200, clientY: 90 });
    expect(width()).toBe(210);
    expect(drawnAt.at(-1)?.[0]).toBe(1020);
    // Lifting one finger ends the pinch: the one left doesn't jump into a drag.
    fireEvent.pointerUp(canvas, { pointerId: 2 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 120, clientY: 90 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    expect(drawnAt.at(-1)?.[0]).toBe(1020);

    // The corner handle: at 210 px "Hi" is a 344.4 x 285.6 box centred on (1020, 540), so its
    // corner is (1192.2, 682.8) TV px, (198.7, 113.8) on the phone. Pulling it out grows them.
    fireEvent.pointerDown(canvas, { pointerId: 3, clientX: 198.7, clientY: 113.8 });
    fireEvent.pointerMove(canvas, { pointerId: 3, clientX: 227.4, clientY: 137.6 });
    fireEvent.pointerUp(canvas, { pointerId: 3 });
    expect(width()).toBeGreaterThan(300);
    expect(drawnAt.at(-1)?.[0]).toBe(1020);
  });

  it("a twist of two fingers turns the words, settling square near a quarter turn", async () => {
    render(<CastScreen />);
    await addText("Hi");
    const canvas = screen.getByLabelText("What the TV will show");
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 160, clientY: 90 });
    fireEvent.pointerDown(canvas, { pointerId: 2, clientX: 180, clientY: 90 });
    // The second finger swings 30 degrees about the first, at the same spread.
    fireEvent.pointerMove(canvas, {
      pointerId: 2,
      clientX: 160 + 20 * Math.cos(Math.PI / 6),
      clientY: 90 + 20 * Math.sin(Math.PI / 6),
    });
    expect(turned.at(-1)).toBeCloseTo(Math.PI / 6);
    expect(measuredAt.at(-1)).toBe(140);
    // 87 degrees settles at 90.
    const a = (87 * Math.PI) / 180;
    fireEvent.pointerMove(canvas, {
      pointerId: 2,
      clientX: 160 + 20 * Math.cos(a),
      clientY: 90 + 20 * Math.sin(a),
    });
    expect(turned.at(-1)).toBe(Math.PI / 2);
  });

  it("touching the screen away from everything lets go, and a drag there moves nothing", async () => {
    render(<CastScreen />);
    await addText("Hi");
    const canvas = screen.getByLabelText("What the TV will show");
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 60, clientY: 40 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    expect(drawnAt.at(-1)).toEqual([960, 540]);
    expect(screen.queryByRole("group", { name: "Selected text" })).toBeNull();
    expect(screen.getByText(/Tap something on the screen/)).toBeInTheDocument();
  });

  it("a double-tap on the words opens them to edit; a style and Delete act on the held one", async () => {
    render(<CastScreen />);
    await addText("Hi");
    const canvas = screen.getByLabelText("What the TV will show");
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 160, clientY: 90, timeStamp: 100 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 160, clientY: 90, timeStamp: 300 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Words/)).toHaveValue("Hi");
    fireEvent.change(screen.getByLabelText(/^Words/), { target: { value: "Bye" } });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(drawn.at(-1)).toBe("Bye");

    const bar = within(screen.getByRole("group", { name: "Selected text" }));
    fireEvent.click(bar.getByRole("button", { name: "Yellow" }));
    expect(bar.getByRole("button", { name: "Yellow" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(bar.getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeDisabled();
  });

  it("keeps the draft while the owner looks at the TVs", async () => {
    render(<CastScreen />);
    await addText("Hi");
    fireEvent.click(screen.getByRole("button", { name: "TVs" }));
    drawn.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(drawn).toContain("Hi");
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeEnabled();
  });

  it("lists what was sent and takes one down from its sheet", async () => {
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [sent()], kept_s: 604800 });
    const stop = vi
      .spyOn(api, "castStop")
      .mockResolvedValue({ id: "n1", stopped: { a: true, b: false } });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Dinner's ready/ }));
    expect(screen.getByText("Cast to 2 of 2")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Take it down" }));
    });
    expect(stop).toHaveBeenCalledWith("n1");
    expect(screen.getByText("Taken down on 1 TV.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take it down" })).toBeNull();
  });

  it("reopens a cast sent from here in Send, its layers still movable", async () => {
    vi.spyOn(api, "castSend").mockResolvedValue({ sent: sent({ seconds: 120 }) });
    vi.spyOn(api, "castSent").mockResolvedValue({
      notices: [sent({ seconds: 120 })],
      kept_s: 604800,
    });
    render(<CastScreen />);
    await addText("Dinner's ready");
    fireEvent.click(screen.getByRole("button", { name: /^Cast to/ }));
    await screen.findByText("Cast to 2 of 2");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    // Start over, so what comes back is the sent one, not what was left on the canvas.
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    drawn.length = 0;
    const again = await screen.findByRole("button", { name: "Edit Dinner's ready again" });
    await act(async () => {
      fireEvent.click(again);
    });
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-pressed", "true");
    expect(drawn).toContain("Dinner's ready");
    expect(screen.getByLabelText("On screen for")).toHaveValue(2);
    expect(screen.getByLabelText("Unit")).toHaveValue("min");
    expect(screen.getByRole("button", { name: "TVs: Big TV, Parents" })).toBeInTheDocument();
  });

  it("reopens a cast it has no layers for as its frame, one picture", async () => {
    const decode = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(HTMLImageElement.prototype, "decode", {
      value: decode,
      configurable: true,
    });
    vi.spyOn(api, "castSent").mockResolvedValue({
      notices: [sent({ id: "old", tvs: [] })],
      kept_s: 604800,
    });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    const again = await screen.findByRole("button", { name: "Edit Dinner's ready again" });
    await act(async () => {
      fireEvent.click(again);
    });
    expect(decode).toHaveBeenCalled();
    expect(screen.queryByText("Add text or a picture to start")).not.toBeInTheDocument();
    // It reached no TV, so every TV that would take it now is picked.
    expect(screen.getByRole("button", { name: "TVs: Big TV, Parents" })).toBeInTheDocument();
  });

  it("drops a slow frame when another past cast was opened since", async () => {
    let finish: () => void = () => {};
    Object.defineProperty(HTMLImageElement.prototype, "decode", {
      value: () =>
        new Promise<void>((r) => {
          finish = r;
        }),
      configurable: true,
    });
    vi.spyOn(api, "castSend").mockResolvedValue({ sent: sent({ id: "new", title: "Fresh" }) });
    vi.spyOn(api, "castSent").mockResolvedValue({
      notices: [sent({ id: "new", title: "Fresh" }), sent({ id: "old", title: "Stale" })],
      kept_s: 604800,
    });
    render(<CastScreen />);
    await addText("Fresh");
    fireEvent.click(screen.getByRole("button", { name: /^Cast to/ }));
    await screen.findByText("Cast to 2 of 2");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));

    // The old one waits on its frame; the fresh one opens at once from what was kept.
    fireEvent.click(await screen.findByRole("button", { name: "Edit Stale again" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Edit Fresh again" }));
    });
    drawn.length = 0;
    await act(async () => finish());
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(drawn).toContain("Fresh");
  });

  it("says when a past cast can't be opened to edit", async () => {
    Object.defineProperty(HTMLImageElement.prototype, "decode", {
      value: vi.fn().mockRejectedValue(new Error("gone")),
      configurable: true,
    });
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [sent()], kept_s: 604800 });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    const again = await screen.findByRole("button", { name: "Edit Dinner's ready again" });
    await act(async () => {
      fireEvent.click(again);
    });
    expect(screen.getByText("Couldn't open that one to edit.")).toBeInTheDocument();
  });

  it("says when nothing was cast this week", async () => {
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [], kept_s: 604800 });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    expect(await screen.findByText("Nothing cast this week.")).toBeInTheDocument();
  });
});
