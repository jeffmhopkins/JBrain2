// The map's Timeline tab (plan §M8a, mock a-full-bleed.html #nav=3), against the reviewed
// fixture's week with the clock pinned at its "now", Sat Oct 10 2026, 2:40 pm: the day
// strip, a day and a session loaded, the status that never says Live away from now, Back
// to live, Follow, the speeds, the jump into the Nether, a death's cause, the session list.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftDim, MinecraftTimelinePart } from "../api/client";
import {
  mcAllowlist,
  mcPlayers,
  mcServer,
  mcServerSettings,
  mcStatus,
  mcVersion,
  mcWorlds,
} from "../minecraftFixtures";
import {
  mcMapInfo,
  mcMapPlayers,
  mcMapPoint,
  mcMapStructures,
  mcTimeline,
} from "../minecraftMapFixtures";
import { MinecraftScreen } from "./MinecraftScreen";

const NOW = new Date(2026, 9, 10, 14, 40);
const at = (d: number, h: number, m: number) => new Date(2026, 9, d, h, m).getTime() / 1000;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The words use non-breaking spaces so a time never wraps. */
const text = (el: Element | null) => (el?.textContent ?? "").replace(/ /g, " ");

describe("the map's timeline", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    // Only the date is faked, from the fixture's now on: timers and animation frames run as
    // usual, and the clock still moves so a waitFor can time out.
    vi.useFakeTimers({ toFake: ["Date"], shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const server = mcServer();
    fetchMock.mockImplementation(async (input) => {
      const url = new URL(String(input), "http://box");
      const path = url.pathname;
      const q = url.searchParams;
      const slot = q.get("slot") ?? "slot1";
      const dim = (q.get("dim") ?? "overworld") as MinecraftDim;
      if (path === "/api/minecraft") return json(mcStatus(server));
      if (path.startsWith("/api/minecraft/version")) return json(mcVersion());
      if (path === "/api/minecraft/players") return json(mcPlayers());
      if (path === "/api/minecraft/worlds") return json(mcWorlds());
      if (path === "/api/minecraft/allowlist") return json(mcAllowlist());
      if (path === "/api/minecraft/server-settings") return json(mcServerSettings());
      if (path === "/api/minecraft/backups") return json({ snapshots: [] });
      if (path === "/api/minecraft/map/info") return json(mcMapInfo(slot, dim));
      if (path === "/api/minecraft/map/players") return json(mcMapPlayers(slot, slot === "slot2"));
      if (path === "/api/minecraft/map/structures") return json(mcMapStructures(slot, dim));
      if (path === "/api/minecraft/map/point") {
        return json(mcMapPoint(slot, dim, Number(q.get("x")), Number(q.get("z"))));
      }
      if (path === "/api/minecraft/timeline") {
        return json(
          mcTimeline(slot, Number(q.get("from")), Number(q.get("to")), {
            include: (q.get("include") ?? "trails,events,sessions").split(
              ",",
            ) as MinecraftTimelinePart[],
            players: (q.get("players") ?? "").split(",").filter(Boolean),
            dim,
          }),
        );
      }
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const timelineCalls = () =>
    fetchMock.mock.calls
      .map(([u]) => new URL(String(u), "http://box"))
      .filter((u) => u.pathname === "/api/minecraft/timeline");

  const status = () => text(document.querySelector(".mc-map-status"));

  // The first, cold render loads the whole map screen: on a busy CI runner that alone has
  // taken longer than the default 1 s wait.
  const OPEN = { timeout: 5000 };

  async function openTimeline() {
    render(<MinecraftScreen />);
    const row = await screen.findByRole("button", { name: /^Map/ }, OPEN);
    await waitFor(() => expect(row).toHaveTextContent("on the map"), OPEN);
    fireEvent.click(row);
    await screen.findByRole("application", undefined, OPEN);
    await screen.findByText("Live · 2 on", undefined, OPEN);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    return screen.findByRole("radiogroup", { name: "Day" }, OPEN);
  }

  async function loadFoxsNetherTrip() {
    const days = await openTimeline();
    fireEvent.click(within(days).getByRole("radio", { name: /^Yesterday/ }));
    const chip = await screen.findByRole("button", { name: /^BlockyFox, 4:10.7:52.pm/ });
    fireEvent.click(chip);
    await waitFor(() => expect(chip).toHaveAttribute("aria-pressed", "true"));
    return chip;
  }

  it("opens at now — still live — with a week of days and today's sessions", async () => {
    const days = await openTimeline();
    const strip = within(days).getAllByRole("radio");
    expect(strip.map((d) => text(d))).toEqual([
      "Sun4",
      "Mon5",
      "Tue6",
      "Wed7",
      "Thu8",
      "Fri9",
      "Today10",
    ]);
    expect(within(days).getByRole("radio", { name: /^Today/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(within(days).getByRole("radio", { name: /^Yesterday/ })).toHaveAccessibleName(
      "Yesterday — 3 sessions: BlockyFox, Mira_P, Steve42",
    );
    const chips = await screen.findByRole("group", { name: "Sessions that day" });
    expect(within(chips).getAllByRole("button").map(text)).toEqual([
      "M9:40 am45 min",
      "B1:58 pmon now",
      "M2:33 pmon now",
    ]);
    // Pinned to now, the timeline is still the live view, and says so.
    expect(status()).toMatch(/^Live · 2 on/);
    expect(screen.getByRole("button", { name: /Live/ })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(timelineCalls().length).toBeGreaterThan(1));
  });

  it("a day sets the range to its activity; a chip loads its session from the start", async () => {
    const days = await openTimeline();
    fireEvent.click(within(days).getByRole("radio", { name: /^Yesterday/ }));
    await waitFor(() => expect(status()).toMatch(/^Not live · yesterday 4:10 pm/));
    const slider = screen.getByRole("slider", { name: "Time" });
    expect(Number(slider.getAttribute("aria-valuemin"))).toBe(at(9, 16, 10) - 600);
    expect(Number(slider.getAttribute("aria-valuemax"))).toBe(at(9, 20, 14) + 600);
    const chips = screen.getByRole("group", { name: "Sessions that day" });
    for (const c of within(chips).getAllByRole("button")) {
      expect(c).toHaveAttribute("aria-pressed", "false");
    }

    const chip = within(chips).getByRole("button", { name: /^BlockyFox/ });
    expect(chip).toHaveAccessibleName(/3h.42m, 1 death$/);
    fireEvent.click(chip);
    await waitFor(() => expect(chip).toHaveAttribute("aria-pressed", "true"));
    expect(Number(slider.getAttribute("aria-valuemin"))).toBe(at(9, 16, 10) - 120);
    expect(Number(slider.getAttribute("aria-valuemax"))).toBe(at(9, 19, 52) + 120);
    expect(Number(slider.getAttribute("aria-valuenow"))).toBe(at(9, 16, 10));
    expect(status()).toBe(
      "Not live · yesterday 4:10 pmPaused · positions from the travel log · terrain as of now",
    );
    // The range was asked for once, whole: scrubbing inside it asks nothing more.
    const asked = timelineCalls().at(-1);
    expect(Number(asked?.searchParams.get("from"))).toBe(at(9, 16, 10) - 120);
    const before = timelineCalls().length;
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    fireEvent.keyDown(slider, { key: "End" });
    expect(Number(slider.getAttribute("aria-valuenow"))).toBe(at(9, 19, 52) + 120);
    expect(timelineCalls().length).toBe(before);
    expect(status()).not.toMatch(/\bLive\b/);
  });

  it("Back to live leaves the replay from either tab", async () => {
    await loadFoxsNetherTrip();
    // In the Timeline tab, the transport's Now is the way back to the live edge.
    fireEvent.click(screen.getByRole("button", { name: "Now" }));
    await waitFor(() => expect(status()).toMatch(/^Live · 2 on/));

    fireEvent.click(screen.getByRole("radio", { name: /^Yesterday/ }));
    await waitFor(() => expect(status()).toMatch(/^Not live/));
    // Time mode survives the tabs: Layers keeps saying Not live, and offers the way back.
    fireEvent.click(screen.getByRole("tab", { name: "Layers" }));
    expect(status()).toMatch(/^Not live · yesterday/);
    fireEvent.click(screen.getByRole("button", { name: "Back to live" }));
    await waitFor(() => expect(status()).toMatch(/^Live · 2 on/));
    expect(screen.queryByRole("button", { name: "Back to live" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Layers" })).toHaveAttribute("aria-selected", "true");
  });

  it("speed cycles one tap at a time; play replays and pause holds the moment", async () => {
    await loadFoxsNetherTrip();
    const speed = screen.getByRole("button", { name: /^Playback speed/ });
    const seen = [text(speed)];
    for (let i = 0; i < 4; i++) {
      fireEvent.click(speed);
      seen.push(text(speed));
    }
    expect(seen).toEqual(["60×", "600×", "1×", "10×", "60×"]);
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    await waitFor(() => expect(status()).toMatch(/Replaying at 60×/));
    expect(status()).toMatch(/^Not live/);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(status()).toMatch(/Paused/));
  });

  it("Follow rides into the Nether with the player, and says so", async () => {
    await loadFoxsNetherTrip();
    const follow = screen.getByRole("button", { name: "Follow BlockyFox" });
    expect(follow).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(follow);
    expect(follow).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("radio", { name: "Overworld" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    const portal = screen.getByRole("button", { name: /BlockyFox went to the Nether/ });
    fireEvent.click(portal);
    expect(await screen.findByText("BlockyFox went to the Nether")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Nether" })).toHaveAttribute("aria-checked", "true");
    expect(status()).toMatch(/^Not live · yesterday 4:\d\d pm/);
    // Dragging the map away from them stops following.
    const map = screen.getByRole("application");
    fireEvent.pointerDown(map, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(map, { pointerId: 1, clientX: 160, clientY: 140 });
    fireEvent.pointerUp(map, { pointerId: 1, clientX: 160, clientY: 140 });
    expect(follow).toHaveAttribute("aria-pressed", "false");
  });

  it("a death on the scrubber pins its cause and place", async () => {
    await loadFoxsNetherTrip();
    fireEvent.click(screen.getByRole("button", { name: /BlockyFox burned in lava/ }));
    const row = await screen.findByText("BlockyFox burned in lava");
    expect(text(row.parentElement)).toMatch(/X 41 · Z -58/);
    expect(screen.getByRole("button", { name: "Copy X Z 41 -58" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Nether" })).toHaveAttribute("aria-checked", "true");
    expect(status()).toMatch(/^Not live/);
  });

  it("the session list is every session, newest first, by player; a row loads it", async () => {
    await openTimeline();
    fireEvent.click(screen.getByRole("button", { name: "All sessions" }));
    const sheet = await screen.findByRole("dialog", { name: "Sessions" });
    const who = within(sheet).getByRole("radiogroup", { name: "Whose sessions" });
    expect(within(who).getAllByRole("radio").map(text)).toEqual([
      "All players 17",
      "BlockyFox 6",
      "Mira_P 6",
      "Pebble_J 2",
      "Steve42 3",
    ]);
    fireEvent.click(within(who).getByRole("radio", { name: /^BlockyFox/ }));
    const rows = within(sheet).getAllByRole("button", { name: /–/ });
    expect(rows.map((r) => text(r.querySelector(".mc-sl-t")))).toEqual([
      "1:58 pm–nowon now",
      "4:10–7:52 pm",
      "4:05–5:12 pm",
      "10:20–11:05 am",
      "3:31–4:48 pm",
      "4:02–5:31 pm",
    ]);
    // The Nether trip carries its portal and its death.
    expect(text(rows[1] ?? null)).toMatch(/Nether.*1 death/);
    fireEvent.click(rows[1] as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Sessions" })).toBeNull());
    expect(await screen.findByRole("button", { name: /^BlockyFox, 4:10/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() => expect(status()).toMatch(/^Not live · yesterday 4:10 pm/));
  });

  it("a range preset and who's shown", async () => {
    await openTimeline();
    const week = screen.getByRole("radio", { name: "This week" });
    fireEvent.click(week);
    expect(week).toHaveAttribute("aria-checked", "true");
    const slider = screen.getByRole("slider", { name: "Time" });
    expect(Number(slider.getAttribute("aria-valuemin"))).toBe(NOW.getTime() / 1000 - 7 * 86_400);
    const shown = screen.getByRole("group", { name: "Who's shown" });
    const steve = within(shown).getByRole("button", { name: /Steve42/ });
    expect(steve).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(steve);
    expect(steve).toHaveAttribute("aria-pressed", "false");
  });

  describe("on a drawn map", () => {
    // A 2D context that draws nothing but remembers where discs and skulls went, so a
    // test can tap what the map drew; and a phone-sized page to draw it on.
    let calls: { name: string; args: unknown[] }[] = [];
    beforeEach(() => {
      calls = [];
      const ctx = new Proxy(
        {},
        {
          get: (_t, name: string) =>
            name === "measureText"
              ? (s: string) => ({ width: s.length * 6 })
              : (...args: unknown[]) => {
                  // Each frame starts by setting the transform: keep only the latest frame.
                  if (name === "setTransform") calls = [];
                  calls.push({ name, args });
                },
          set: () => true,
        },
      );
      vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
        ctx as unknown as CanvasRenderingContext2D,
      );
      const real = HTMLElement.prototype.getBoundingClientRect;
      const phone = {
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        right: 390,
        bottom: 844,
        width: 390,
        height: 844,
      };
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
        this: HTMLElement,
      ) {
        // Only the map's own page is phone-sized; the rest of the app measures as jsdom does.
        const map = this.classList.contains("mc-map") || this.classList.contains("mc-map-view");
        return map ? ({ ...phone, toJSON: () => ({}) } as DOMRect) : real.call(this);
      });
    });

    /** Where the last frame drew a player's initial, or a skull's rose disc. */
    const drawn = (pick: (c: { name: string; args: unknown[] }) => boolean, from = 0) => {
      const hit = calls.filter(pick).at(-1);
      return hit ? { x: Number(hit.args[from]), y: Number(hit.args[from + 1]) } : null;
    };

    function tapAt(el: Element, x: number, y: number) {
      fireEvent.pointerDown(el, { pointerId: 1, clientX: x, clientY: y });
      fireEvent.pointerUp(el, { pointerId: 1, clientX: x, clientY: y });
    }

    async function openLiveMap() {
      render(<MinecraftScreen />);
      const row = await screen.findByRole("button", { name: /^Map/ });
      await waitFor(() => expect(row).toHaveTextContent("on the map"));
      fireEvent.click(row);
      const map = await screen.findByRole("application");
      // jsdom loads no tiles, so the line counts them too; it is still the live view.
      await waitFor(() => expect(status()).toMatch(/Live · 2 on/));
      await waitFor(() => expect(timelineCalls().length).toBeGreaterThan(0));
      return map;
    }

    it("a player's Follow opens the timeline following them; Sessions opens their list", async () => {
      const map = await openLiveMap();
      let at: { x: number; y: number } | null = null;
      await waitFor(() => {
        at = drawn((c) => c.name === "fillText" && c.args[0] === "B", 1);
        expect(at).not.toBeNull();
      });
      const fox = at as unknown as { x: number; y: number };
      tapAt(map, fox.x, fox.y);
      expect(await screen.findByText(/^online · here now/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Sessions" }));
      const sheet = await screen.findByRole("dialog", { name: "Sessions" });
      expect(within(sheet).getByRole("radio", { name: /^BlockyFox/ })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      fireEvent.click(within(sheet).getByRole("button", { name: "Close" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

      fireEvent.click(screen.getByRole("button", { name: "Follow" }));
      expect(await screen.findByRole("button", { name: "Follow BlockyFox" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.getByRole("tab", { name: "Timeline" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      // jsdom loads no tiles, so the line also counts them; it still isn't Not live.
      expect(status()).toMatch(/Live · 2 on/);
      expect(status()).not.toMatch(/Not live/);
    });

    it("a skull on the live map gives the cause and replays the minutes before", async () => {
      const map = await openLiveMap();
      let skull: { x: number; y: number } | null = null;
      await waitFor(() => {
        // The skull's rose disc is the only 9-px circle the map draws.
        skull = drawn((c) => c.name === "arc" && c.args[2] === 9, 0);
        expect(skull).not.toBeNull();
      });
      const s = skull as unknown as { x: number; y: number };
      tapAt(map, s.x, s.y);
      const replay = await screen.findByRole("button", { name: "Replay the minutes before" });
      expect(
        screen.getByText(
          /^(BlockyFox|Mira_P|Steve42|Pebble_J) (killed by a Zombie|blown up by a Creeper|drowned|fell from a high place|burned in lava)$/,
        ),
      ).toBeInTheDocument();
      fireEvent.click(replay);
      await waitFor(() => expect(status()).toMatch(/^Not live · .*Replaying at 10×/));
      expect(screen.getByRole("button", { name: /^Follow / })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    });
  });
});
