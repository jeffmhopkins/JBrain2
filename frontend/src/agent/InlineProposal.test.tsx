import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InlineProposal } from "./InlineProposal";
import type { EnactResult, ProposalDetail } from "./types";

// A two-med tree where a summary note depends on both meds — mirrors the approved
// mock (docs/mocks/inline-approvals/d-one-tree.html): editable med leaves + a
// dependent that goes "held" when a prerequisite is declined.
function detail(over: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    id: "p1",
    kind: "correction",
    status: "staged",
    domain: "health",
    title: "Update — BP meds",
    nodes: [
      {
        id: "l1",
        parent_id: null,
        type: "leaf",
        op: "add_note",
        label: "lisinopril",
        preview: { body: "10 mg daily" },
        deps: [],
        status: "pending",
      },
      {
        id: "l2",
        parent_id: null,
        type: "leaf",
        op: "add_note",
        label: "HCTZ",
        preview: { body: "12.5 mg daily" },
        deps: [],
        status: "pending",
      },
      {
        id: "l3",
        parent_id: null,
        type: "leaf",
        op: "add_note",
        label: "combination note",
        preview: { body: "on combination therapy" },
        deps: ["l1", "l2"],
        status: "pending",
      },
    ],
    ...over,
  };
}

const ok = (over: Partial<EnactResult> = {}): EnactResult => ({
  enacted: ["l1", "l2", "l3"],
  held: [],
  outcome: "Enacted 3 of 3 — 3 approved. Returned to assistant as 3 approvals.",
  ...over,
});

function renderCard(fns: {
  getProposal?: () => Promise<ProposalDetail>;
  decideNode?: ReturnType<typeof vi.fn>;
  editNode?: ReturnType<typeof vi.fn>;
  enactProposal?: ReturnType<typeof vi.fn>;
  onOutcome?: ReturnType<typeof vi.fn>;
  onEnacted?: ReturnType<typeof vi.fn>;
}) {
  const decideNode = fns.decideNode ?? vi.fn(async () => undefined);
  const editNode = fns.editNode ?? vi.fn(async () => undefined);
  const enactProposal = fns.enactProposal ?? vi.fn(async () => ok());
  const onOutcome = fns.onOutcome ?? vi.fn();
  const onEnacted = fns.onEnacted ?? vi.fn();
  render(
    <InlineProposal
      proposalId="p1"
      onOutcome={onOutcome}
      onEnacted={onEnacted}
      getProposal={fns.getProposal ?? (async () => detail())}
      decideNode={decideNode}
      editNode={editNode}
      enactProposal={enactProposal}
    />,
  );
  return { decideNode, editNode, enactProposal, onOutcome, onEnacted };
}

describe("InlineProposal", () => {
  it("renders the leaves, defaults all approved, and shows the ready tally", async () => {
    renderCard({});
    expect(await screen.findByText("Update — BP meds")).toBeInTheDocument();
    expect(screen.getByText("lisinopril")).toBeInTheDocument();
    // 3 of 3 ready by default.
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/of 3 ready/)).toBeInTheDocument();
  });

  it("double-taps Enact: arms first, then approves every leaf, enacts, and returns the outcome", async () => {
    const decideNode = vi.fn(async () => undefined);
    const enactProposal = vi.fn(async () => ok());
    const onOutcome = vi.fn();
    const onEnacted = vi.fn();
    renderCard({ decideNode, enactProposal, onOutcome, onEnacted });

    const btn = await screen.findByRole("button", { name: /^Enact 3$/ });
    fireEvent.click(btn); // arm
    expect(await screen.findByRole("button", { name: /Tap to enact 3/ })).toBeInTheDocument();
    expect(enactProposal).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Tap to enact 3/ })); // enact
    await waitFor(() => expect(enactProposal).toHaveBeenCalledWith("p1"));
    expect(decideNode).toHaveBeenCalledTimes(3);
    expect(decideNode).toHaveBeenCalledWith("p1", "l1", "approve");
    expect(onOutcome).toHaveBeenCalledWith(
      "Enacted 3 of 3 — 3 approved. Returned to assistant as 3 approvals.",
    );
    expect(onEnacted).toHaveBeenCalled();
    // Collapses to the resolved line.
    expect(await screen.findByText(/one message sent to the assistant/)).toBeInTheDocument();
  });

  it("declines a leaf with a reason, which rides the reject decision", async () => {
    const decideNode = vi.fn(async () => undefined);
    renderCard({ decideNode });

    // Decline the combination note (a dependent with nothing under it) so the ready
    // count is unambiguous — declining a prerequisite would additionally hold l3.
    fireEvent.click(await screen.findByRole("button", { name: "Decline combination note" }));
    const reason = await screen.findByLabelText("Reason for declining combination note");
    fireEvent.change(reason, { target: { value: "redundant" } });

    fireEvent.click(screen.getByRole("button", { name: /^Enact 2$/ }));
    fireEvent.click(screen.getByRole("button", { name: /Tap to enact 2/ }));
    await waitFor(() => expect(decideNode).toHaveBeenCalledWith("p1", "l3", "reject", "redundant"));
    expect(decideNode).toHaveBeenCalledWith("p1", "l1", "approve");
  });

  it("corrects a value in place, which edits before approving", async () => {
    const decideNode = vi.fn(async () => undefined);
    const editNode = vi.fn(async () => undefined);
    renderCard({ decideNode, editNode });

    fireEvent.click(await screen.findByRole("button", { name: "Correct HCTZ" }));
    const input = await screen.findByLabelText("Correct HCTZ");
    fireEvent.change(input, { target: { value: "25 mg daily" } });
    fireEvent.blur(input);
    expect(await screen.findByText(/· edited/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Enact 3$/ }));
    fireEvent.click(screen.getByRole("button", { name: /Tap to enact 3/ }));
    await waitFor(() => expect(editNode).toHaveBeenCalledWith("p1", "l2", "25 mg daily"));
    expect(decideNode).toHaveBeenCalledWith("p1", "l2", "approve");
  });

  it("holds a dependent when its prerequisite is declined (held not counted as ready)", async () => {
    renderCard({});
    // Decline l1 → l3 (deps l1,l2) becomes held.
    fireEvent.click(await screen.findByRole("button", { name: "Decline lisinopril" }));
    expect(await screen.findByText(/a prerequisite is declined/)).toBeInTheDocument();
    // ready drops to 1 (only l2), 1 declined, 1 held.
    expect(screen.getByText(/of 3 ready/)).toBeInTheDocument();
    expect(screen.getByText(/1 declined/)).toBeInTheDocument();
    expect(screen.getByText(/1 held/)).toBeInTheDocument();
  });

  it("shows a held count in the resolved line when the server holds a leaf", async () => {
    renderCard({ enactProposal: vi.fn(async () => ok({ enacted: ["l1", "l2"], held: ["l3"] })) });
    fireEvent.click(await screen.findByRole("button", { name: /^Enact 3$/ }));
    fireEvent.click(screen.getByRole("button", { name: /Tap to enact 3/ }));
    expect(await screen.findByText(/1 held/)).toBeInTheDocument();
  });
});
