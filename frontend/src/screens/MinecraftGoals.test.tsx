// Players' goals on the Minecraft screen (plan §M24; GUI gate docs/mocks/minecraft-goals/,
// variant B "expandable if clicked", look-only). Every state the gate drew: the glance
// lines, a player's goals expanded in place, a goal's detail, the finished goals folded,
// an offline player, a world that isn't loaded, no goals, and a server that can't count.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftGoalCount, MinecraftGoals } from "../api/client";
import { mockFetch } from "../api/mock";
import {
  mcGoalProgress,
  mcGoals,
  mcPlayers,
  mcStatus,
  mcVersion,
  mcWorlds,
} from "../minecraftFixtures";
import { itemWords, moreText, whyParts } from "../minecraftGoals";
import { MinecraftScreen } from "./MinecraftScreen";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const NB = " ";
/** Text as a person reads it: the no-break spaces that hold a value together are spaces. */
const read = (el: Element | null) => (el?.textContent ?? "").replaceAll(NB, " ");

describe("Minecraft goals", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let goals: MinecraftGoals | (() => Response) = mcGoals();
  let progressDown = false;

  beforeEach(() => {
    goals = mcGoals();
    progressDown = false;
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input) => {
      const url = new URL(String(input), "http://x");
      const path = url.pathname;
      if (path === "/api/minecraft") return json(mcStatus());
      if (path === "/api/minecraft/version") return json(mcVersion());
      if (path === "/api/minecraft/worlds") return json(mcWorlds());
      if (path === "/api/minecraft/players") return json(mcPlayers());
      if (path === "/api/minecraft/goals") {
        return typeof goals === "function" ? goals() : json(goals);
      }
      if (path === "/api/minecraft/goals/progress") {
        if (progressDown) {
          return json({ detail: "Minecraft sidecar unreachable — is it stopped?" }, 503);
        }
        const xuid = url.searchParams.get("xuid") ?? "";
        return json(mcGoalProgress(xuid, url.searchParams.get("flush") === "true"));
      }
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const progressCalls = () =>
    fetchMock.mock.calls
      .map(([u]) => new URL(String(u), "http://x"))
      .filter((u) => u.pathname === "/api/minecraft/goals/progress");

  async function glance(name: RegExp) {
    render(<MinecraftScreen />);
    return screen.findByRole("button", { name });
  }

  async function expand(who: string) {
    const button = await glance(new RegExp(`${who}'s goals`));
    await waitFor(() => expect(button).not.toHaveTextContent("…"));
    fireEvent.click(button);
    return document.getElementById(`mc-goals-x-${who}`) as HTMLElement;
  }

  it("glances each player's first counted goal, saying the rest are goals", async () => {
    const fox = await glance(/BlockyFox's goals/);
    await waitFor(() => expect(read(fox)).toContain("14 / 23"));
    expect(read(fox)).toMatch(/14 \/ 23\s*iron blocks\s*· \+1 more goal/);
    expect(fox).toHaveAttribute("aria-expanded", "false");
    expect(fox).toHaveAccessibleName(/Get 23 iron blocks, 14 of 23, and 1 more goal/);
    const steve = screen.getByRole("button", { name: /Steve42's goals/ });
    await waitFor(() => expect(read(steve)).toMatch(/18 \/ 16\s*torches\s*· reached/));
    // Pebble_J has no goals, so no glance line at all.
    expect(screen.queryByRole("button", { name: /Pebble_J's goals/ })).toBeNull();
    expect(screen.getByText(/time on this server · goals/)).toBeInTheDocument();
  });

  it("counts once per player with counted goals, from the files as they are", async () => {
    await glance(/BlockyFox's goals/);
    await waitFor(() => expect(progressCalls()).toHaveLength(3));
    const asked = progressCalls().map((u) => u.searchParams.get("xuid"));
    expect(asked.sort()).toEqual(["x-BlockyFox", "x-Mira_P", "x-Steve42"]);
    expect(progressCalls().every((u) => !u.searchParams.has("flush"))).toBe(true);
  });

  it("tapping the glance line opens every goal in place, finished ones folded", async () => {
    const panel = await expand("BlockyFox");
    expect(screen.getByRole("button", { name: /BlockyFox's goals/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    const iron = within(panel).getByRole("button", { name: /Get 23 iron blocks/ });
    expect(read(iron)).toMatch(/9 carried · 5 in chests near Home · as of \d min ago/);
    expect(within(iron).getByRole("img", { name: "14 of 23" })).toBeInTheDocument();
    const beacon = within(panel).getByRole("button", { name: /Beacon at the base/ });
    expect(read(beacon)).toMatch(/No count · made/);
    const fin = within(panel).getByRole("button", { name: /1.done · 1.abandoned/ });
    expect(fin).toHaveAttribute("aria-expanded", "false");
    expect(within(panel).queryByText("Tame a horse")).toBeNull();
    expect(read(panel)).toContain("To change one, BlockyFox asks Dave.");
  });

  it("unfolds the finished goals", async () => {
    const panel = await expand("BlockyFox");
    fireEvent.click(within(panel).getByRole("button", { name: /1.done · 1.abandoned/ }));
    expect(within(panel).getByText("20 obsidian for the portal")).toBeInTheDocument();
    expect(within(panel).getByText("Tame a horse")).toBeInTheDocument();
    expect(within(panel).getByText("done")).toBeInTheDocument();
    expect(within(panel).getByText("abandoned")).toBeInTheDocument();
  });

  it("a goal opens its detail in place: sources, far, file age, caveats, its journal", async () => {
    const panel = await expand("BlockyFox");
    fireEvent.click(within(panel).getByRole("button", { name: /Get 23 iron blocks/ }));
    const text = read(panel);
    expect(text).toContain("14of 23 — 9 to go");
    const sources = within(panel).getByRole("list", { name: "Where the count comes from" });
    const rows = within(sources).getAllByRole("listitem").map(read);
    expect(rows[0]).toMatch(/On BlockyFox.*1 shulker box carried: what's inside isn't counted.*9/);
    expect(rows[1]).toMatch(/Near Home.*chests within 48 blocks · 14 blocks from BlockyFox.*5/);
    expect(rows[2]).toMatch(/Near Mine outpostfar.*620 blocks from BlockyFox.*0/);
    expect(rows[3]).toMatch(/Near BlockyFox.*where they stand.*0/);
    // The far cost is one deep scan per check for all far places, and the app never pays it.
    expect(text).toContain("one per check, for all far places");
    expect(text).toContain("never spends BlockyFox's deep scans");
    expect(text).toMatch(/Chests as of \d min ago \(the world's saved files\)/);
    // "1 shulker box" never breaks between the number and its noun.
    expect(within(sources).getAllByRole("listitem")[0]?.textContent).toContain(
      `1${NB}shulker${NB}box`,
    );
    expect(text).toContain("got 5 iron blocks from the mine");
    expect(text).toContain("BlockyFox, in game");
    expect(text).not.toContain("Goal made");
  });

  it("Count again forces a fresh save of the running world for that player", async () => {
    const panel = await expand("BlockyFox");
    fireEvent.click(within(panel).getByRole("button", { name: /Get 23 iron blocks/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "Count again" }));
    await waitFor(() =>
      expect(progressCalls().some((u) => u.searchParams.get("flush") === "true")).toBe(true),
    );
    const forced = progressCalls().filter((u) => u.searchParams.get("flush") === "true");
    expect(forced.map((u) => u.searchParams.get("xuid"))).toEqual(["x-BlockyFox"]);
    await waitFor(() => expect(read(panel)).toContain("Chests as of just now"));
  });

  it("a reached goal says the player can tell Dave, and promises no nudge", async () => {
    const panel = await expand("Steve42");
    const torch = within(panel).getByRole("button", { name: /Get 16 torches/ });
    expect(read(torch)).toContain("reached");
    expect(read(torch)).toContain("Steve42 can tell Dave it's done");
    expect(read(panel)).not.toMatch(/Dave asks/);
  });

  it("an offline player's pockets count only while they're on", async () => {
    const panel = await expand("Mira_P");
    const oak = within(panel).getByRole("button", { name: /Get 64 oak logs/ });
    expect(read(oak)).toContain(
      "40 in chests near Tree farm · what Mira_P carries counts while they're on",
    );
    fireEvent.click(oak);
    expect(read(panel)).toMatch(/On Mira_Pcounted only while Mira_P is on—/);
  });

  it("a goal in a world that isn't loaded is counted from its saved files", async () => {
    const panel = await expand("Mira_P");
    const glass = within(panel).getByRole("button", { name: /Glass for the sky bridge/ });
    expect(read(glass)).toContain("not loaded In Creative test, which isn't the loaded world");
    expect(read(glass)).toContain("120 in chests near Sky base");
    fireEvent.click(glass);
    expect(read(panel)).toContain("its chests are read from its saved files");
    expect(read(panel)).toContain("counted only while Creative test is loaded");
  });

  it("a server that can't count says so, and keeps the goals", async () => {
    progressDown = true;
    const panel = await expand("BlockyFox");
    const iron = within(panel).getByRole("button", { name: /Get 23 iron blocks/ });
    expect(read(iron)).toContain("Can't count right now — Minecraft sidecar unreachable");
    expect(within(iron).getByRole("img", { name: "Not counted" })).toBeInTheDocument();
    fireEvent.click(iron);
    expect(read(panel)).toContain("is it stopped? Counting here is free");
  });

  it("the row still opens the player's Sheet, now with their goals and latest journal", async () => {
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "BlockyFox" }));
    const sheet = (await screen.findByRole("heading", { name: "BlockyFox", level: 2 })).closest(
      ".sheet",
    ) as HTMLElement;
    expect(within(sheet).getByRole("heading", { name: /Goals/ })).toHaveTextContent("2 open");
    const iron = await within(sheet).findByRole("button", { name: /Get 23 iron blocks/ });
    fireEvent.click(iron);
    expect(read(sheet)).toContain("one per check, for all far places");
    expect(within(sheet).getByRole("heading", { name: /^Journal/, level: 3 })).toBeInTheDocument();
    expect(read(sheet)).toContain("smelted a stack of iron ore");
  });

  it("no goals: the Sheet teaches the sentence that makes one", async () => {
    goals = { world: "slot2", players: [] };
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "BlockyFox" }));
    expect(await screen.findByText("No goals yet")).toBeInTheDocument();
    expect(screen.getByText(/Dave, remind me I need 23 iron blocks/)).toBeInTheDocument();
    expect(screen.getByText("Nothing in the journal yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /goals:/ })).toBeNull();
    expect(progressCalls()).toHaveLength(0);
  });

  it("is look-only: nothing edits a goal", async () => {
    const panel = await expand("BlockyFox");
    fireEvent.click(within(panel).getByRole("button", { name: /Get 23 iron blocks/ }));
    const labels = within(panel)
      .getAllByRole("button")
      .map((b) => read(b));
    expect(labels.some((l) => /mark|edit|delete|abandon it|done\?/i.test(l))).toBe(false);
    expect(within(panel).queryByRole("textbox")).toBeNull();
  });
});

describe("goal wording", () => {
  it("names an item the way people count it", () => {
    expect(itemWords("iron_block", 23)).toBe("iron blocks");
    expect(itemWords("minecraft:torch", 16)).toBe("torches");
    expect(itemWords("glass", 200)).toBe("glass");
    expect(itemWords("oak_leaves", 10)).toBe("oak leaves");
    expect(itemWords("diamond", 1)).toBe("diamond");
    expect(itemWords("jbrain:power_pack", 2)).toBe("power packs");
    expect(itemWords("white_wool", 9)).toBe("white wool");
    expect(itemWords("spruce_sapling", 3)).toBe("spruce saplings");
    expect(itemWords("honey_bottle", 3)).toBe("honey bottles");
    expect(itemWords("berry", 4)).toBe("berries");
  });

  it("says more goals as goals", () => {
    expect(moreText(0)).toBe("");
    expect(moreText(1)).toBe(`+1${NB}more goal`);
    expect(moreText(3)).toBe(`+3${NB}more goals`);
  });

  it("explains a count with nothing in it", () => {
    const c = mcGoalProgress("x-BlockyFox").goals[0] as MinecraftGoalCount;
    const empty = { ...c, have: 0, carried: null, chests: [], here: null, as_of: null };
    expect(whyParts(empty, "BlockyFox", Date.now())).toEqual(["none in chests near their places"]);
  });
});

describe("the dev mock's goals", () => {
  it("serves the gate's fixture, its counts and Count again", async () => {
    const list = (await (await mockFetch("/api/minecraft/goals")).json()) as MinecraftGoals;
    expect(list.players.map((p) => p.gamertag)).toEqual(["BlockyFox", "Mira_P", "Steve42"]);
    const fresh = await (
      await mockFetch("/api/minecraft/goals/progress?xuid=x-BlockyFox&flush=true")
    ).json();
    expect(fresh.goals[0]).toMatchObject({ have: 14, age_s: 0, flush: "flushed" });
  });
});
