import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type IntakeLinkEditorDeps, IntakeLinkProposalEditor } from "./IntakeLinkProposalEditor";

function node(over: Record<string, unknown> = {}) {
  return {
    id: "node-1",
    status: "pending",
    preview: {
      subject_id: "dad",
      domain: "health",
      persona_brief: "Be warm.",
      fields_brief: "conditions, meds, allergies",
      opening_blurb: "Please share family history.",
      label: "Dad's history",
      max_runs: 5,
      max_opens: 20,
      bind_on_first: true,
      ttl_hours: 24,
      capture_enterer_name: true,
      disclose_owner_identity: false,
      ...over,
    },
  };
}

function deps(over: Partial<IntakeLinkEditorDeps> = {}): IntakeLinkEditorDeps {
  return {
    patchConfig: vi.fn(async () => {}),
    mintFromProposal: vi.fn(async () => ({
      id: "link-9",
      label: "Dad's history",
      expires_at: "2026-07-01T00:00:00Z",
      secret: "s3cr3t",
    })),
    rejectNode: vi.fn(async () => {}),
    ...over,
  };
}

describe("IntakeLinkProposalEditor", () => {
  it("seeds the form from the staged config and previews the recipient view", async () => {
    render(
      <IntakeLinkProposalEditor proposalId="p1" node={node()} onClose={vi.fn()} deps={deps()} />,
    );

    expect((screen.getByLabelText("Opening blurb") as HTMLTextAreaElement).value).toBe(
      "Please share family history.",
    );
    // Subject + domain are locked (fixed at staging) — rendered, not editable.
    expect(screen.getByText(/dad · Medical/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Opening blurb"), {
      target: { value: "New blurb for the recipient." },
    });
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));
    expect(screen.getByText("New blurb for the recipient.")).toBeInTheDocument();
  });

  it("patches every editable field then mints, surfacing the secret once", async () => {
    const d = deps();
    const onMinted = vi.fn();
    render(
      <IntakeLinkProposalEditor
        proposalId="p1"
        node={node()}
        onClose={vi.fn()}
        onMinted={onMinted}
        deps={d}
      />,
    );

    fireEvent.change(screen.getByLabelText("Opening blurb"), { target: { value: "Edited." } });
    fireEvent.click(screen.getByRole("button", { name: /Approve & mint/ }));

    await waitFor(() => expect(d.mintFromProposal).toHaveBeenCalledWith("p1"));
    expect(d.patchConfig).toHaveBeenCalledWith(
      "node-1",
      expect.objectContaining({ opening_blurb: "Edited.", max_runs: 5, bind_on_first: true }),
    );
    expect(onMinted).toHaveBeenCalled();
    // The show-once secret card appears with the full share URL.
    expect(await screen.findByText(/Your link is ready/)).toBeInTheDocument();
    expect(screen.getByText(/\/intake#t=s3cr3t/)).toBeInTheDocument();
  });

  it("requires a confirming second tap to reject, then closes", async () => {
    const d = deps();
    const onClose = vi.fn();
    render(<IntakeLinkProposalEditor proposalId="p1" node={node()} onClose={onClose} deps={d} />);

    const reject = screen.getByRole("button", { name: "Reject" });
    fireEvent.click(reject);
    expect(d.rejectNode).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Tap again/ }));

    await waitFor(() => expect(d.rejectNode).toHaveBeenCalledWith("p1", "node-1"));
    expect(onClose).toHaveBeenCalled();
  });

  it("blocks minting an already-rejected proposal", () => {
    render(
      <IntakeLinkProposalEditor
        proposalId="p2"
        node={{ ...node(), status: "rejected" }}
        onClose={vi.fn()}
        deps={deps()}
      />,
    );
    expect(screen.getByText(/was rejected/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve & mint/ })).toBeDisabled();
  });
});
