import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProposalDetail } from "../agent/types";
import type {
  IntakeLink,
  IntakeSessionRow,
  IntakeSubmission,
  IntakeSubmissionDetail,
} from "../intake/types";
import { type IntakeLinksDeps, IntakeLinksScreen } from "./IntakeLinksScreen";

function proposal(over: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    id: "P1",
    kind: "intake-submission",
    status: "staged",
    domain: "health",
    title: "Dad's health",
    nodes: [
      {
        id: "N1",
        parent_id: null,
        type: "leaf",
        op: "add_intake_note",
        label: "Dad's health",
        preview: { body: "Dad has **diabetes**.", domain: "health", submission_id: "S1" },
        deps: [],
        status: "pending",
      },
    ],
    ...over,
  };
}

function link(over: Partial<IntakeLink> = {}): IntakeLink {
  return {
    id: "L1",
    subject_id: "dad",
    domain_code: "health",
    label: "Dad's medical history",
    fields_brief: "conditions",
    persona_brief: "warm",
    opening_blurb: "Please share.",
    max_runs: 5,
    runs_used: 3,
    max_opens: 20,
    opens_used: 11,
    bind_on_first: true,
    capture_enterer_name: true,
    disclose_owner_identity: false,
    status: "active",
    created_at: "2026-06-30T00:00:00Z",
    expires_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

function submission(over: Partial<IntakeSubmission> = {}): IntakeSubmission {
  return {
    id: "S1",
    link_id: "L1",
    session_id: "sess-1",
    enterer_name: "Carol Hopkins",
    draft: { summary: "Dad has diabetes." },
    status: "submitted",
    proposal_id: null,
    note_ids: [],
    created_at: "2026-06-30T00:00:00Z",
    updated_at: "2026-06-30T00:00:00Z",
    ...over,
  };
}

function deps(over: Partial<IntakeLinksDeps> = {}): IntakeLinksDeps {
  return {
    listLinks: vi.fn(async () => [link()]),
    listSubmissions: vi.fn(async () => [submission()]),
    listSessions: vi.fn(
      async (): Promise<IntakeSessionRow[]> => [
        { id: "sess-1", link_id: "L1", opened_at: "2026-06-30T00:00:00Z", status: "submitted" },
      ],
    ),
    getSubmission: vi.fn(
      async (): Promise<IntakeSubmissionDetail> => ({
        ...submission(),
        transcript: [
          { role: "interviewer", text: "Any conditions?" },
          { role: "recipient", text: "Dad has diabetes." },
        ],
      }),
    ),
    materialize: vi.fn(async () => ({ proposal_id: "P1" })),
    revokeLink: vi.fn(async () => {}),
    mintLink: vi.fn(async () => ({
      id: "L2",
      label: "Dad's medical history",
      expires_at: "2026-07-02T00:00:00Z",
      secret: "fresh",
    })),
    getProposal: vi.fn(async () => proposal()),
    decideNode: vi.fn(async () => {}),
    enactProposal: vi.fn(async () => ({ enacted: ["N1"], held: [] })),
    ...over,
  };
}

describe("IntakeLinksScreen", () => {
  it("groups links into Needs review / Active / Closed", async () => {
    const d = deps({
      listLinks: vi.fn(async () => [
        link({ id: "L1", status: "active" }),
        link({ id: "L3", label: "Insurance", status: "revoked" }),
      ]),
      listSubmissions: vi.fn(async (id: string) => (id === "L1" ? [submission()] : [])),
    });
    render(<IntakeLinksScreen deps={d} />);

    expect(await screen.findByText("Needs review")).toBeInTheDocument();
    // "Active" also appears as a status badge — scope to the section header.
    expect(screen.getByText("Active", { selector: ".intake-sec-h" })).toBeInTheDocument();
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.getByText(/1 submission awaiting review/)).toBeInTheDocument();
    expect(screen.getByText("Insurance")).toBeInTheDocument();
  });

  it("shows a general (no-subject) link's About as 'No specific person'", async () => {
    const d = deps({
      listLinks: vi.fn(async () => [
        link({ subject_id: null, domain_code: "general", label: "Family cookbook" }),
      ]),
      listSessions: vi.fn(async () => []),
      listSubmissions: vi.fn(async () => []),
    });
    render(<IntakeLinksScreen deps={d} />);
    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    expect(await screen.findByText(/No specific person · General/)).toBeInTheDocument();
  });

  it("materializes, then approves the single note inline (no Proposals-panel hop)", async () => {
    const d = deps();
    render(<IntakeLinksScreen deps={d} />);

    // Active link row → detail.
    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    expect(await screen.findByText("Conversations")).toBeInTheDocument();

    // Open the awaiting conversation.
    fireEvent.click(await screen.findByText("Carol Hopkins"));
    expect(await screen.findByText(/read the full history/)).toBeInTheDocument();
    // The summary draft and the transcript bubble both echo the answer.
    expect(screen.getAllByText("Dad has diabetes.").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Any conditions?")).toBeInTheDocument();

    // Review as a note → materialize, which loads the single-note Proposal.
    fireEvent.click(screen.getByRole("button", { name: /Review as a note/ }));
    await waitFor(() => expect(d.materialize).toHaveBeenCalledWith("S1"));

    // The materialized note is previewed inline (rich text: **diabetes** → bold).
    expect(await screen.findByText(/Note to add · Dad's health/)).toBeInTheDocument();

    // Approve → decide + enact the leaf right here, then confirm it was added.
    fireEvent.click(await screen.findByRole("button", { name: /Approve & add to notes/ }));
    await waitFor(() => expect(d.decideNode).toHaveBeenCalledWith("P1", "N1", "approve"));
    expect(d.enactProposal).toHaveBeenCalledWith("P1");
    expect(await screen.findByText(/Added to your notes/)).toBeInTheDocument();
  });

  it("rejects a materialized note inline, keeping nothing", async () => {
    const d = deps();
    render(<IntakeLinksScreen deps={d} />);

    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    fireEvent.click(await screen.findByText("Carol Hopkins"));
    fireEvent.click(await screen.findByRole("button", { name: /Review as a note/ }));
    await waitFor(() => expect(d.materialize).toHaveBeenCalledWith("S1"));

    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await waitFor(() => expect(d.decideNode).toHaveBeenCalledWith("P1", "N1", "reject"));
    expect(d.enactProposal).not.toHaveBeenCalled();
    expect(await screen.findByText(/nothing was kept/)).toBeInTheDocument();
  });

  it("re-mints (clone + revoke) and reveals the fresh secret", async () => {
    const d = deps();
    render(<IntakeLinksScreen deps={d} />);

    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    fireEvent.click(await screen.findByRole("button", { name: /Re-mint & copy link/ }));

    await waitFor(() => expect(d.mintLink).toHaveBeenCalled());
    expect(d.revokeLink).toHaveBeenCalledWith("L1");
    expect(await screen.findByText(/Re-minted — copy the fresh link/)).toBeInTheDocument();
    expect(screen.getByText(/\/intake#t=fresh/)).toBeInTheDocument();
  });

  it("clamps a re-minted link's TTL to the backend's 720h ceiling", async () => {
    const d = deps({
      // A span just past 30 days — float drift would otherwise trip the backend's le=720.
      listLinks: vi.fn(async () => [
        link({ created_at: "2026-01-01T00:00:00Z", expires_at: "2026-01-31T00:00:30Z" }),
      ]),
    });
    render(<IntakeLinksScreen deps={d} />);
    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    fireEvent.click(await screen.findByRole("button", { name: /Re-mint & copy link/ }));

    await waitFor(() => expect(d.mintLink).toHaveBeenCalled());
    expect(d.mintLink).toHaveBeenCalledWith(
      expect.objectContaining({ ttl_hours: expect.any(Number) }),
    );
    const body = (d.mintLink as ReturnType<typeof vi.fn>).mock.calls.at(0)?.at(0);
    expect(body.ttl_hours).toBeLessThanOrEqual(720);
  });

  it("revokes a link only after a confirming second tap", async () => {
    const d = deps();
    render(<IntakeLinksScreen deps={d} />);

    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    const revoke = await screen.findByRole("button", { name: "Revoke link" });
    fireEvent.click(revoke);
    expect(d.revokeLink).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Tap again — revoke/ }));
    await waitFor(() => expect(d.revokeLink).toHaveBeenCalledWith("L1"));
  });

  it("tags an abandoned session and shows nothing was kept", async () => {
    const d = deps({
      listSubmissions: vi.fn(async () => []),
      listSessions: vi.fn(async () => [
        { id: "sess-2", link_id: "L1", opened_at: "2026-06-30T00:00:00Z", status: "abandoned" },
      ]),
    });
    render(<IntakeLinksScreen deps={d} />);

    fireEvent.click(await screen.findByText(/3\/5 submitted/));
    fireEvent.click(await screen.findByText("Abandoned"));
    expect(await screen.findByText(/nothing was\s+submitted/)).toBeInTheDocument();
  });
});
