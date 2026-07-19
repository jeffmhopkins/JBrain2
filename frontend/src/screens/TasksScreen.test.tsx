import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { type Task, type TaskGroup, type TaskRun, api } from "../api/client";
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

const GROUPS: TaskGroup[] = [{ id: "g-money", name: "Money", position: 0 }];

// A grouped task and an ungrouped one, so both the group header and the trailing
// "Ungrouped" bucket render.
const GROUPED: Task = {
  id: "t1",
  group_id: "g-money",
  position: 0,
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

const UNGROUPED_TASK: Task = {
  ...GROUPED,
  id: "t2",
  group_id: null,
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

function mount(tasks: Task[] = [GROUPED, UNGROUPED_TASK], groups: TaskGroup[] = GROUPS) {
  vi.spyOn(api, "tasks").mockResolvedValue(tasks);
  vi.spyOn(api, "taskGroups").mockResolvedValue(groups);
  vi.spyOn(api, "taskRuns").mockResolvedValue([RUN]);
  const onClose = vi.fn();
  const onOpenSession = vi.fn();
  const view = render(<TasksScreen onClose={onClose} onOpenSession={onOpenSession} />);
  return { onClose, onOpenSession, unmount: view.unmount };
}

describe("TasksScreen", () => {
  beforeEach(() => localStorage.clear()); // per-task "viewed" markers are device-local

  it("renders custom group headers and a trailing Ungrouped bucket", async () => {
    mount();
    expect(await screen.findByText("Morning brief")).toBeInTheDocument();
    // The group the owner named + the system Ungrouped catch-all, both as headers.
    expect(screen.getByRole("heading", { name: "Money" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ungrouped" })).toBeInTheDocument();
    expect(screen.getByText("Ad hoc digest")).toBeInTheDocument();
  });

  it("filters to one group via the chip row", async () => {
    mount();
    await screen.findByText("Morning brief");
    fireEvent.click(screen.getByRole("tab", { name: /^Money/ }));
    // Only the Money task remains; the ungrouped one is filtered out.
    expect(screen.getByText("Morning brief")).toBeInTheDocument();
    expect(screen.queryByText("Ad hoc digest")).not.toBeInTheDocument();
  });

  it("moves a task to another group through the ⋯ sheet", async () => {
    const reorder = vi.spyOn(api, "reorderTasks").mockResolvedValue([]);
    mount();
    await screen.findByText("Ad hoc digest");
    // Open the ungrouped task's move sheet and file it into Money.
    fireEvent.click(screen.getByRole("button", { name: "Move Ad hoc digest to a group" }));
    const sheet = await screen.findByRole("dialog", { name: "Move task" });
    fireEvent.click(within(sheet).getByRole("button", { name: /Money/ }));
    await waitFor(() => expect(reorder).toHaveBeenCalled());
    // Destination bucket is Money, and the moved id is appended to its ordered list.
    expect(reorder).toHaveBeenCalledWith("g-money", ["t1", "t2"]);
  });

  it("creates a group from the move sheet and files the task into it", async () => {
    const createGroup = vi
      .spyOn(api, "createTaskGroup")
      .mockResolvedValue({ id: "g-new", name: "Errands", position: 1 });
    const reorder = vi.spyOn(api, "reorderTasks").mockResolvedValue([]);
    mount();
    await screen.findByText("Ad hoc digest");
    fireEvent.click(screen.getByRole("button", { name: "Move Ad hoc digest to a group" }));
    const sheet = await screen.findByRole("dialog", { name: "Move task" });
    fireEvent.click(within(sheet).getByRole("button", { name: "New group…" }));
    fireEvent.change(within(sheet).getByLabelText("New group name"), {
      target: { value: "Errands" },
    });
    fireEvent.click(within(sheet).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createGroup).toHaveBeenCalledWith("Errands"));
    await waitFor(() => expect(reorder).toHaveBeenCalledWith("g-new", ["t2"]));
  });

  it("reorders a task within its group via the keyboard grip", async () => {
    const reorder = vi.spyOn(api, "reorderTasks").mockResolvedValue([]);
    // Two tasks in the same group so there is something to reorder.
    const a = { ...GROUPED, id: "t1", name: "Alpha", position: 0 };
    const b = { ...GROUPED, id: "t3", name: "Beta", position: 1 };
    mount([a, b]);
    await screen.findByText("Alpha");
    fireEvent.click(screen.getByRole("button", { name: "Organize groups and order" }));
    // Nudge Alpha down past Beta with the arrow key on its grip handle.
    fireEvent.keyDown(screen.getByRole("button", { name: /Reorder Alpha/ }), { key: "ArrowDown" });
    await waitFor(() => expect(reorder).toHaveBeenCalledWith("g-money", ["t3", "t1"]));
  });

  it("renames a group inline in organize mode", async () => {
    const rename = vi
      .spyOn(api, "renameTaskGroup")
      .mockResolvedValue({ id: "g-money", name: "Finance", position: 0 });
    mount();
    await screen.findByText("Morning brief");
    fireEvent.click(screen.getByRole("button", { name: "Organize groups and order" }));
    fireEvent.click(screen.getByRole("button", { name: "Rename Money" }));
    const input = screen.getByLabelText("Rename group");
    fireEvent.change(input, { target: { value: "Finance" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(rename).toHaveBeenCalledWith("g-money", "Finance"));
  });

  it("deletes a group behind a tap-again confirm", async () => {
    const del = vi.spyOn(api, "deleteTaskGroup").mockResolvedValue(undefined);
    mount();
    await screen.findByText("Morning brief");
    fireEvent.click(screen.getByRole("button", { name: "Organize groups and order" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Money" }));
    // First tap arms; nothing deleted yet.
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete Money" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("g-money"));
  });

  it("collapses a group header to hide its cards and expands it again", async () => {
    mount();
    await screen.findByText("Morning brief");
    // Collapsing Money hides its card; the Ungrouped bucket is untouched.
    fireEvent.click(screen.getByRole("button", { name: "Collapse Money group" }));
    expect(screen.queryByText("Morning brief")).not.toBeInTheDocument();
    expect(screen.getByText("Ad hoc digest")).toBeInTheDocument();
    // The same header now offers to expand it back.
    fireEvent.click(screen.getByRole("button", { name: "Expand Money group" }));
    expect(screen.getByText("Morning brief")).toBeInTheDocument();
  });

  it("remembers collapsed groups across a remount (device-local)", async () => {
    const { unmount } = mount();
    await screen.findByText("Morning brief");
    fireEvent.click(screen.getByRole("button", { name: "Collapse Money group" }));
    expect(screen.queryByText("Morning brief")).not.toBeInTheDocument();
    unmount();

    // A fresh mount (new session) reads the persisted collapse state.
    mount();
    await screen.findByText("Ad hoc digest");
    expect(screen.queryByText("Morning brief")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand Money group" })).toBeInTheDocument();
  });

  it("ignores collapse when filtered to a single group (no header to toggle)", async () => {
    localStorage.setItem("jb.tasks.collapsedGroups", JSON.stringify(["g-money"]));
    mount();
    await screen.findByText("Ad hoc digest");
    // Money is collapsed in the All view…
    expect(screen.queryByText("Morning brief")).not.toBeInTheDocument();
    // …but filtering to it shows the card regardless of the stored collapse.
    fireEvent.click(screen.getByRole("tab", { name: /^Money/ }));
    expect(screen.getByText("Morning brief")).toBeInTheDocument();
  });

  it("toggles a task through the optimistic enable endpoint", async () => {
    const setEnabled = vi.spyOn(api, "setTaskEnabled").mockResolvedValue({
      ...GROUPED,
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
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Open latest session/ })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("NEW")).not.toBeInTheDocument();
    expect(screen.queryByText("Daily Action Brief — 3 items need a reply")).not.toBeInTheDocument();
  });

  it("persists the viewed marker so the band stays hidden after a remount", async () => {
    vi.spyOn(api, "tasks").mockResolvedValue([GROUPED, UNGROUPED_TASK]);
    vi.spyOn(api, "taskGroups").mockResolvedValue(GROUPS);
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
    await screen.findByText("Morning brief");
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
    const createTask = vi.spyOn(api, "createTask").mockResolvedValue(GROUPED);
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
      .mockResolvedValue({ ...GROUPED, agent: "archivist" });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "New task" }));
    const prompt = await screen.findByPlaceholderText("Tell the agent what to do on each run…");
    fireEvent.change(prompt, { target: { value: "Label everything from chase.com." } });
    fireEvent.click(screen.getByRole("button", { name: /Archivist/ }));
    fireEvent.click(screen.getByText("Save task"));
    await waitFor(() => expect(createTask).toHaveBeenCalled());
    expect(createTask.mock.calls[0]?.[0].agent).toBe("archivist");
    expect(createTask.mock.calls[0]?.[0].domain_scopes).toEqual([]);
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
