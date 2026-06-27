import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type Automation, type AutomationsResponse, type CatalogAction, api } from "../api/client";
import { AutomationsScreen } from "./AutomationsScreen";

const NOW = new Date().toISOString();
const SOON = new Date(Date.now() + 120_000).toISOString();

const EVENT: Automation = {
  trigger_id: "e1",
  kind: "on_event",
  group: "event",
  pipeline: "event_integrate_note",
  enabled: true,
  manual: false,
  steps: [
    {
      action: "integrate_note",
      cost_class: "expensive",
      description: "Extract facts, resolve entities.",
      known: true,
    },
  ],
  recent_runs: [
    {
      id: "r1",
      status: "error",
      started_at: NOW,
      duration_ms: 31000,
      last_error: "integrate_note: ocr dep failed",
    },
  ],
  on_event: "note.ingested",
  schedule_id: null,
  interval_seconds: null,
  next_run_at: null,
  last_run_at: null,
  schedule_kind: null,
  schedule_freq: null,
  schedule_days: [],
  schedule_time: null,
  run_at: null,
  timezone: null,
};

const RECONCILER: Automation = {
  trigger_id: "s1",
  kind: "schedule",
  group: "reconcile",
  pipeline: "reconcile_pending_notes",
  enabled: true,
  manual: true,
  steps: [
    {
      action: "reconcile_pending_notes",
      cost_class: "cheap",
      description: "Re-enqueue ingest for pending notes.",
      known: true,
    },
  ],
  recent_runs: [{ id: "r2", status: "done", started_at: NOW, duration_ms: 200, last_error: null }],
  on_event: null,
  schedule_id: "sched-1",
  interval_seconds: 300,
  next_run_at: SOON,
  last_run_at: NOW,
  schedule_kind: "interval",
  schedule_freq: null,
  schedule_days: [],
  schedule_time: null,
  run_at: null,
  timezone: "UTC",
};

// A nightly sweep set to a task-style weekly repeat — the editable cadence the owner
// can change (the reconciler above keeps its interval mode and offers no editor).
const NIGHTLY: Automation = {
  trigger_id: "n1",
  kind: "schedule",
  group: "nightly",
  pipeline: "nightly_purge_deleted_artifacts",
  enabled: true,
  manual: true,
  steps: [
    {
      action: "purge_deleted_artifacts",
      cost_class: "cheap",
      description: "Reap deleted-note artifacts.",
      known: true,
    },
  ],
  recent_runs: [],
  on_event: null,
  schedule_id: "sched-n1",
  interval_seconds: null,
  next_run_at: SOON,
  last_run_at: NOW,
  schedule_kind: "repeat",
  schedule_freq: "weekly",
  schedule_days: [0, 6],
  schedule_time: "02:00",
  run_at: null,
  timezone: "UTC",
};

const ACTIONS: CatalogAction[] = [
  {
    name: "integrate_note",
    cost_class: "expensive",
    domain_optional: true,
    mutating: true,
    description: "Extract facts.",
    seeded: true,
  },
  {
    name: "reconcile_pending_notes",
    cost_class: "cheap",
    domain_optional: true,
    mutating: true,
    description: "Re-enqueue ingest.",
    seeded: false,
  },
];

function mount(opts: { automations?: Automation[]; actions?: CatalogAction[] } = {}) {
  const data: AutomationsResponse = {
    automations: opts.automations ?? [EVENT, RECONCILER],
    actions: opts.actions ?? ACTIONS,
  };
  vi.spyOn(api, "automations").mockResolvedValue(data);
  const onClose = vi.fn();
  const onOpenRuns = vi.fn();
  render(<AutomationsScreen onClose={onClose} onOpenRuns={onOpenRuns} />);
  return { onClose, onOpenRuns };
}

describe("AutomationsScreen", () => {
  it("renders the grouped when -> do cards", async () => {
    mount();
    expect(await screen.findByText("On a note event")).toBeInTheDocument();
    expect(screen.getByText("Reconcilers · every few minutes")).toBeInTheDocument();
    // The event card reads as "When <event> -> run <pipeline>".
    expect(screen.getByText("note.ingested")).toBeInTheDocument();
    expect(screen.getAllByText("event_integrate_note").length).toBeGreaterThan(0);
    expect(screen.getByText("reconcile_pending_notes")).toBeInTheDocument();
  });

  it("expands a card to its steps + recent runs, surfacing a failed run's error", async () => {
    mount();
    fireEvent.click(await screen.findByText("note.ingested"));
    // Pipeline step: action + cost-class chip + description.
    expect(await screen.findByText("integrate_note")).toBeInTheDocument();
    expect(screen.getByText("expensive")).toBeInTheDocument();
    expect(screen.getByText("Extract facts, resolve entities.")).toBeInTheDocument();
    // A failed recent run shows its error text.
    expect(screen.getByText(/ocr dep failed/)).toBeInTheDocument();
  });

  it("toggles a trigger (and its schedule) through the owner-only PATCH endpoints", async () => {
    const setTrigger = vi.spyOn(api, "setTriggerEnabled").mockResolvedValue();
    const setSchedule = vi.spyOn(api, "setScheduleEnabled").mockResolvedValue();
    mount();
    // The reconciler is schedule-bound, so flipping it toggles BOTH.
    const sw = await screen.findByLabelText("Disable reconcile_pending_notes");
    fireEvent.click(sw);
    await waitFor(() => expect(setTrigger).toHaveBeenCalledWith("s1", false));
    expect(setSchedule).toHaveBeenCalledWith("sched-1", false);
    expect(await screen.findByRole("status")).toHaveTextContent(/disabled/);
  });

  it("fires a manual trigger via Run now and is disabled for event triggers", async () => {
    const runTrigger = vi.spyOn(api, "runTrigger").mockResolvedValue();
    mount();
    // Expand the reconciler (manual) to reveal its Run-now.
    fireEvent.click(await screen.findByText("reconcile_pending_notes"));
    const runBtn = await screen.findByRole("button", { name: /Run now/ });
    fireEvent.click(runBtn);
    await waitFor(() => expect(runTrigger).toHaveBeenCalledWith("s1"));

    // Expand the event card: its Run-now is disabled (auto, not manually fireable).
    fireEvent.click(screen.getByText("note.ingested"));
    const runBtns = screen.getAllByRole("button", { name: /Run now/ });
    expect(runBtns.some((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });

  it("drills through to the Runs surface from All runs", async () => {
    const { onOpenRuns } = mount();
    fireEvent.click(await screen.findByText("note.ingested"));
    fireEvent.click(await screen.findByRole("button", { name: /All runs/ }));
    expect(onOpenRuns).toHaveBeenCalled();
  });

  it("lists the action registry in the Catalog tab with seeded/in-code flags", async () => {
    mount();
    fireEvent.click(await screen.findByRole("tab", { name: /Catalog/ }));
    const heading = await screen.findByText(/Action registry · 2 actions/);
    expect(heading).toBeInTheDocument();
    const cat = heading.parentElement as HTMLElement;
    expect(within(cat).getByText("seeded")).toBeInTheDocument();
    expect(within(cat).getByText("in-code")).toBeInTheDocument();
  });

  it("returns to Ops via the back control", async () => {
    const { onClose } = mount();
    await screen.findByText("note.ingested");
    fireEvent.click(screen.getByLabelText("Back to launcher"));
    expect(onClose).toHaveBeenCalled();
  });

  it("edits a nightly sweep's schedule like a task and PUTs the new spec", async () => {
    const update = vi.spyOn(api, "updateSchedule").mockResolvedValue();
    mount({ automations: [NIGHTLY] });
    // Expand the nightly sweep and open its schedule editor.
    fireEvent.click(await screen.findByText("nightly_purge_deleted_artifacts"));
    fireEvent.click(await screen.findByRole("button", { name: /Edit schedule/ }));
    // Switch from weekly repeat to a plain daily repeat and save.
    fireEvent.click(await screen.findByRole("button", { name: "Daily" }));
    fireEvent.click(screen.getByRole("button", { name: /Save schedule/ }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update).toHaveBeenCalledWith(
      "sched-n1",
      expect.objectContaining({
        schedule_kind: "repeat",
        schedule_freq: "daily",
        interval_seconds: null,
        // A daily repeat carries no explicit day list.
        schedule_days: [],
      }),
    );
  });

  it("offers the interval editor for a reconciler", async () => {
    mount({ automations: [RECONCILER] });
    fireEvent.click(await screen.findByText("reconcile_pending_notes"));
    fireEvent.click(await screen.findByRole("button", { name: /Edit schedule/ }));
    // The reconciler opens on the number+unit interval control (Minutes/Hours unit).
    expect(await screen.findByLabelText("Interval value")).toBeInTheDocument();
    const unit = screen.getByLabelText("Interval unit") as HTMLSelectElement;
    expect(within(unit).getByText("minutes")).toBeInTheDocument();
    expect(within(unit).getByText("hours")).toBeInTheDocument();
    // It must NOT offer the wall-clock repeat that would downgrade a sub-day sweep.
    expect(screen.queryByRole("tab", { name: "Repeats" })).not.toBeInTheDocument();
  });

  it("edits a reconciler's interval and PUTs interval_seconds", async () => {
    const update = vi.spyOn(api, "updateSchedule").mockResolvedValue();
    mount({ automations: [RECONCILER] });
    fireEvent.click(await screen.findByText("reconcile_pending_notes"));
    fireEvent.click(await screen.findByRole("button", { name: /Edit schedule/ }));
    // Change 5 -> 15 minutes and save.
    fireEvent.change(await screen.findByLabelText("Interval value"), { target: { value: "15" } });
    fireEvent.click(screen.getByRole("button", { name: /Save schedule/ }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update).toHaveBeenCalledWith(
      "sched-1",
      expect.objectContaining({
        schedule_kind: "interval",
        interval_seconds: 900,
        // The wall-clock fields stay empty for an interval cadence.
        schedule_freq: null,
        schedule_time: null,
        schedule_days: [],
        run_at: null,
      }),
    );
  });
});
