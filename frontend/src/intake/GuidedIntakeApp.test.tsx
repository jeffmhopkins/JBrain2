import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatEvent } from "../agent/types";
import { api } from "../api/client";
import { GuidedIntakeApp } from "./GuidedIntakeApp";

// Pin the URL state; tests override `secret` to model a missing/invalid link.
const url = vi.hoisted(() => ({ secret: "tok" as string | null }));
vi.mock("./share", async (orig) => ({
  ...(await orig<typeof import("./share")>()),
  parseIntakeSecret: () => url.secret,
}));

const CONFIG = {
  session_id: "sess-1",
  link_id: "link-1",
  opening_blurb: "Please share a phone number.",
  capture_enterer_name: true,
  disclose_owner_identity: false,
};

function streamOf(...texts: string[]) {
  return async function* (): AsyncGenerator<ChatEvent> {
    for (const t of texts) yield { type: "text_delta", text: t };
    yield { type: "done", stop_reason: "end_turn" };
  };
}

describe("GuidedIntakeApp", () => {
  beforeEach(() => {
    url.secret = "tok";
    vi.restoreAllMocks();
  });

  it("redeems, runs the interview, reviews, and confirms", async () => {
    const redeem = vi.spyOn(api, "intakeRedeem").mockResolvedValue(CONFIG);
    vi.spyOn(api, "intakeChat").mockImplementation(
      streamOf("What is the best phone number?") as typeof api.intakeChat,
    );
    const confirm = vi.spyOn(api, "intakeConfirm").mockResolvedValue({ submission_id: "sub-1" });
    render(<GuidedIntakeApp />);

    // Welcome: the blurb shows; Begin is disabled until a name is entered.
    expect(await screen.findByText(/Please share a phone number/)).toBeInTheDocument();
    const begin = screen.getByRole("button", { name: /Begin interview/ });
    expect(begin).toBeDisabled();
    expect(redeem).toHaveBeenCalledWith("tok");

    fireEvent.change(screen.getByLabelText(/Your name/), { target: { value: "Carol" } });
    expect(begin).toBeEnabled();
    fireEvent.click(begin);

    // Interview: the guide's first question streams in.
    expect(await screen.findByText(/What is the best phone number/)).toBeInTheDocument();

    // Proceed to review, which shows the guide's latest summary as the draft.
    fireEvent.click(await screen.findByRole("button", { name: /Review & send/ }));
    expect(await screen.findByText(/Does this look right/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Looks right/ }));
    expect(await screen.findByText(/Sent for review/)).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledWith("Carol");
  });

  it("shows the dead-link state when there is no secret", async () => {
    url.secret = null;
    render(<GuidedIntakeApp />);
    expect(await screen.findByText(/can't be opened/)).toBeInTheDocument();
  });

  it("shows the dead-link state when redeem fails (invalid/expired link)", async () => {
    vi.spyOn(api, "intakeRedeem").mockRejectedValue(new Error("401"));
    render(<GuidedIntakeApp />);
    await waitFor(() => expect(screen.getByText(/can't be opened/)).toBeInTheDocument());
  });
});
