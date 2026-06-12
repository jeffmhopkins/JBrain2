import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ProposalTree } from "./ProposalTree";
import type { Decision, EnactResult, ProposalDetail } from "./types";

function detail(over: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    id: "p1",
    kind: "knowledge",
    status: "staged",
    domain: "health",
    title: "Add two facts",
    nodes: [
      {
        id: "a",
        parent_id: null,
        type: "leaf",
        op: "add_note",
        label: "Add a fact",
        preview: { body: "PCP is Dr. Lin" },
        deps: [],
        status: "pending",
      },
    ],
    ...over,
  };
}

describe("ProposalTree", () => {
  it("renders the tree and each leaf's preview", async () => {
    render(
      <ProposalTree
        proposalId="p1"
        onClose={vi.fn()}
        getProposal={vi.fn(async () => detail())}
        decideNode={vi.fn()}
        enactProposal={vi.fn()}
      />,
    );
    expect(await screen.findByText("Add two facts")).toBeInTheDocument();
    expect(screen.getByText("PCP is Dr. Lin")).toBeInTheDocument();
  });

  it("approves a node and reloads", async () => {
    const decideNode = vi.fn(async (_p: string, _n: string, _d: Decision) => undefined);
    const getProposal = vi.fn(async () => detail());
    render(
      <ProposalTree
        proposalId="p1"
        onClose={vi.fn()}
        getProposal={getProposal}
        decideNode={decideNode}
        enactProposal={vi.fn()}
      />,
    );
    await screen.findByLabelText("Approve Add a fact");
    fireEvent.click(screen.getByLabelText("Approve Add a fact"));
    await waitFor(() => expect(decideNode).toHaveBeenCalledWith("p1", "a", "approve"));
    // Reloaded after the decision (initial load + post-decision load).
    await waitFor(() => expect(getProposal.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("enacts and shows the enacted/held summary", async () => {
    const enactProposal = vi.fn(async (): Promise<EnactResult> => ({ enacted: ["a"], held: [] }));
    render(
      <ProposalTree
        proposalId="p1"
        onClose={vi.fn()}
        getProposal={vi.fn(async () =>
          detail({ nodes: detail().nodes.map((n) => ({ ...n, status: "approved" })) }),
        )}
        decideNode={vi.fn()}
        enactProposal={enactProposal}
      />,
    );
    fireEvent.click(await screen.findByText("Enact approved"));
    await waitFor(() => expect(enactProposal).toHaveBeenCalledWith("p1"));
    expect(await screen.findByText("Enacted 1 · held 0")).toBeInTheDocument();
  });
});
