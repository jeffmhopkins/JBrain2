import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type CastNotice, type CastTv, api } from "../api/client";
import { CastScreen } from "./CastScreen";

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
function fakeCanvas() {
  let px = 10;
  const ctx = new Proxy(
    {
      measureText: (t: string) => ({ width: t.length * px * 0.5 }),
      fillText: (t: string, x: number, y: number) => {
        drawn.push(t);
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
  drawn.length = 0;
  drawnAt.length = 0;
  vi.spyOn(api, "castTvs").mockResolvedValue({ tvs: TVS });
  fakeCanvas();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("Cast", () => {
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

    // The ready and off TVs are picked to start with; the busy one is not.
    const picks = within(screen.getByRole("group", { name: "TVs" }));
    expect(picks.getByRole("button", { name: /Big TV/ })).toHaveAttribute("aria-pressed", "true");
    expect(picks.getByRole("button", { name: /Twins room/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    const cast = screen.getByRole("button", { name: /Cast to 2 TVs/ });
    expect(cast).toBeDisabled();

    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    fireEvent.change(screen.getByLabelText(/Words on the picture/), {
      target: { value: "Dinner's ready" },
    });
    // The words are drawn over the picture on the phone, as the TV will get them.
    expect(screen.getByLabelText("What the TV will show")).toBeInTheDocument();
    expect(drawn).toContain("Dinner's ready");

    // Picking a TV that is in use says so before sending.
    fireEvent.click(picks.getByRole("button", { name: /Twins room/ }));
    expect(
      screen.getByText("Twins room is netflix · playing and will be skipped."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 of 3 TVs/ }));

    await screen.findByText("Cast to 2 of 2");
    expect(send).toHaveBeenCalledWith({
      tv_ids: ["a", "b", "c"],
      title: "Dinner's ready",
      frame_b64: "RlJBTUU=",
      seconds: 30,
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
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    fireEvent.change(screen.getByLabelText(/Words on the picture/), { target: { value: "Look" } });
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 TVs/ }));
    expect(await screen.findByText("That picture couldn't be read.")).toBeInTheDocument();
  });

  it("says when the box can't reach the TVs to send", async () => {
    vi.spyOn(api, "castSend").mockRejectedValue(new ApiError(503, "cast sidecar unreachable"));
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    fireEvent.change(screen.getByLabelText(/Words on the picture/), { target: { value: "Look" } });
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 TVs/ }));
    expect(await screen.findByText("The box can't reach the TVs right now.")).toBeInTheDocument();
  });

  it("needs a photo or words before it can cast, and keeps words to what the box takes", async () => {
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    const words = screen.getByLabelText(/Words on the picture/) as HTMLInputElement;
    fireEvent.change(words, { target: { value: "x".repeat(200) } });
    expect(words.value).toHaveLength(120);
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeEnabled();
  });

  it("shrinks words that would run off the screen, and says so", async () => {
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    const words = screen.getByLabelText(/Words on the picture/);
    fireEvent.change(words, { target: { value: "Dinner" } });
    expect(screen.queryByText(/so every word fits/)).toBeNull();
    fireEvent.change(words, { target: { value: "word ".repeat(24).trim() } });
    expect(await screen.findByText(/^Shrunk to \d+ px so every word fits/)).toBeInTheDocument();
    expect(drawn.slice(-4).join(" ").split(" ").filter(Boolean)).toHaveLength(24);
  });

  it("two fingers on words alone don't drag them", async () => {
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    fireEvent.change(screen.getByLabelText(/Words on the picture/), { target: { value: "Hi" } });
    const canvas = screen.getByLabelText("What the TV will show");
    const before = drawnAt.at(-1);
    fireEvent.pointerDown(canvas, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerDown(canvas, { pointerId: 2, clientX: 50, clientY: 10 });
    fireEvent.pointerMove(canvas, { pointerId: 2, clientX: 90, clientY: 40 });
    expect(drawnAt.at(-1)).toEqual(before);
    fireEvent.pointerUp(canvas, { pointerId: 2 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 30, clientY: 10 });
    expect(drawnAt.at(-1)?.[0]).toBeGreaterThan(before?.[0] ?? 0);
  });

  it("keeps the draft while the owner looks at the TVs", async () => {
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    fireEvent.change(screen.getByLabelText(/Words on the picture/), { target: { value: "Hi" } });
    fireEvent.click(screen.getByRole("button", { name: "TVs" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(screen.getByRole("tab", { name: /Words/ }));
    expect(screen.getByLabelText(/Words on the picture/)).toHaveValue("Hi");
  });

  it("lists what was sent and takes one down from its sheet", async () => {
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [sent()], kept_s: 604800 });
    const stop = vi
      .spyOn(api, "castStop")
      .mockResolvedValue({ id: "n1", stopped: { a: true, b: false } });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    fireEvent.click(await screen.findByRole("button", { name: /Dinner's ready/ }));
    expect(screen.getByText("Cast to 2 of 2")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Take it down" }));
    });
    expect(stop).toHaveBeenCalledWith("n1");
    expect(screen.getByText("Taken down on 1 TV.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take it down" })).toBeNull();
  });

  it("says when nothing was cast this week", async () => {
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [], kept_s: 604800 });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    expect(await screen.findByText("Nothing cast this week.")).toBeInTheDocument();
  });
});
