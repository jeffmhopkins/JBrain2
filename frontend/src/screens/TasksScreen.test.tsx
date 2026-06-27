import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { type Task, type TaskRun, api } from "../api/client";
import { TasksScreen } from "./TasksScreen";

const FUTURE = new Date(Date.now() + 3600_000).toISOString();

// The latest run the server embeds on the task — drives the always-visible band.
const LATEST_T1: TaskRun = {
  id: "lr1",
  task_id: "t1",
  session_id: "sess-latest",
  status: "done",
  trigger: "schedule",
  summary: "Daily Action Brief — 3 items need a reply",
  error: null,
  step_count: 7,
  cost_tokens: 200,
  started_at: new Date(Date.now() - 300_000).toISOString(),
  ended_at: new Date(Date.now() - 240_000).toISOString(),
};

const SCHEDULED: Task = {
  id: "t1",
  name: "Morning brief",
  prompt: "Give me the news.",
  agent: "jerv",
  domain_scopes: [],
  schedule_kind: "repeat",
  schedule_freq: "weekdays",
  schedule_days: [],
  schedule_time: "07:00",
  run_at: null,
  timezone: "UTC",
  enabled: true,
  notify_push: true,
  home_card: true,
  next_run_at: FUTURE,
  last_run_at: LATEST_T1.started_at,
  latest_run: LATEST_T1,
};

const MANUAL: Task = {
  ...SCHEDULED,
  id: "t2",
  name: "Ad hoc digest",
  schedule_kind: "on_demand",
  schedule_freq: null,
  next_run_at: null,
  last_run_at: null,
  latest_run: null, // never run → inert placeholder band
};

// An older run, distinct from the embedded latest, returned when a card expands.
const RUN: TaskRun = {
  id: "r1",
  task_id: "t1",
  session_id: "s1",
  status: "done",
  trigger: "schedule",
  summary: "Five bullets on overnight news.",
  error: null,
  step_count: 4,
  cost_tokens: 100,
  started_at: new Date(Date.now() - 7200_000).toISOString(),
  ended_at: new Date(Date.now() - 7100_000).toISOString(),
};

function mount(tasks: Task[] = [SCHEDULED, MANUAL]) {
  vi.spyOn(api, "tasks").mockResolvedValue(tasks);
  vi.spyOn(api, "taskRuns").mockResolvedValue([RUN]);
  const onClose = vi.fn();
  const onOpenSession = vi.fn();
  render(<TasksScreen onClose={onClose} onOpenSession={onOpenSession} />);
  return { onClose, onOpenSession };
}

describe("TasksScreen", () => {
  beforeEach(() => localStorage.clear()); // per-task "viewed" markers are device-local

  it("renders the Scheduled and On demand groups", async () => {
    mount();
    expect(await screen.findByText("Morning brief")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Scheduled" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "On demand" })).toBeInTheDocument();
    expect(screen.getByText("Ad hoc digest")).toBeInTheDocument();
  });

  it("toggles a task through the optimistic enable endpoint", async () => {
    const setEnabled = vi.spyOn(api, "setTaskEnabled").mockResolvedValue({
      ...SCHEDULED,
      enabled: false,
    });
    mount();
    const sw = await screen.findByLabelText("Pause Morning brief");
    fireEvent.click(sw);
    await waitFor(() => expect(setEnabled).toHaveBeenCalledWith("t1", false));
  });

  it("loads recent runs when a card is expanded", async () => {
    mount();
    fireEvent.click(await screen.findByText("Morning brief"));
    expect(await screen.findByText("Five bullets on overnight news.")).toBeInTheDocument();
    expect(api.taskRuns).toHaveBeenCalledWith("t1");
  });

  it("shows the latest result on the collapsed card and opens its session in one tap", async () => {
    const { onOpenSession } = mount();
    // The band summary is visible without expanding the card.
    const band = await screen.findByRole("button", { name: /Open latest session/ });
    expect(screen.getByText("Daily Action Brief — 3 items need a reply")).toBeInTheDocument();
    fireEvent.click(band);
    expect(onOpenSession).toHaveBeenCalledWith("sess-latest", "jerv");
  });

  it("hides the band once its latest session is opened (no unviewed result left)", async () => {
    mount();
    const band = await screen.findByRole("button", { name: /Open latest session/ });
    expect(screen.getByText("NEW")).toBeInTheDocument();
    fireEvent.click(band);
    // Nothing new to surface anymore: the band disappears, leaving just the header.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Open latest session/ })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("NEW")).not.toBeInTheDocument();
    expect(screen.queryByText("Daily Action Brief — 3 items need a reply")).not.toBeInTheDocument();
  });

  it("persists the viewed marker so the band stays hidden after a remount", async () => {
    // The user's actual flow: opening a session unmounts this screen (the handoff
    // drops the Tasks card to reveal the chat), then reopening Tasks must keep the
    // band hidden. The marker has to reach localStorage — not just component state —
    // for that to survive the remount. A wrapper that unmounts TasksScreen on open
    // mirrors the live handoff so this guards the persistence end-to-end.
    vi.spyOn(api, "tasks").mockResolvedValue([SCHEDULED, MANUAL]);
    vi.spyOn(api, "taskRuns").mockResolvedValue([RUN]);
    function Harness() {
      const [open, setOpen] = useState(true);
      return open ? (
        <TasksScreen onClose={vi.fn()} onOpenSession={() => setOpen(false)} />
      ) : (
        <span>chat</span>
      );
    }
    const { unmount } = render(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: /Open latest session/ }));
    await screen.findByText("chat"); // the screen unmounted, just like the live handoff
    unmount();

    render(<TasksScreen onClose={vi.fn()} onOpenSession={vi.fn()} />);
    await screen.findByText("Morning brief"); // the card is back…
    // …but its viewed band is not — the marker survived in localStorage.
    expect(screen.queryByRole("button", { name: /Open latest session/ })).not.toBeInTheDocument();
    expect(screen.queryByText("NEW")).not.toBeInTheDocument();
  });

  it("shows an inert placeholder band for a task that has never run", async () => {
    mount();
    await screen.findByText("Ad hoc digest");
    expect(screen.getByText("No runs yet")).toBeInTheDocument();
  });

  it("runs a task now", async () => {
    const runTask = vi.spyOn(api, "runTask").mockResolvedValue(RUN);
    mount();
    fireEvent.click(await screen.findByText("Morning brief")); // expand to reveal Run now
    fireEvent.click(await screen.findByText("Run now"));
    await waitFor(() => expect(runTask).toHaveBeenCalledWith("t1"));
  });

  it("creates a task from the editor", async () => {
    const createTask = vi.spyOn(api, "createTask").mockResolvedValue(SCHEDULED);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "New task" }));
    const prompt = await screen.findByPlaceholderText("Tell the agent what to do on each run…");
    fireEvent.change(prompt, { target: { value: "Summarize my week." } });
    fireEvent.click(screen.getByText("Save task"));
    await waitFor(() => expect(createTask).toHaveBeenCalled());
    expect(createTask.mock.calls[0]?.[0].prompt).toBe("Summarize my week.");
  });

  it("creates an Archivist task (Gmail organizer, no scope dial)", async () => {
    const createTask = vi
      .spyOn(api, "createTask")
      .mockResolvedValue({ ...SCHEDULED, agent: "archivist" });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "New task" }));
    const prompt = await screen.findByPlaceholderText("Tell the agent what to do on each run…");
    fireEvent.change(prompt, { target: { value: "Label everything from chase.com." } });
    // The Archivist is offered in the agent picker; selecting it starts with no scopes.
    fireEvent.click(screen.getByRole("button", { name: /Archivist/ }));
    fireEvent.click(screen.getByText("Save task"));
    await waitFor(() => expect(createTask).toHaveBeenCalled());
    expect(createTask.mock.calls[0]?.[0].agent).toBe("archivist");
    expect(createTask.mock.calls[0]?.[0].domain_scopes).toEqual([]); // a non-KB persona reads nothing
  });

  it("opens the session an older run produced from the expanded history", async () => {
    const { onOpenSession } = mount();
    fireEvent.click(await screen.findByText("Morning brief")); // expand to reveal runs
    fireEvent.click(await screen.findByRole("button", { name: /^Open session/ }));
    expect(onOpenSession).toHaveBeenCalledWith("s1", "jerv");
  });

  it("returns to the launcher via the back control", async () => {
    const { onClose } = mount();
    await screen.findByText("Morning brief");
    fireEvent.click(screen.getByLabelText("Back to launcher"));
    expect(onClose).toHaveBeenCalled();
  });
});
