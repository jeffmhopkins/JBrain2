import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EngineState } from "../api/client";
import {
  armEngineSwitch,
  hhmmss,
  peekEngineSnapshot,
  resetEngineStore,
  setEngineNavigator,
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

  it("shows desired ≠ effective as a fallback with Retry now / Keep", async () => {
    const ctl = stubEngine(engineState({ desired: "flash-next" }));
    render(<LocalEngineCard />);
    expect(await screen.findAllByText(/Flash-Next selected · Standard serving/)).not.toHaveLength(
      0,
    );
    expect(screen.getByRole("button", { name: /Local engine/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    expect(screen.getByText("Switch to Flash-Next?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep Standard" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "standard", force: false }]));
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

  it("offers force only when the guard blocks, as an explicit Switch anyway", async () => {
    const ctl = stubEngine(engineState({ guard: "inside the nightly window (02:00–05:00)" }));
    render(<LocalEngineCard />);
    await openCard();
    await screen.findByText("Standard serving");
    fireEvent.click(seg("Flash-Next"));
    const confirm = screen.getByRole("region", { name: "Confirm engine switch" });
    expect(confirm).toHaveTextContent(/Not now: inside the nightly window/);
    expect(within(confirm).queryByRole("button", { name: "Switch" })).toBeNull();
    fireEvent.click(within(confirm).getByRole("button", { name: "Switch anyway" }));
    await waitFor(() => expect(ctl.posts).toEqual([{ engine: "flash-next", force: true }]));
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
