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

  it("renders a merge leaf as entity name chips, not a uuid sentence", async () => {
    const merge = detail({
      kind: "merge",
      title: "Merge “F-150” and “F150”",
      nodes: [
        {
          id: "m",
          parent_id: null,
          type: "leaf",
          op: "merge_entities",
          label: "Merge “F-150” and “F150”",
          preview: {
            entity_a: "98f10c10-110f-454d-a355-f51d0687adf3",
            entity_b: "5b0ac405-846d-42af-9dfe-45bbc5b1f0e1",
            name_a: "F-150",
            name_b: "F150",
            kind_a: "Product",
            kind_b: "Product",
          },
          deps: [],
          status: "pending",
        },
      ],
    });
    const { container } = render(
      <ProposalTree
        proposalId="p1"
        onClose={vi.fn()}
        getProposal={vi.fn(async () => merge)}
        decideNode={vi.fn()}
        enactProposal={vi.fn()}
      />,
    );
    // Both entity names render as chips; the raw uuids never appear in the body.
    expect(await screen.findByText("F-150")).toBeInTheDocument();
    expect(screen.getByText("F150")).toBeInTheDocument();
    expect(container.textContent).not.toContain("98f10c10");
  });

  it("does not echo the panel title when a leaf's label repeats it", async () => {
    const dup = detail({
      title: "PCP is Dr. Lin",
      nodes: [
        {
          id: "a",
          parent_id: null,
          type: "leaf",
          op: "add_note",
          label: "PCP is Dr. Lin", // single-leaf corrections set label === title
          preview: { body: "PCP is Dr. Lin" },
          deps: [],
          status: "pending",
        },
      ],
    });
    render(
      <ProposalTree
        proposalId="p1"
        onClose={vi.fn()}
        getProposal={vi.fn(async () => dup)}
        decideNode={vi.fn()}
        enactProposal={vi.fn()}
      />,
    );
    // Title in the bar (once) + body once — the head shows the op, not a third copy.
    expect(await screen.findByText("New note")).toBeInTheDocument();
    expect(screen.getAllByText("PCP is Dr. Lin")).toHaveLength(2);
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
