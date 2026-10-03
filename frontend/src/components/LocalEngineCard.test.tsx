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
  setEngineNavigator,
  setEngineState,
} from "../engineState";
import { LocalEngineCard } from "./LocalEngineCard";
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

async function openCard() {
  const head = await screen.findByRole("button", { name: /Local engine/ });
  if (head.getAttribute("aria-expanded") !== "true") fireEvent.click(head);
  return head;
}

function seg(name: string): HTMLElement {
  const group = screen.getByRole("group", { name: "Local engine" });
  return within(group).getByRole("button", { name: new RegExp(`^${name}`) });
}

beforeEach(() => resetEngineStore());
afterEach(() => {
  resetEngineStore();
  vi.unstubAllGlobals();
});

describe("LocalEngineCard", () => {
  it("is collapsed when idle, with the serving engine in the summary", async () => {
    stubEngine(engineState());
    render(<LocalEngineCard />);
    const head = await screen.findByRole("button", { name: /Local engine/ });
    expect(head).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(head).toHaveTextContent("Standard"));
  });

  it("shows the readouts, with an em dash for anything not reported", async () => {
    stubEngine(engineState());
    render(<LocalEngineCard />);
    await openCard();
    expect(await screen.findByText("Standard serving")).toBeInTheDocument();
    expect(seg("Standard")).toHaveAttribute("aria-pressed", "true");
    expect(seg("Flash-Next")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("81.9 GB")).toBeInTheDocument();
    expect(screen.getByText(/host free 26\.3 GB/)).toBeInTheDocument();
    // No tok/s from the api and no switch yet → dashes, never a fabricated number.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("No switch recorded yet.")).toBeInTheDocument();
  });

  it("arms an inline confirm that states the consequence, then POSTs on Switch", async () => {
    const ctl = stubEngine(engineState());
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(within(confirm).getByText("Switch to Flash-Next?")).toBeInTheDocument();
    expect(confirm).toHaveTextContent(/Local AI pauses/);
    expect(confirm).toHaveTextContent(/rolls back to Standard on its own/);
    expect(confirm).toHaveTextContent(/Every local per-task pick runs on Flash-Next/);
    // No nightly guard → no force option.
    expect(within(confirm).queryByRole("button", { name: "Switch anyway" })).toBeNull();

    ctl.current = engineState({ switching: true, switch: switchStatus({ stage: "draining" }) });
    fireEvent.click(within(confirm).getByRole("button", { name: "Switch" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "flash-next", force: false }]));
    expect(await screen.findByText(/Step 1 of 5 · Drain local calls/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Confirm engine switch" })).toBeNull();
  });

  it("Cancel disarms without sending anything and keeps the card open", async () => {
    const ctl = stubEngine(engineState());
    render(<LocalEngineCard />);
    const head = await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("region", { name: "Confirm engine switch" })).toBeNull();
    expect(head).toHaveAttribute("aria-expanded", "true");
    expect(ctl.posts).toEqual([]);
  });

  it("opens itself during a switch and shows phased progress with timestamps", async () => {
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
    render(<LocalEngineCard />);
    expect(await screen.findByText(/Step 5 of 5 · Smoke test/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Local engine/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Load qwen3.8-flash-next")).toBeInTheDocument();
    const stamp = hhmmss("2026-10-02T09:41:14Z") ?? "";
    expect(screen.getAllByText(stamp).length).toBeGreaterThan(0);
    // Every segment is locked while it runs.
    expect(seg("Standard")).toBeDisabled();
    expect(seg("Flash-Next")).toBeDisabled();
    // The notes tail is shown.
    expect(screen.getByText(/drain timed out after 60 s/)).toBeInTheDocument();
  });

  it("after a rollback opens itself with the reason, Try again and Dismiss", async () => {
    const sw = switchStatus({
      stage: "rolled_back",
      reason: "smoke test failed: image probe: empty reply; standard was put back",
      ended_at: "2026-10-02T09:44:00Z",
      smoke: [
        { probe: "text", ok: true, detail: "ok" },
        { probe: "image", ok: false, detail: "empty reply" },
      ],
    });
    stubEngine(engineState({ switch: sw }));
    render(<LocalEngineCard />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/rolled back/);
    expect(alert).toHaveTextContent(/Reason: smoke test failed: image probe/);
    expect(screen.getByText("failed")).toBeInTheDocument(); // last smoke
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(screen.getByRole("region", { name: "Confirm engine switch" })).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByText(/Reason: smoke test failed/)).toBeNull());
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
    render(<LocalEngineCard />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not be rolled back cleanly/);
    expect(alert).toHaveTextContent(/could not be confirmed stopped/);
  });

  it("disables Flash-Next when it isn't installed and links to On-box models", async () => {
    stubEngine(engineState({ installed: { standard: true, "flash-next": false } }));
    const models = vi.fn();
    setEngineNavigator({ ops: vi.fn(), models });
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText(/Flash-Next isn't installed/);
    expect(seg("Flash-Next")).toBeDisabled();
    expect(seg("Flash-Next")).toHaveTextContent("not installed");
    fireEvent.click(screen.getByRole("button", { name: "Install in On-box models" }));
    expect(models).toHaveBeenCalledTimes(1);
  });

  it("disables switching while a one-shot runs", async () => {
    stubEngine(engineState({ oneshot: "perplexity", perplexity_running: true }));
    render(<LocalEngineCard />);
    await openCard();
    expect(await screen.findByText(/perplexity test \(a debug one-shot\)/)).toBeInTheDocument();
    expect(seg("Flash-Next")).toBeDisabled();
  });

  it("shows desired ≠ effective as a fallback; Keep goes through the confirm", async () => {
    const ctl = stubEngine(
      engineState({ desired: "flash-next", fallback_reason: "weights incomplete — 71 of 95 GB" }),
    );
    render(<LocalEngineCard />);
    expect(await screen.findAllByText(/Flash-Next selected · Standard serving/)).not.toHaveLength(
      0,
    );
    expect(screen.getByRole("button", { name: /Local engine/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    // The server's reason, when it gives one.
    expect(screen.getAllByText(/weights incomplete — 71 of 95 GB/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    expect(screen.getByText("Switch to Flash-Next?")).toBeInTheDocument();

    // Keep never POSTs from the notice: it arms a confirm that says nothing stops.
    fireEvent.click(screen.getByRole("button", { name: "Keep Standard" }));
    expect(ctl.posts).toEqual([]);
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(within(confirm).getByText("Keep Standard?")).toBeInTheDocument();
    expect(confirm).toHaveTextContent(/nothing stops or reloads/);
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep Standard" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "standard", force: false }]));
  });

  it("on an inconsistent box, Keep becomes a confirmed switch with the consequence", async () => {
    const ctl = stubEngine(
      engineState({
        desired: "flash-next",
        running: ["standard", "flash-next"],
        consistent: false,
      }),
    );
    render(<LocalEngineCard />);
    await screen.findAllByText(/Flash-Next selected · Standard serving/);
    expect(screen.queryByRole("button", { name: "Keep Standard" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Switch to Standard" }));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(within(confirm).getByText("Switch to Standard?")).toBeInTheDocument();
    expect(confirm).toHaveTextContent(/Local AI pauses/);
    expect(confirm).toHaveTextContent(/Flash-Next stops/);
    fireEvent.click(within(confirm).getByRole("button", { name: "Switch" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "standard", force: false }]));
  });

  it("behind a guard, force needs a separate acknowledgement", async () => {
    const ctl = stubEngine(engineState({ guard: "inside the nightly window (02:00–05:00)" }));
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(confirm).toHaveTextContent(/Not now: inside the nightly window/);
    const anyway = within(confirm).getByRole("button", { name: "Switch anyway" });
    expect(anyway).toBeDisabled();
    fireEvent.click(anyway);
    expect(ctl.posts).toEqual([]);
    fireEvent.click(within(confirm).getByRole("checkbox", { name: /I understand/ }));
    expect(anyway).toBeEnabled();
    fireEvent.click(anyway);
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "flash-next", force: true }]));
  });

  it("a guard appearing under an armed confirm disables the button, never sends force", async () => {
    const ctl = stubEngine(engineState());
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    const button = within(screen.getByRole("region", { name: "Confirm engine switch" })).getByRole(
      "button",
      { name: "Switch" },
    );
    // The next poll brings a guard while the finger is on its way to the button.
    act(() => setEngineState(engineState({ guard: "a workflow run is executing" })));
    // Same element, now disabled and relabelled — a tap lands on nothing.
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Switch anyway");
    fireEvent.click(button);
    expect(ctl.posts).toEqual([]);
    // An acknowledgement belongs to one guard text: a new reason needs a new tick.
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand/ }));
    expect(button).toBeEnabled();
    act(() => setEngineState(engineState({ guard: "inside the nightly window" })));
    expect(button).toBeDisabled();
  });

  it("drops an armed confirm when the serving engine changes under it", async () => {
    stubEngine(engineState());
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    expect(screen.getByRole("region", { name: "Confirm engine switch" })).toBeInTheDocument();
    act(() =>
      setEngineState(
        engineState({ desired: "flash-next", effective: "flash-next", running: ["flash-next"] }),
      ),
    );
    expect(screen.queryByRole("region", { name: "Confirm engine switch" })).toBeNull();
  });

  it("sends one POST however fast the Switch is tapped", async () => {
    const ctl = stubEngine(engineState());
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    const button = screen.getByRole("button", { name: "Switch" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(ctl.posts).toHaveLength(1));
  });

  it("makes only the step headline live", async () => {
    stubEngine(engineState({ switching: true, switch: switchStatus({ stage: "stopping" }) }));
    const { container } = render(<LocalEngineCard />);
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
    render(<LocalEngineCard />);
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

  it("cancels a draining switch, ends neutral, and can be re-armed", async () => {
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
    render(<LocalEngineCard />);
    fireEvent.click(await screen.findByRole("button", { name: /nothing has stopped yet/ }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(/Switch cancelled — Standard still serving/),
    ).toBeInTheDocument();
    // Neutral: no alert, no progress, and the control is live again.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Step \d of 5/)).toBeNull();
    expect(seg("Flash-Next")).toBeEnabled();
    fireEvent.click(seg("Flash-Next"));
    expect(screen.getByRole("region", { name: "Confirm engine switch" })).toBeInTheDocument();
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
    render(<LocalEngineCard />);
    fireEvent.click(await screen.findByRole("button", { name: /nothing has stopped yet/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/still draining can be cancelled/);
    // A 409 is not "unsupported": the button stays on offer while it is draining.
    expect(screen.getByRole("button", { name: /nothing has stopped yet/ })).toBeInTheDocument();
  });

  it("says prominently when no local engine is up, with a way to start one", async () => {
    const ctl = stubEngine(
      engineState({
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
      }),
    );
    render(<LocalEngineCard />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/No local engine is up — flash-next did not report running/);
    expect(screen.getByRole("button", { name: /Local engine/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    // Starting the recorded (effective) engine is a real, confirmed switch here.
    fireEvent.click(within(alert).getByRole("button", { name: "Start Standard" }));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(within(confirm).getByText("Switch to Standard?")).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "Switch" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "standard", force: false }]));
  });

  it("shows a decode rate only when the server reports one", async () => {
    stubEngine(engineState({ decode_tps: 38.24 }));
    render(<LocalEngineCard />);
    await openCard();
    expect(await screen.findByText("38.2 tok/s")).toBeInTheDocument();
  });

  it("says when it can't reach the engine, keeping the last reading", async () => {
    const ctl = stubEngine(engineState());
    render(<LocalEngineCard />);
    await openCard();
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
    const ctl = stubEngine(engineState());
    ctl.postReply = () =>
      json(
        { detail: "a supervisor one-shot (update) is running; switch once it has finished" },
        409,
      );
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    fireEvent.click(screen.getByRole("button", { name: "Switch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/one-shot \(update\) is running/);
  });

  it("announces a switch it watched finish", async () => {
    const ctl = stubEngine(engineState());
    ctl.postReply = () => json(switchStatus({ id: "sw-9", stage: "draining" }), 202);
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
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
    fireEvent.click(screen.getByRole("button", { name: "Switch" }));
    expect(await screen.findByText(/Flash-Next is serving/)).toBeInTheDocument();
    expect(screen.getByText(/smoke test passed/)).toBeInTheDocument();
  });

  it("opens itself with the confirm armed when the banner asks to switch back", async () => {
    stubEngine(
      engineState({ desired: "flash-next", effective: "flash-next", running: ["flash-next"] }),
    );
    armEngineSwitch("standard");
    // StrictMode unmounts and remounts effects once; the arm must survive that.
    const { unmount } = render(
      <StrictMode>
        <LocalEngineCard />
      </StrictMode>,
    );
    expect(await screen.findByText("Switch to Standard?")).toBeInTheDocument();
    expect(screen.getByText(/per-task picks go back to their own models/)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.getByText("Switch to Standard?")).toBeInTheDocument();
    // Leaving Ops drops it.
    unmount();
    await waitFor(() => expect(armedNow()).toBeNull());
  });

  it("says unavailable when the engine can't be read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "supervisor unreachable: boom" }, 502)),
    );
    render(<LocalEngineCard />);
    const head = await screen.findByRole("button", { name: /Local engine/ });
    await waitFor(() => expect(head).toHaveTextContent("unavailable"));
    fireEvent.click(head);
    expect(screen.getByText(/supervisor unreachable: boom/)).toBeInTheDocument();
  });
});
