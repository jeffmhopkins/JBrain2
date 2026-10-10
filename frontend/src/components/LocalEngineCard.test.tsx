import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EngineState } from "../api/client";
import {
  ENGINE_POLL_IDLE_MS,
  armEngineSwitch,
  hhmmss,
  nextDelay,
  peekEngineSnapshot,
  refreshEngine,
  resetEngineStore,
  setEngineState,
} from "../engineState";
import { LocalEngineSection, engineGlance } from "./LocalEngineCard";
import { engineState, switchStatus } from "./engineFixtures";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** GET serves `current`; POST records its body and answers `postReply`. */
function stubEngine(initial: EngineState) {
  const ctl = {
    current: initial,
    posts: [] as { engine: string; force: boolean }[],
    postReply: (): Response => json(switchStatus({ stage: "draining" }), 202),
  };
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input, init) => {
      const path = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (path === "/api/settings/llm/engine" && method === "GET") return json(ctl.current);
      if (path === "/api/settings/llm/engine" && method === "POST") {
        ctl.posts.push(JSON.parse(String(init?.body)) as { engine: string; force: boolean });
        return ctl.postReply();
      }
      return json({ detail: "not found" }, 404);
    }),
  );
  return ctl;
}

const armedNow = () => peekEngineSnapshot().armed;

// The box the owner actually runs: Flash-Next chosen, and (in these fixtures) a start that fell
// back to Standard — the case the page's one action exists for.
const fellBack = (over: Partial<EngineState> = {}) =>
  engineState({
    desired: "flash-next",
    fallback_reason: "weights incomplete — 71 of 95 GB",
    ...over,
  });

beforeEach(() => resetEngineStore());
afterEach(() => {
  resetEngineStore();
  vi.unstubAllGlobals();
});

describe("LocalEngineSection", () => {
  it("shows the readouts, with an em dash for anything not reported, and no switch", async () => {
    stubEngine(engineState());
    render(<LocalEngineSection />);
    expect(await screen.findByText("Standard serving")).toBeInTheDocument();
    // The owner settled on Flash-Next: there is no engine switch to flip.
    expect(screen.queryByRole("group", { name: "Local engine" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Flash-Next/ })).toBeNull();
    expect(screen.getByText("81.9 GB")).toBeInTheDocument();
    expect(screen.getByText(/host free 26\.3 GB/)).toBeInTheDocument();
    // No tok/s from the api and no switch yet → dashes, never a fabricated number.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("No switch recorded yet.")).toBeInTheDocument();
  });

  it("shows a fallback with the reason, and Retry goes through the confirm to the chosen engine", async () => {
    const ctl = stubEngine(fellBack());
    render(<LocalEngineSection />);
    expect(await screen.findAllByText(/Flash-Next selected · Standard serving/)).not.toHaveLength(
      0,
    );
    expect(screen.getAllByText(/weights incomplete — 71 of 95 GB/).length).toBeGreaterThan(0);
    // Keeping the fallback engine is not offered: the chosen engine is the only way forward.
    expect(screen.queryByRole("button", { name: /Keep Standard|Switch to Standard/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(within(confirm).getByText("Start Flash-Next?")).toBeInTheDocument();
    expect(confirm).toHaveTextContent(/Local AI pauses/);
    expect(confirm).toHaveTextContent(/rolls back to Standard on its own/);
    expect(ctl.posts).toEqual([]);

    ctl.current = fellBack({ switching: true, switch: switchStatus({ stage: "draining" }) });
    fireEvent.click(within(confirm).getByRole("button", { name: "Start Flash-Next" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "flash-next", force: false }]));
    expect(await screen.findByText(/Step 1 of 5 · Drain local calls/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Confirm engine switch" })).toBeNull();
  });

  it("Cancel disarms without sending anything", async () => {
    const ctl = stubEngine(fellBack());
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("region", { name: "Confirm engine switch" })).toBeNull();
    expect(ctl.posts).toEqual([]);
  });

  it("shows phased progress with timestamps during a switch", async () => {
    const sw = switchStatus({
      stage: "smoke",
      model: "qwen3.8-flash-next",
      stages: [
        { stage: "draining", at: "2026-10-02T09:41:05Z" },
        { stage: "stopping", at: "2026-10-02T09:41:14Z" },
        { stage: "starting", at: "2026-10-02T09:41:35Z" },
        { stage: "loading", at: "2026-10-02T09:41:40Z" },
        { stage: "smoke", at: "2026-10-02T09:42:30Z" },
      ],
      smoke: [{ probe: "text", ok: true, detail: "ok" }],
      notes: ["drain timed out after 60 s with gpt-oss-120b still busy; proceeding"],
    });
    stubEngine(engineState({ switching: true, switch: sw }));
    render(<LocalEngineSection />);
    expect(await screen.findByText(/Step 5 of 5 · Smoke test/)).toBeInTheDocument();
    expect(screen.getByText("Load qwen3.8-flash-next")).toBeInTheDocument();
    const stamp = hhmmss("2026-10-02T09:41:14Z") ?? "";
    expect(screen.getAllByText(stamp).length).toBeGreaterThan(0);
    expect(screen.getByText(/drain timed out after 60 s/)).toBeInTheDocument();
  });

  it("after a rollback shows the reason, Try again and Dismiss", async () => {
    const sw = switchStatus({
      stage: "rolled_back",
      reason: "smoke test failed: image probe: empty reply; standard was put back",
      ended_at: "2026-10-02T09:44:00Z",
      smoke: [
        { probe: "text", ok: true, detail: "ok" },
        { probe: "image", ok: false, detail: "empty reply" },
      ],
    });
    stubEngine(fellBack({ fallback_reason: null, switch: sw }));
    render(<LocalEngineSection />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/rolled back/);
    expect(alert).toHaveTextContent(/Reason: smoke test failed: image probe/);
    expect(screen.getByText("failed")).toBeInTheDocument(); // last smoke
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Start Flash-Next?")).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByText(/Reason: smoke test failed/)).toBeNull());
  });

  it("offers no Try again for a failed switch away from the chosen engine", async () => {
    stubEngine(
      engineState({
        switch: switchStatus({ stage: "rolled_back", target: "flash-next", reason: "boom" }),
      }),
    );
    render(<LocalEngineSection />);
    const alert = await screen.findByRole("alert");
    expect(within(alert).queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("names a failed (not rolled back) switch as such", async () => {
    stubEngine(
      engineState({
        switch: switchStatus({
          stage: "failed",
          reason: "flash-next could not be confirmed stopped",
        }),
      }),
    );
    render(<LocalEngineSection />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not be rolled back cleanly/);
    expect(alert).toHaveTextContent(/could not be confirmed stopped/);
  });

  it("holds Retry while a one-shot runs", async () => {
    stubEngine(fellBack({ oneshot: "perplexity", perplexity_running: true }));
    render(<LocalEngineSection />);
    expect(await screen.findByRole("button", { name: "Retry now" })).toBeDisabled();
  });

  it("behind a guard, force needs a separate acknowledgement", async () => {
    const ctl = stubEngine(fellBack({ guard: "inside the nightly window (02:00–05:00)" }));
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(confirm).toHaveTextContent(/Not now: inside the nightly window/);
    const anyway = within(confirm).getByRole("button", { name: "Start Flash-Next anyway" });
    expect(anyway).toBeDisabled();
    fireEvent.click(anyway);
    expect(ctl.posts).toEqual([]);
    fireEvent.click(within(confirm).getByRole("checkbox", { name: /I understand/ }));
    expect(anyway).toBeEnabled();
    fireEvent.click(anyway);
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "flash-next", force: true }]));
  });

  it("a guard appearing under an armed confirm disables the button, never sends force", async () => {
    const ctl = stubEngine(fellBack());
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    const button = within(screen.getByRole("region", { name: "Confirm engine switch" })).getByRole(
      "button",
      { name: "Start Flash-Next" },
    );
    // The next poll brings a guard while the finger is on its way to the button.
    act(() => setEngineState(fellBack({ guard: "a workflow run is executing" })));
    // Same element, now disabled and relabelled — a tap lands on nothing.
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Start Flash-Next anyway");
    fireEvent.click(button);
    expect(ctl.posts).toEqual([]);
    // An acknowledgement belongs to one guard text: a new reason needs a new tick.
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand/ }));
    expect(button).toBeEnabled();
    act(() => setEngineState(fellBack({ guard: "inside the nightly window" })));
    expect(button).toBeDisabled();
  });

  it("drops an armed confirm when the serving engine changes under it", async () => {
    stubEngine(fellBack());
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    expect(screen.getByRole("region", { name: "Confirm engine switch" })).toBeInTheDocument();
    act(() =>
      setEngineState(
        engineState({ desired: "flash-next", effective: "flash-next", running: ["flash-next"] }),
      ),
    );
    expect(screen.queryByRole("region", { name: "Confirm engine switch" })).toBeNull();
  });

  it("sends one POST however fast Start is tapped", async () => {
    const ctl = stubEngine(fellBack());
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    const button = screen.getByRole("button", { name: "Start Flash-Next" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(ctl.posts).toHaveLength(1));
  });

  it("makes only the step headline live", async () => {
    stubEngine(engineState({ switching: true, switch: switchStatus({ stage: "stopping" }) }));
    const { container } = render(<LocalEngineSection />);
    const headline = await screen.findByText(/Step 2 of 5 · Stop Standard/);
    expect(headline.tagName).toBe("OUTPUT");
    expect(container.querySelector("[aria-live]")).toBeNull();
  });

  it("offers Cancel only while draining, and stops offering it if the server has no route", async () => {
    const ctl = stubEngine(
      engineState({ switching: true, switch: switchStatus({ stage: "draining" }) }),
    );
    const calls: string[] = [];
    const base = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        if (path.includes("/cancel") || path.endsWith("/engine/switch")) {
          calls.push(`${init?.method} ${path}`);
          return json({ detail: "Not Found" }, 404);
        }
        return base(input, init);
      }),
    );
    render(<LocalEngineSection />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Cancel — nothing has stopped yet/ }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't cancel a switch/);
    expect(calls).toEqual(["POST /api/settings/llm/engine/cancel"]);
    expect(screen.queryByRole("button", { name: /Cancel — nothing has stopped yet/ })).toBeNull();
    // Past draining there is never a cancel.
    act(() =>
      setEngineState(engineState({ switching: true, switch: switchStatus({ stage: "starting" }) })),
    );
    expect(screen.queryByRole("button", { name: /nothing has stopped yet/ })).toBeNull();
    expect(ctl.posts).toEqual([]);
  });

  it("cancels a draining switch and ends neutral", async () => {
    const ctl = stubEngine(
      engineState({ switching: true, switch: switchStatus({ id: "sw-c", stage: "draining" }) }),
    );
    const base = globalThis.fetch;
    const cancelled = switchStatus({
      id: "sw-c",
      stage: "cancelled",
      reason: "cancelled while draining — nothing was stopped; standard serves",
      ended_at: "2026-10-02T09:41:20Z",
    });
    const cancel = vi.fn(async () => {
      ctl.current = engineState({ switching: false, switch: cancelled });
      return json(cancelled, 202);
    });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) =>
        String(input).endsWith("/engine/cancel") ? cancel() : base(input, init),
      ),
    );
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: /nothing has stopped yet/ }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(/Switch cancelled — Standard still serving/),
    ).toBeInTheDocument();
    // Neutral: no alert, no progress.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Step \d of 5/)).toBeNull();
    // The store's beat is back to idle.
    expect(nextDelay()).toBe(ENGINE_POLL_IDLE_MS);
  });

  it("shows a cancel refused past draining as the server's error", async () => {
    stubEngine(engineState({ switching: true, switch: switchStatus({ stage: "draining" }) }));
    const base = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) =>
        String(input).endsWith("/engine/cancel")
          ? json({ detail: "only a switch that is still draining can be cancelled" }, 409)
          : base(input, init),
      ),
    );
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: /nothing has stopped yet/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/still draining can be cancelled/);
    // A 409 is not "unsupported": the button stays on offer while it is draining.
    expect(screen.getByRole("button", { name: /nothing has stopped yet/ })).toBeInTheDocument();
  });

  const noEngine = (over: Partial<EngineState> = {}) =>
    engineState({
      desired: "flash-next",
      running: [],
      consistent: false,
      services: {
        standard: { service: "local-llm", state: "exited" },
        "flash-next": { service: "flash-next", state: "exited" },
      },
      switch: switchStatus({
        stage: "failed",
        no_engine_up: true,
        reason: "flash-next did not report running; standard could NOT be put back",
      }),
      ...over,
    });

  it("says prominently when no local engine is up, and offers to start the chosen one", async () => {
    const ctl = stubEngine(noEngine());
    render(<LocalEngineSection />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/No local engine is up — flash-next did not report running/);
    expect(within(alert).queryByRole("button", { name: "Start Standard" })).toBeNull();
    fireEvent.click(within(alert).getByRole("button", { name: "Start Flash-Next" }));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(within(confirm).getByText("Start Flash-Next?")).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "Start Flash-Next" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "flash-next", force: false }]));
  });

  it("offers no Start when the chosen engine cannot start", async () => {
    stubEngine(noEngine({ installed: { standard: true, "flash-next": false } }));
    render(<LocalEngineSection />);
    const alert = await screen.findByRole("alert");
    expect(within(alert).queryByRole("button")).toBeNull();
  });

  it("shows a decode rate only when the server reports one", async () => {
    stubEngine(engineState({ decode_tps: 38.24 }));
    render(<LocalEngineSection />);
    expect(await screen.findByText("38.2 tok/s")).toBeInTheDocument();
  });

  it("says when it can't reach the engine, keeping the last reading", async () => {
    const ctl = stubEngine(engineState());
    render(<LocalEngineSection />);
    await screen.findByText("Standard serving");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "supervisor unreachable" }, 502)),
    );
    await act(async () => {
      await refreshEngine();
    });
    expect(screen.getByText(/Can't reach the engine · last read \d\d:\d\d/)).toBeInTheDocument();
    expect(screen.getByText("Standard serving")).toBeInTheDocument();
    expect(ctl.posts).toEqual([]);
  });

  it("surfaces a refused POST's detail", async () => {
    const ctl = stubEngine(fellBack());
    ctl.postReply = () =>
      json(
        { detail: "a supervisor one-shot (update) is running; switch once it has finished" },
        409,
      );
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    fireEvent.click(screen.getByRole("button", { name: "Start Flash-Next" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/one-shot \(update\) is running/);
  });

  it("announces a switch it watched finish", async () => {
    const ctl = stubEngine(fellBack());
    ctl.postReply = () => json(switchStatus({ id: "sw-9", stage: "draining" }), 202);
    render(<LocalEngineSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry now" }));
    ctl.current = engineState({
      desired: "flash-next",
      effective: "flash-next",
      running: ["flash-next"],
      switch: switchStatus({
        id: "sw-9",
        stage: "done",
        ended_at: "2026-10-02T09:43:10Z",
        smoke: [{ probe: "text", ok: true, detail: "ok" }],
      }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Start Flash-Next" }));
    expect(await screen.findByText(/Flash-Next is serving/)).toBeInTheDocument();
    expect(screen.getByText(/smoke test passed/)).toBeInTheDocument();
  });

  it("keeps a confirm armed from elsewhere through a StrictMode remount, and drops it on leaving", async () => {
    stubEngine(fellBack());
    armEngineSwitch("flash-next");
    const { unmount } = render(
      <StrictMode>
        <LocalEngineSection />
      </StrictMode>,
    );
    expect(await screen.findByText("Start Flash-Next?")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.getByText("Start Flash-Next?")).toBeInTheDocument();
    // Leaving the page drops it.
    unmount();
    await waitFor(() => expect(armedNow()).toBeNull());
  });

  it("says unavailable when the engine can't be read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "supervisor unreachable: boom" }, 502)),
    );
    render(<LocalEngineSection />);
    expect(await screen.findByText(/supervisor unreachable: boom/)).toBeInTheDocument();
  });
});

describe("engineGlance", () => {
  const none = new Set<string>();

  it("names the serving engine when all is well", () => {
    expect(
      engineGlance(
        engineState({ desired: "flash-next", effective: "flash-next", running: ["flash-next"] }),
        null,
        none,
      ),
    ).toEqual({ word: "Flash-Next", tone: "" });
  });

  it("flags a switch, a fallback, a failure and no engine", () => {
    expect(
      engineGlance(engineState({ switching: true, switch: switchStatus() }), null, none),
    ).toEqual({ word: "switching", tone: "warn" });
    expect(engineGlance(fellBack(), null, none)).toEqual({
      word: "Standard (fallback)",
      tone: "warn",
    });
    const failed = engineState({ switch: switchStatus({ id: "sw-f", stage: "rolled_back" }) });
    expect(engineGlance(failed, null, none)).toEqual({ word: "start failed", tone: "bad" });
    // Dismissed is dismissed on the tile too.
    expect(engineGlance(failed, null, new Set(["sw-f"]))).toEqual({ word: "Standard", tone: "" });
    expect(
      engineGlance(
        engineState({ running: [], switch: switchStatus({ stage: "failed", no_engine_up: true }) }),
        null,
        none,
      ),
    ).toEqual({ word: "no engine up", tone: "bad" });
    expect(engineGlance(engineState({ consistent: false }), null, none)).toEqual({
      word: "Standard · not up",
      tone: "warn",
    });
  });

  it("says checking until the first read, and unavailable when reads fail", () => {
    expect(engineGlance(null, null, none)).toEqual({ word: "checking…", tone: "" });
    expect(engineGlance(null, "boom", none)).toEqual({ word: "unavailable", tone: "" });
  });
});
