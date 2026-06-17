import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WikiTalkOut } from "../api/client";
import { TalkScreen } from "./TalkScreen";

// A board carrying every structural element: an open discussion (auto-expanded first), a resolved
// discussion with an Editor post + outcome chip, and the auto Build-log with rev'd builder posts.
const BOARD: WikiTalkOut = {
  title: "Celine Hopkins",
  topics: [
    {
      id: "t-globex",
      kind: "discussion",
      title: "Outdated: still says she works at Globex",
      status: "open",
      meta: null,
      posts: [
        {
          id: "p1",
          author: "owner",
          body: "She left Globex in March.",
          source: null,
          outcome: null,
          created_at: "2026-06-17T09:14:00Z",
          rev: null,
        },
      ],
    },
    {
      id: "t-addr",
      kind: "discussion",
      title: "Drop the old apartment address?",
      status: "resolved",
      meta: null,
      posts: [
        {
          id: "p2",
          author: "editor",
          body: "Excluded that note from this article.",
          source: {
            note_id: "n-9",
            meta: "Note · Mar 12, 2026",
            snippet: "moved out of the Boulder place",
            domain: "general",
          },
          outcome: "source excluded · rebuilt",
          created_at: "2026-03-12T10:01:00Z",
          rev: null,
        },
      ],
    },
    {
      id: "t-log",
      kind: "build_log",
      title: "Build log",
      status: "open",
      meta: "auto · 2 entries",
      posts: [
        {
          id: "l1",
          author: "builder",
          body: "Created article (Person guide); 11 facts across 3 domains.",
          source: null,
          outcome: null,
          created_at: "2026-03-02T02:11:00Z",
          rev: 1,
        },
        {
          id: "l2",
          author: "builder",
          body: "Rebuilt article (Person guide); 12 facts across 3 domains.",
          source: null,
          outcome: null,
          created_at: "2026-03-17T02:14:00Z",
          rev: 2,
        },
      ],
    },
  ],
};

// A freshly-built article: only the auto Build-log, no discussion yet.
const EMPTY: WikiTalkOut = {
  title: "Fresh",
  topics: BOARD.topics.filter((t) => t.kind === "build_log"),
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("TalkScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const handlers = { onClose: vi.fn(), onOpenArticle: vi.fn() };

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/api/wiki/celine/talk") return jsonResponse(BOARD);
      if (url === "/api/wiki/celine/talk/topics" && method === "POST") {
        const { title, body } = JSON.parse(String(init?.body));
        return jsonResponse(
          {
            id: "t-new",
            kind: "discussion",
            title,
            status: "open",
            meta: null,
            posts: [
              {
                id: "pn",
                author: "owner",
                body,
                source: null,
                outcome: null,
                created_at: "2026-06-17T12:00:00Z",
                rev: null,
              },
            ],
          },
          201,
        );
      }
      if (url === "/api/wiki/celine/talk/topics/t-globex/posts" && method === "POST") {
        const { body } = JSON.parse(String(init?.body));
        return jsonResponse(
          {
            id: "pr",
            author: "owner",
            body,
            source: null,
            outcome: null,
            created_at: "2026-06-17T12:01:00Z",
            rev: null,
          },
          201,
        );
      }
      if (url === "/api/wiki/celine/talk/topics/t-globex" && method === "PATCH") {
        return jsonResponse({ id: "t-globex", status: "resolved" });
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function setup(articleId = "celine") {
    render(<TalkScreen articleId={articleId} syncStatus="synced" {...handlers} />);
  }

  it("renders topics, status badges, and the build-log meta", async () => {
    setup();
    expect(await screen.findByText("Outdated: still says she works at Globex")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("resolved")).toBeInTheDocument();
    expect(screen.getByText("auto · 2 entries")).toBeInTheDocument();
    // The first topic is auto-expanded → its owner post (signed "You") shows.
    expect(screen.getByText("She left Globex in March.")).toBeInTheDocument();
  });

  it("expands the build log to show rev'd builder posts", async () => {
    setup();
    await screen.findByText("Build log");
    fireEvent.click(screen.getByText("Build log"));
    const post = screen.getByText(/Created article \(Person guide\)/);
    expect(post).toBeInTheDocument();
    // The signature derives "rev N" from the Build-log post order.
    expect(
      within(post.closest(".talk-reply") as HTMLElement).getByText(/rev 1/),
    ).toBeInTheDocument();
  });

  it("renders an Editor post's source card and outcome chip", async () => {
    setup();
    await screen.findByText("Drop the old apartment address?");
    fireEvent.click(screen.getByText("Drop the old apartment address?"));
    expect(screen.getByText(/moved out of the Boulder place/)).toBeInTheDocument();
    expect(screen.getByText("source excluded · rebuilt")).toBeInTheDocument();
  });

  it("files a new topic and shows it", async () => {
    setup();
    await screen.findByText("Outdated: still says she works at Globex");
    fireEvent.click(screen.getByRole("button", { name: /New topic/ }));
    fireEvent.change(screen.getByLabelText("Topic title"), {
      target: { value: "Wrong birthplace" },
    });
    fireEvent.change(screen.getByLabelText(/editorial issue/), {
      target: { value: "She was born in Denver, not Boulder." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create topic" }));
    expect(await screen.findByText("Wrong birthplace")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([u, init]) =>
        String(u) === "/api/wiki/celine/talk/topics" && (init as RequestInit)?.method === "POST",
    );
    expect(post).toBeTruthy();
  });

  it("posts a reply to an open topic", async () => {
    setup();
    await screen.findByText("She left Globex in March.");
    fireEvent.change(screen.getByLabelText(/Reply to Outdated/), {
      target: { value: "Please fix the Career section." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Post" }));
    expect(await screen.findByText("Please fix the Career section.")).toBeInTheDocument();
  });

  it("resolves an open topic", async () => {
    setup();
    await screen.findByText("She left Globex in March.");
    fireEvent.click(screen.getByRole("button", { name: "Mark resolved" }));
    // The PATCH flips the badge; the resolve control becomes "Reopen".
    expect(await screen.findByRole("button", { name: "Reopen" })).toBeInTheDocument();
  });

  it("opens the article from the Talk topbar affordance", async () => {
    setup();
    await screen.findByText("Outdated: still says she works at Globex");
    fireEvent.click(screen.getByRole("button", { name: "Open article" }));
    expect(handlers.onOpenArticle).toHaveBeenCalledWith("celine");
  });

  it("renders an empty board (Build-log only, no discussion)", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(EMPTY));
    setup("fresh");
    expect(await screen.findByText("Build log")).toBeInTheDocument();
    expect(screen.queryByText(/Globex/)).not.toBeInTheDocument();
  });

  it("shows the quiet error line when the board fails to load", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));
    setup();
    expect(
      await screen.findByText("couldn't load the discussion — reopen to retry."),
    ).toBeInTheDocument();
  });
});
