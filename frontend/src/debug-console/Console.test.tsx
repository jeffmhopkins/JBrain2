import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Console } from "./Console";

describe("Console", () => {
  beforeEach(() => {
    localStorage.clear();
    window.location.hash = "";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(): { calls: string[] } {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        calls.push(url);
        if (url.includes("/api/debug/whoami"))
          return new Response(JSON.stringify({ label: "Test", scopes: ["sql.read"] }), {
            status: 200,
          });
        if (url.includes("/api/debug/activity"))
          return new Response(JSON.stringify({ events: [], last: 0 }), { status: 200 });
        if (url.includes("/api/debug/suspend-self")) return new Response(null, { status: 204 });
        return new Response("{}", { status: 200 });
      }),
    );
    return { calls };
  }

  it("auto-connects from a cached token using same-origin (relative) calls", async () => {
    localStorage.setItem(
      "jbrain.debug.token",
      JSON.stringify({ base: "https://pub.example.com", key: "K" }),
    );
    const { calls } = stubFetch();
    render(<Console />);

    // Connected: the token label from whoami renders in the header.
    expect(await screen.findByText("Test")).toBeInTheDocument();
    // The call target is a relative path, NOT the token's embedded public host —
    // that is what lets the LAN-only console work over jbrain.local.
    expect(calls).toContain("/api/debug/whoami");
    expect(calls.every((u) => u.startsWith("/api/"))).toBe(true);
  });

  it("suspends the token via suspend-self and shows the paused banner", async () => {
    localStorage.setItem(
      "jbrain.debug.token",
      JSON.stringify({ base: "https://pub.example.com", key: "K" }),
    );
    const { calls } = stubFetch();
    render(<Console />);
    fireEvent.click(await screen.findByRole("button", { name: /Suspend/ }));

    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/debug/suspend-self"))).toBe(true),
    );
    expect(await screen.findByText(/Token suspended/)).toBeInTheDocument();
  });
});
