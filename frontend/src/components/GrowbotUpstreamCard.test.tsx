import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotUpstream } from "../api/client";
import { GrowbotUpstreamCard } from "./GrowbotUpstreamCard";

const PIN = "4a2cfc29ea5e79a2f1d5e9df30a602729d67b71b";
const NEW = "9e41b07c22d1a0e5b0c3d4f5a6b7c8d9e0f1a2b3";

function upstream(over: Partial<GrowbotUpstream> = {}): GrowbotUpstream {
  return {
    state: "current",
    repo_url: "https://github.com/britcruise9/GrowBot",
    pinned: PIN,
    head: PIN,
    head_at: "2026-10-13T10:00:00Z",
    ahead_by: 0,
    commits: [],
    policies: [],
    other_files: 0,
    compare_url: "",
    checked_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    error: "",
    ...over,
  };
}

const BEHIND = upstream({
  state: "behind",
  head: NEW,
  head_at: "2026-11-09T10:00:00Z",
  ahead_by: 2,
  commits: [
    { sha: NEW, title: "normal-spin: retune cw", date: "2026-11-09T10:00:00Z" },
    { sha: "a".repeat(40), title: "policies: diy-v2 walk", date: "" },
  ],
  policies: [
    { path: "app/policies/diy-v2/walk_fwd.json", change: "new", previous_path: null },
    { path: "app/policy_drrma.json", change: "removed", previous_path: null },
    { path: "policy/forward/forward_90mm.json", change: "renamed", previous_path: "x" },
  ],
  other_files: 1,
  compare_url: "https://github.com/britcruise9/GrowBot/compare/4a2cfc29ea5e...9e41b07c22d1",
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function serve(first: GrowbotUpstream | null, checked?: GrowbotUpstream | Error) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      calls.push(`${(init?.method ?? "GET").toUpperCase()} ${path}`);
      if (path === "/api/growbot/upstream") return first ? json(first) : json({}, 404);
      if (path === "/api/growbot/upstream/check") {
        if (checked instanceof Error) return json({ detail: checked.message }, 503);
        return json(checked ?? first);
      }
      return json({}, 404);
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GrowbotUpstreamCard", () => {
  it("is quiet when the box matches upstream", async () => {
    serve(upstream());
    render(<GrowbotUpstreamCard />);
    const card = await screen.findByRole("region", { name: "Walks and spins" });
    expect(within(card).getByText("Up to date")).toBeInTheDocument();
    expect(card).toHaveTextContent("same walks and spins as the official GrowBot repo");
    expect(within(card).getByText("4a2cfc2")).toBeInTheDocument();
    expect(within(card).getByText("2 h ago")).toBeInTheDocument();
    expect(within(card).queryByText("Upstream")).toBeNull();
    expect(within(card).queryByRole("link", { name: "See on GitHub" })).toBeNull();
  });

  it("lists new policies, the commits, how they come in and the GitHub diff", async () => {
    serve(BEHIND);
    render(<GrowbotUpstreamCard />);
    const card = await screen.findByRole("region", { name: "Walks and spins" });
    expect(within(card).getByText("3 new or changed")).toBeInTheDocument();
    expect(within(card).getByText("diy-v2/walk_fwd.json")).toBeInTheDocument();
    expect(within(card).getByText("policy_drrma.json")).toBeInTheDocument();
    expect(within(card).getByText("policy/forward/forward_90mm.json")).toBeInTheDocument();
    expect(within(card).getByText("removed")).toHaveClass("gb-tag", "bad");
    expect(within(card).getByText("2 commits since your copy, 1 other file")).toBeInTheDocument();
    expect(within(card).getByText("normal-spin: retune cw")).toBeInTheDocument();
    expect(card).toHaveTextContent('ask Claude to "sync GrowBot to upstream"');
    expect(card).toHaveTextContent("Ops → Update");
    expect(within(card).getByText(/^9e41b07 · /)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "See on GitHub" })).toHaveAttribute(
      "href",
      BEHIND.compare_url,
    );
  });

  it("says when upstream moved but GitHub would not list the files", async () => {
    serve({ ...BEHIND, policies: null, error: "GitHub wouldn't list the changed files." });
    render(<GrowbotUpstreamCard />);
    const card = await screen.findByRole("region", { name: "Walks and spins" });
    expect(within(card).getByText("Upstream has moved")).toBeInTheDocument();
    expect(within(card).getByText("GitHub wouldn't list the changed files.")).toBeTruthy();
    expect(card).not.toHaveTextContent("sync GrowBot");
  });

  it("says when upstream moved without new policies", async () => {
    serve({ ...BEHIND, policies: [], ahead_by: null, other_files: 0 });
    render(<GrowbotUpstreamCard />);
    const card = await screen.findByRole("region", { name: "Walks and spins" });
    expect(within(card).getByText("No new policies")).toBeInTheDocument();
    expect(card).toHaveTextContent("published no new walks or spins");
    expect(within(card).getByText("2+ commits since your copy")).toBeInTheDocument();
  });

  it("names a failed check and one that never ran", async () => {
    serve(upstream({ state: "unknown", head: null, error: "GitHub didn't answer." }));
    const { unmount } = render(<GrowbotUpstreamCard />);
    expect(await screen.findByText("Couldn't check")).toBeInTheDocument();
    expect(screen.getByText("GitHub didn't answer.")).toBeInTheDocument();
    unmount();
    serve(upstream({ state: "unknown", pinned: null, head: null, checked_at: null }));
    render(<GrowbotUpstreamCard />);
    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
    expect(screen.getByText("not yet")).toBeInTheDocument();
    expect(screen.queryByText("Your copy")).toBeNull();
  });

  it("checks now and shows the answer", async () => {
    const calls = serve(upstream(), BEHIND);
    render(<GrowbotUpstreamCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Check now" }));
    expect(await screen.findByText("3 new or changed")).toBeInTheDocument();
    expect(calls).toEqual(["GET /api/growbot/upstream", "POST /api/growbot/upstream/check"]);
  });

  it("shows why a check failed and lets it be tried again", async () => {
    serve(upstream(), new Error("the upstream check is not running"));
    render(<GrowbotUpstreamCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Check now" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Check now" })).toBeEnabled());
    expect(screen.getByText(/not running|503/)).toBeInTheDocument();
    expect(screen.getByText("Up to date")).toBeInTheDocument();
  });

  it("says when the box could not be asked, rather than hiding", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "the upstream check is not running" }, 503)),
    );
    render(<GrowbotUpstreamCard />);
    expect(await screen.findByText("Couldn't check")).toBeInTheDocument();
    expect(screen.getByText(/not running|503/)).toBeInTheDocument();
  });

  it("keeps a never-checked box neutral and counts commits it could not total", async () => {
    serve({
      ...BEHIND,
      ahead_by: null,
      policies: null,
      compare_url: "javascript:alert(1)",
    });
    render(<GrowbotUpstreamCard />);
    expect(await screen.findByText("2+ commits since your copy, 1 other file")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "See on GitHub" })).toBeNull();
  });

  it("gives a box that has never checked a plain dot", async () => {
    serve(upstream({ state: "unknown", head: null, checked_at: null }));
    const { container } = render(<GrowbotUpstreamCard />);
    expect(await screen.findByText("Not checked yet")).not.toHaveClass("gb-bad-t");
    expect(container.querySelector(".gb-dot")?.className).toBe("gb-dot ");
  });

  it("stays away on a box without the check", async () => {
    const calls = serve(null);
    const { container } = render(<GrowbotUpstreamCard />);
    await waitFor(() => expect(calls).toHaveLength(1));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
