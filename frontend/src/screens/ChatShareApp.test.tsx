import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type SharedChat, api, faviconUrl, setFaviconSource } from "../api/client";
import { ChatShareApp } from "./ChatShareApp";

// The app reads the live location; pin the share token so the boot path runs.
const share = vi.hoisted(() => ({ token: "tok" as string | null }));
vi.mock("../agent/chatShare", async (orig) => ({
  ...(await orig<typeof import("../agent/chatShare")>()),
  parseChatSharePath: () => share.token,
}));

const CHAT: SharedChat = {
  title: "SpaceX Pretrade",
  agent: "jerv",
  last_active_at: "2026-10-12T17:47:00Z",
  turns: [
    {
      role: "user",
      content: "Where are shares trading?",
      tools: [],
      reasoning: "",
      attachments: [],
      elapsed_ms: null,
    },
    {
      role: "assistant",
      content: "Above the last tender.",
      tools: [
        {
          id: "t1",
          name: "web_search",
          ok: true,
          args: { query: "spacex secondary" },
          web_sources: [{ url: "https://www.cnbc.com/x", title: "CNBC" }],
        },
        {
          id: "t2",
          name: "generate_image",
          ok: true,
          view: { view: "generated_image", surface: "bubble", data: { image_id: "g1" } },
        },
      ],
      reasoning: "search first",
      attachments: [],
      elapsed_ms: 41_000,
    },
  ],
} as unknown as SharedChat;

describe("ChatShareApp", () => {
  beforeEach(() => {
    share.token = "tok";
    setFaviconSource(null);
    vi.restoreAllMocks();
  });

  it("renders the shared chat through the app's own transcript", async () => {
    vi.spyOn(api, "chatShareView").mockResolvedValue(CHAT);
    render(<ChatShareApp />);
    expect(await screen.findByRole("heading", { name: "SpaceX Pretrade" })).toBeInTheDocument();
    expect(screen.getByText("Research")).toBeInTheDocument();
    expect(screen.getByText("1 turn")).toBeInTheDocument();
    expect(screen.getByText("Where are shares trading?")).toBeInTheDocument();
    expect(screen.getByText("Above the last tender.")).toBeInTheDocument();
    // The same Thought / Worked disclosures the owner sees.
    expect(document.querySelector(".fb-act-think")).not.toBeNull();
    expect(document.querySelector(".fb-act-work")).not.toBeNull();
    expect(screen.getByText(/41s/)).toBeInTheDocument();
    // An owner-only image is a quiet placeholder, not a broken request.
    expect(screen.getByText("Image not included in shared chats")).toBeInTheDocument();
    expect(document.querySelector('img[src*="/api/images/generated"]')).toBeNull();
    // Nothing on the page can send.
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("leaves the owner's staged proposals off the shared page", async () => {
    const turns = CHAT.turns.map((t, i) =>
      i === 1
        ? {
            ...t,
            tools: [
              {
                id: "p",
                name: "propose",
                ok: true,
                proposal: { proposal_id: "pr1", kind: "fact" },
              },
            ],
          }
        : t,
    );
    vi.spyOn(api, "chatShareView").mockResolvedValue({ ...CHAT, turns } as SharedChat);
    const getProposal = vi.spyOn(api, "getProposal");
    render(<ChatShareApp />);
    await screen.findByText("Above the last tender.");
    expect(screen.queryByText(/Review proposal/)).not.toBeInTheDocument();
    expect(getProposal).not.toHaveBeenCalled();
  });

  it("serves favicons from the share's own route", async () => {
    vi.spyOn(api, "chatShareView").mockResolvedValue(CHAT);
    render(<ChatShareApp />);
    await screen.findByRole("heading", { name: "SpaceX Pretrade" });
    expect(faviconUrl("cnbc.com")).toBe("/api/chat-share/tok/favicon?host=cnbc.com");
  });

  it("says a revoked or unknown link no longer works", async () => {
    vi.spyOn(api, "chatShareView").mockRejectedValue(new ApiError(404, "gone"));
    render(<ChatShareApp />);
    expect(await screen.findByText("This link no longer works")).toBeInTheDocument();
  });

  it("tells a failed load apart from a dead link", async () => {
    vi.spyOn(api, "chatShareView").mockRejectedValue(new Error("offline"));
    render(<ChatShareApp />);
    expect(await screen.findByText("Couldn’t load this chat")).toBeInTheDocument();
  });

  it("treats a missing token as a dead link", async () => {
    share.token = null;
    render(<ChatShareApp />);
    expect(await screen.findByText("This link no longer works")).toBeInTheDocument();
  });

  it("reloads the page for newer turns", async () => {
    vi.spyOn(api, "chatShareView").mockResolvedValue(CHAT);
    const reload = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      reload,
    } as Location);
    render(<ChatShareApp />);
    fireEvent.click(await screen.findByRole("button", { name: /Reload for newer turns/ }));
    expect(reload).toHaveBeenCalled();
  });
});
