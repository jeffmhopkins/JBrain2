import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type Task, type TaskRun, api } from "../api/client";
import { TasksScreen } from "./TasksScreen";

const FUTURE = new Date(Date.now() + 3600_000).toISOString();

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
  last_run_at: null,
};

const MANUAL: Task = {
  ...SCHEDULED,
  id: "t2",
  name: "Ad hoc digest",
  schedule_kind: "on_demand",
  schedule_freq: null,
  next_run_at: null,
};

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
  render(<TasksScreen onClose={onClose} />);
  return { onClose };
}

describe("TasksScreen", () => {
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
    fireEvent.click(await screen.findByText("New task"));
    const prompt = await screen.findByPlaceholderText("Tell the agent what to do on each run…");
    fireEvent.change(prompt, { target: { value: "Summarize my week." } });
    fireEvent.click(screen.getByText("Save task"));
    await waitFor(() => expect(createTask).toHaveBeenCalled());
    expect(createTask.mock.calls[0]?.[0].prompt).toBe("Summarize my week.");
  });

  it("returns to the launcher via the back control", async () => {
    const { onClose } = mount();
    await screen.findByText("Morning brief");
    fireEvent.click(screen.getByLabelText("Back to launcher"));
    expect(onClose).toHaveBeenCalled();
  });
});
