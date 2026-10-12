import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ChatShareSheet } from "./ChatShareSheet";
import type { AgentSession } from "./types";

function chat(over: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "s1",
    title: "SpaceX Pretrade",
    status: "active",
    agent: "jerv",
    domain_scopes: ["general"],
    subject_ids: [],
    created_at: "2026-10-12T17:40:00Z",
    last_active_at: "2026-10-12T17:47:00Z",
    turn_count: 2,
    ...over,
  };
}

const LINK = {
  id: "l1",
  created_at: "2026-10-12T17:47:00Z",
  last_viewed_at: null,
  view_count: 0,
};

describe("ChatShareSheet", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("mints a link, shows it once, and tells the list to refresh", async () => {
    vi.spyOn(api, "chatShares").mockResolvedValue([]);
    const mint = vi.spyOn(api, "mintChatShare").mockResolvedValue({ ...LINK, token: "tok" });
    const onChanged = vi.fn();
    render(<ChatShareSheet session={chat()} onClose={vi.fn()} onChanged={onChanged} />);
    expect(screen.queryByText(/can read your/)).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /Create share link/ }));
    expect(await screen.findByText(`${window.location.origin}/chat/s/tok`)).toBeInTheDocument();
    expect(mint).toHaveBeenCalledWith("s1");
    expect(onChanged).toHaveBeenCalled();
    expect(screen.getByText(/isn’t shown again/)).toBeInTheDocument();
    expect(screen.getByText("Not opened yet")).toBeInTheDocument();
  });

  it("warns about firewalled domains and takes a second tap to publish", async () => {
    vi.spyOn(api, "chatShares").mockResolvedValue([]);
    const mint = vi.spyOn(api, "mintChatShare").mockResolvedValue({ ...LINK, token: "tok" });
    render(
      <ChatShareSheet
        session={chat({ domain_scopes: ["general", "finance", "health"] })}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Financial, Medical")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /Create share link/ }));
    expect(mint).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Tap again to share/ }));
    await waitFor(() => expect(mint).toHaveBeenCalledWith("s1"));
  });

  it("lists live links with their use and revokes one", async () => {
    vi.spyOn(api, "chatShares").mockResolvedValue([
      { ...LINK, view_count: 3, last_viewed_at: "2026-10-12T19:00:00Z" },
    ]);
    const revoke = vi.spyOn(api, "revokeChatShare").mockResolvedValue();
    const onChanged = vi.fn();
    render(<ChatShareSheet session={chat()} onClose={vi.fn()} onChanged={onChanged} />);
    expect(await screen.findByText(/Opened 3 times/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("l1"));
    expect(await screen.findByText("Link revoked")).toBeInTheDocument();
    expect(screen.queryByText(/Opened 3 times/)).not.toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("says so when minting fails", async () => {
    vi.spyOn(api, "chatShares").mockResolvedValue([]);
    vi.spyOn(api, "mintChatShare").mockRejectedValue(new Error("boom"));
    render(<ChatShareSheet session={chat()} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /Create share link/ }));
    expect(await screen.findByText(/Couldn’t create the link/)).toBeInTheDocument();
  });
});
