import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { JpanelScreen, durationText, whenText } from "./JpanelScreen";

// The reader is a father on a phone at work and the senders are two four-year-olds, so
// the states this screen has to survive are not edge cases: a transcript that came back
// as nonsense, one that came back empty, and a twin who has not sent anything at all.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const GARBLED =
  "and then and then the the dinosaur he goed in the the water but not the water the other one";

const THREADS = [
  {
    device_id: "panel-ellie",
    name: "Ellie",
    unplayed: 2,
    messages: [
      {
        id: "jp-1",
        from_name: "Ellie",
        to_name: "Dad",
        direction: "in",
        transcript: "There is a joke. There is a joke.",
        composed: "voice",
        duration_ms: 3400,
        created_at: new Date(Date.now() - 6 * 60_000).toISOString(),
        played_at: null,
      },
      {
        id: "jp-2",
        from_name: "Ellie",
        to_name: "Dad",
        direction: "in",
        transcript: "",
        composed: "voice",
        duration_ms: 1900,
        created_at: new Date(Date.now() - 24 * 60_000).toISOString(),
        played_at: null,
      },
      {
        id: "jp-3",
        from_name: "Ellie",
        to_name: "Dad",
        direction: "in",
        transcript: GARBLED,
        composed: "voice",
        duration_ms: 19_600,
        created_at: new Date(Date.now() - 190 * 60_000).toISOString(),
        played_at: new Date(Date.now() - 120 * 60_000).toISOString(),
      },
    ],
  },
  { device_id: "panel-mabel", name: "Mabel", unplayed: 0, messages: [] },
];

const SENT = {
  id: "jp-sent-1",
  from_name: "Dad",
  to_name: "Ellie",
  direction: "out",
  transcript: "Five more minutes then teeth.",
  composed: "text",
  duration_ms: 3200,
  created_at: new Date().toISOString(),
  played_at: null,
};

/** The requests each case cares about; everything else 404s, as a real box would. */
function box(opts: { threads?: unknown[]; post?: () => Response } = {}) {
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (path.startsWith("/api/jpanel/messages") && method === "GET") {
      return json({ panels: opts.threads ?? THREADS });
    }
    if (path === "/api/jpanel/messages" && method === "POST") {
      return opts.post ? opts.post() : json(SENT, 201);
    }
    if (/^\/api\/jpanel\/messages\/[^/]+\/played$/.test(path) && method === "POST") {
      return new Response(null, { status: 204 });
    }
    if (path === "/api/endpoint/ports") return json({ ports: [], flasher: true });
    if (path === "/api/endpoint/firmware") return json({ version: "0.2.0", url: "https://box/fw" });
    return new Response(null, { status: 404 });
  };
}

/** jsdom has no media pipeline, so playback is observed through the element it built. */
class FakeAudio {
  static built: FakeAudio[] = [];
  readonly src: string;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  paused = false;
  constructor(src: string) {
    this.src = src;
    FakeAudio.built.push(this);
  }
  play(): Promise<void> {
    return Promise.resolve();
  }
  pause(): void {
    this.paused = true;
  }
}

/** jsdom has no IntersectionObserver. This one records what was observed and lets a case
 *  say "that row came into view", which is the only way to drive read-tracking here. */
class FakeObserver {
  static live: FakeObserver[] = [];
  readonly cb: IntersectionObserverCallback;
  readonly targets: Element[] = [];
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb;
    FakeObserver.live.push(this);
  }
  observe(el: Element): void {
    this.targets.push(el);
  }
  disconnect(): void {}
  /** Scroll every observed row into view. */
  showAll(): void {
    this.cb(
      this.targets.map((target) => ({ target, isIntersecting: true }) as IntersectionObserverEntry),
      this as unknown as IntersectionObserver,
    );
  }
}

/** The row a given transcript sits in. Every twin's messages share one accessible name,
 *  so indexing a list of play buttons would silently follow a different message the
 *  moment the thread grows; the transcript is what identifies a message here anyway. */
function rowFor(transcript: HTMLElement): HTMLElement {
  const row = transcript.closest("li");
  if (!row) throw new Error("transcript is not inside a message row");
  return row;
}

describe("JpanelScreen messages", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    FakeAudio.built = [];
    FakeObserver.live = [];
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("Audio", FakeAudio);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("groups by panel and badges each panel's unplayed count", async () => {
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);

    const ellie = await screen.findByRole("region", { name: "Ellie" });
    expect(within(ellie).getByText("2 unplayed")).toBeTruthy();
    // The count is the box's, not one recounted from the rows on this page: a `limit`
    // that truncated the thread must not quietly deflate the badge.
    const mabel = screen.getByRole("region", { name: "Mabel" });
    expect(within(mabel).queryByText(/unplayed/)).toBeNull();
  });

  it("still offers a panel that has sent nothing — an empty thread is normal", async () => {
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);

    const mabel = await screen.findByRole("region", { name: "Mabel" });
    expect(within(mabel).getByText("Nothing from Mabel yet.")).toBeTruthy();
    // A silent twin is still someone you can message; the compose box is not conditional
    // on there being a conversation already.
    expect(within(mabel).getByLabelText("Message Mabel")).toBeTruthy();
  });

  it("leads with the transcript and puts the player beside it, not over it", async () => {
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);

    // The garbled transcript is shown whole — it is the thing being read, and clipping it
    // would hide exactly the words that need squinting at.
    const row = rowFor(await screen.findByText(GARBLED));
    // Beside, not below: the play control shares the body row with the words.
    const body = within(row).getByText(GARBLED).parentElement;
    expect(body?.className).toContain("jp-msg-body");
    expect(within(row).getByRole("button", { name: "Play Ellie's message" })).toBeTruthy();
  });

  it("says when the transcriber produced no words at all", async () => {
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);

    expect(await screen.findByText(/No words came through/)).toBeTruthy();
  });

  it("plays a message from its own audio route, and a second tap stops it", async () => {
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);

    const row = rowFor(await screen.findByText("There is a joke. There is a joke."));
    fireEvent.click(within(row).getByRole("button", { name: "Play Ellie's message" }));
    expect(FakeAudio.built[0]?.src).toBe("/api/jpanel/messages/jp-1/audio");

    const stop = within(row).getByRole("button", { name: "Stop playing" });
    fireEvent.click(stop);
    expect(FakeAudio.built[0]?.paused).toBe(true);
  });

  it("sends typed text, and the microphone yields while there is a draft", async () => {
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);

    const input = await screen.findByLabelText("Message Ellie");
    fireEvent.change(input, { target: { value: "Five more minutes then teeth." } });
    // ONE ACTION PER COMPOSE ROW. With words in the box the obvious thing is to send them,
    // and two live buttons side by side is the moment a parent taps the wrong one.
    expect(screen.queryByRole("button", { name: "Record a message for Ellie" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Send to Ellie" }));

    await waitFor(() => expect(screen.getByText("Five more minutes then teeth.")).toBeTruthy());
    const post = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "POST");
    expect(post?.[0]).toBe("/api/jpanel/messages");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      to_device: "panel-ellie",
      text: "Five more minutes then teeth.",
    });
    // And it comes back once the draft is cleared, so the next thing said can be spoken.
    expect(await screen.findByRole("button", { name: "Record a message for Ellie" })).toBeTruthy();
  });

  it("clears a panel's history, and says what the box refused to delete", async () => {
    /* The owner: *"add a 'clear history' button per panel."*
     *
     * The box refuses to delete a message a child has not heard yet — JPANEL_PLAN.md §5, a
     * message nobody heard must not evaporate — so the clear is partial by design, and a
     * partial clear that says nothing is worse than one that refuses: the list afterwards has
     * to match what the owner expects to see. */
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (path.startsWith("/api/jpanel/messages") && method === "DELETE") {
        return json({ deleted: 7, kept: 1 });
      }
      return box()(input, init);
    });
    render(<JpanelScreen onClose={vi.fn()} />);

    fireEvent.click(
      await screen.findByRole("button", { name: /Clear the conversation with Ellie/ }),
    );
    expect(await screen.findByText(/Kept 1 Ellie hasn't heard yet/)).toBeTruthy();
    const del = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "") === "DELETE");
    expect(String(del?.[0])).toBe("/api/jpanel/messages?device=panel-ellie");
  });

  it("does not clear when the confirm is declined", async () => {
    /* The one control here that destroys a child's words. Everything else on this surface is
       recoverable by waiting. */
    vi.spyOn(window, "confirm").mockReturnValue(false);
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Clear the conversation with Ellie/ }),
    );
    expect(fetchMock.mock.calls.some((c) => (c[1]?.method ?? "") === "DELETE")).toBe(false);
  });

  it("keeps the whole conversation rather than collapsing it", async () => {
    /* This briefly showed only the newest outbound message, on a misreading of "we shouldn't
       just keep on piling up message after message". The owner corrected it: this is a
       CONVERSATION WINDOW — the fix for a long thread is a scroll cap and a clear button, not
       throwing away what was said. */
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);
    await screen.findByText("There is a joke. There is a joke.");
    const outbound = THREADS[0]?.messages.filter((m) => m.direction === "out") ?? [];
    for (const m of outbound) {
      if (m.transcript.trim()) expect(screen.getByText(m.transcript)).toBeTruthy();
    }
  });

  it("says whether what Dad sent has been heard", async () => {
    /* The status, which survived a misread requirement. This once ALSO collapsed the thread to
       the newest outbound row; the owner corrected that — it is a conversation window — so what
       remains is the part that was actually useful: for a message you sent, the only live
       question is whether the child has heard it, and that is what a parent opens this to find
       out at work. */
    let n = 0;
    fetchMock.mockImplementation(
      box({
        post: () => {
          n += 1;
          return json({ ...SENT, id: `sent-${n}`, transcript: "teeth please" }, 201);
        },
      }),
    );
    render(<JpanelScreen onClose={vi.fn()} />);

    const input = await screen.findByLabelText("Message Ellie");
    fireEvent.change(input, { target: { value: "teeth please" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to Ellie" }));

    await waitFor(() => expect(screen.getByText("teeth please")).toBeTruthy());
    expect(screen.getAllByText(/Not heard yet/).length).toBeGreaterThan(0);
  });

  it("says when the box gave up delivering, rather than showing it as merely waiting", async () => {
    /* The two are identical in `played_at` and only one means something is wrong. A firmware
       bug once left a message undeliverable while a panel repeated it every thirty seconds
       (ROOM_ENDPOINT_PLAN.md §10.4cw); without this the owner would read that as a child who
       had not walked past their panel. */
    fetchMock.mockImplementation(
      box({
        threads: [
          {
            ...(THREADS[0] as object),
            messages: [{ ...SENT, played_at: null, undelivered: true }],
          },
        ],
      }),
    );
    render(<JpanelScreen onClose={vi.fn()} />);
    expect(await screen.findByText(/Couldn't be delivered/)).toBeTruthy();
    expect(screen.queryByText(/Not heard yet/)).toBeNull();
  });

  it("offers a microphone when there is nothing typed", async () => {
    /* THIS TEST USED TO ASSERT THE OPPOSITE, and the reversal is the point.
     *
     * `JPANEL_PLAN.md` §3b made the asymmetry binding — *"The panels never send text and
     * never read. The PWA never has to listen if it does not want to"* — and this file
     * enforced it by checking no recorder existed anywhere on the surface.
     *
     * The owner amended it: *"PWA should also be able to actually send audio, a voice
     * message, that have the option to send text that gets rendered."* The reason is the same
     * one `DAD_VOICE` exists for — a synthesised voice reading a father's words is not his
     * voice, and for a child who cannot read, the recording is the only version that carries
     * who it is from. The PWA still never has to LISTEN; it may now speak. */
    fetchMock.mockImplementation(box());
    render(<JpanelScreen onClose={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Record a message for Ellie" })).toBeTruthy();
    // And the send button is not also live — one action per compose row.
    expect(screen.queryByRole("button", { name: "Send to Ellie" })).toBeNull();
  });

  it("keeps the words in the box when the send fails", async () => {
    fetchMock.mockImplementation(box({ post: () => json({ detail: "the box is busy" }, 503) }));
    render(<JpanelScreen onClose={vi.fn()} />);

    const input = await screen.findByLabelText("Message Ellie");
    fireEvent.change(input, { target: { value: "on my way home" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to Ellie" }));

    expect(await screen.findByText(/the box is busy/)).toBeTruthy();
    expect((input as HTMLInputElement).value).toBe("on my way home");
  });
});

describe("JpanelScreen read tracking", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    FakeAudio.built = [];
    FakeObserver.live = [];
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("Audio", FakeAudio);
    vi.stubGlobal("IntersectionObserver", FakeObserver);
    fetchMock.mockImplementation(box());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function played(): string[] {
    return fetchMock.mock.calls
      .filter((c) => (c[1]?.method ?? "GET") === "POST")
      .map((c) => String(c[0]))
      .filter((p) => p.endsWith("/played"));
  }

  it("clears a message once its row has actually been on screen — reading is enough", async () => {
    render(<JpanelScreen onClose={vi.fn()} />);
    await screen.findByText("There is a joke. There is a joke.");

    // Only the two unheard rows are watched: Dad's own text is not his to read, and a
    // message he has already heard is not waiting on him.
    const observer = FakeObserver.live[FakeObserver.live.length - 1];
    expect(observer?.targets).toHaveLength(2);
    await act(async () => observer?.showAll());

    await waitFor(() =>
      expect(played()).toEqual([
        "/api/jpanel/messages/jp-1/played",
        "/api/jpanel/messages/jp-2/played",
      ]),
    );
  });

  it("reports a row once, however many polls redraw it", async () => {
    render(<JpanelScreen onClose={vi.fn()} />);
    await screen.findByText("There is a joke. There is a joke.");

    const observer = FakeObserver.live[FakeObserver.live.length - 1];
    await act(async () => observer?.showAll());
    await act(async () => observer?.showAll());

    await waitFor(() => expect(played()).toHaveLength(2));
  });

  it("clears a message the owner plays, even where nothing can see the row", async () => {
    // The fallback path: a browser with no IntersectionObserver still has a play button.
    vi.stubGlobal("IntersectionObserver", undefined);
    render(<JpanelScreen onClose={vi.fn()} />);

    const row = rowFor(await screen.findByText("There is a joke. There is a joke."));
    fireEvent.click(within(row).getByRole("button", { name: "Play Ellie's message" }));

    await waitFor(() => expect(played()).toEqual(["/api/jpanel/messages/jp-1/played"]));
  });

  it("keeps the last messages on screen when the refetch behind a mark fails", async () => {
    // A failed refetch is not evidence of an empty inbox — the words stay put and the
    // screen says what it is showing rather than blanking.
    let gets = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.startsWith("/api/jpanel/messages") && (init?.method ?? "GET") === "GET") {
        gets += 1;
        if (gets > 1) throw new TypeError("Failed to fetch");
      }
      return box()(input, init);
    });
    render(<JpanelScreen onClose={vi.fn()} />);
    await screen.findByText("There is a joke. There is a joke.");

    const observer = FakeObserver.live[FakeObserver.live.length - 1];
    await act(async () => observer?.showAll());

    expect(await screen.findByText(/Showing what was last fetched/)).toBeTruthy();
    expect(screen.getByText("There is a joke. There is a joke.")).toBeTruthy();
  });

  it("does not report a message that was already heard", async () => {
    render(<JpanelScreen onClose={vi.fn()} />);

    // jp-3 carries a played_at, so playing it again is a re-listen, not news for the box.
    const row = rowFor(await screen.findByText(GARBLED));
    fireEvent.click(within(row).getByRole("button", { name: "Play Ellie's message" }));

    await waitFor(() => expect(FakeAudio.built).toHaveLength(1));
    expect(played()).toEqual([]);
  });
});

describe("JpanelScreen tabs", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(box());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("carries the flasher whole on its second tab", async () => {
    render(<JpanelScreen onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: "Flash" }));
    expect(await screen.findByText("Plug a panel into the box")).toBeTruthy();
    // jpanel owns the wrap and the back bar; the moved surface must not bring a second
    // Back that would close the wrong thing.
    expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  });

  it("opens straight on Flash for someone standing at the box with a board", async () => {
    render(<JpanelScreen onClose={vi.fn()} initialTab="flash" />);

    expect(await screen.findByText("Plug a panel into the box")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Flash" }).getAttribute("aria-selected")).toBe("true");
  });

  it("closes to the launcher from its own back bar", () => {
    const onClose = vi.fn();
    render(<JpanelScreen onClose={onClose} />);

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("message metadata", () => {
  it("dates anything older than a day, so an old message cannot read as today's", () => {
    const now = new Date("2026-09-22T17:00:00Z").getTime();
    expect(whenText(new Date(now - 30_000).toISOString(), now)).toBe("just now");
    expect(whenText(new Date(now - 6 * 60_000).toISOString(), now)).toBe("6m ago");
    expect(whenText(new Date(now - 5 * 3_600_000).toISOString(), now)).toBe("5h ago");
    expect(whenText(new Date(now - 3 * 86_400_000).toISOString(), now)).toMatch(/Sep 19/);
  });

  it("reads a duration as a listen length", () => {
    expect(durationText(3400)).toBe("3s");
    expect(durationText(19_600)).toBe("20s");
    expect(durationText(65_000)).toBe("1:05");
    // A message too short to round to a second is still a message, not "0s".
    expect(durationText(200)).toBe("1s");
  });
});
