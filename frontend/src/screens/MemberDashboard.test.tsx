import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MemberSubject, Principal } from "../api/client";
import { MemberDashboard, type MemberDeps, lastSeen } from "./MemberDashboard";

function principal(over: Partial<Principal> = {}): Principal {
  return { principal_id: "p1", kind: "device_key", label: "Alice", ...over };
}

function subject(over: Partial<MemberSubject> = {}): MemberSubject {
  return {
    subject_id: "s1",
    label: "Bob",
    last_seen: new Date(Date.now() - 5 * 60_000).toISOString(),
    battery_pct: 72,
    connection: "wifi",
    ...over,
  };
}

function deps(over: Partial<MemberDeps> = {}): MemberDeps {
  return {
    probe: vi.fn(async () => principal()),
    listRoster: vi.fn(async () => [subject()]),
    ...over,
  };
}

describe("MemberDashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("unlocks on a device-key session and lands on the Devices roster", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tablist", { name: "Dashboard views" });
    expect(screen.getByText("JBrain360")).toBeInTheDocument();
    // The roster card renders the member's label + activity.
    await screen.findByText("Bob");
    expect(screen.getByText(/5m ago/)).toBeInTheDocument();
    expect(screen.getByText(/72%/)).toBeInTheDocument();
  });

  it("locks when there is no session (probe 401)", async () => {
    const probe = vi.fn(async () => {
      throw new Error("401");
    });
    render(<MemberDashboard deps={deps({ probe })} />);
    await screen.findByText(/not signed in/);
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("locks an owner (non-member) session — the owner belongs on the main app", async () => {
    render(
      <MemberDashboard deps={deps({ probe: vi.fn(async () => principal({ kind: "owner" })) })} />,
    );
    await screen.findByText(/not signed in/);
  });

  it("switches to the Timeline tab", async () => {
    render(<MemberDashboard deps={deps()} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Timeline" }));
    expect(await screen.findByText(/timeline arrives/)).toBeInTheDocument();
  });

  it("shows an empty roster state", async () => {
    render(<MemberDashboard deps={deps({ listRoster: vi.fn(async () => []) })} />);
    expect(await screen.findByText(/no one to show yet/)).toBeInTheDocument();
  });
});

describe("lastSeen", () => {
  it("formats freshness, never an exact position", () => {
    expect(lastSeen(null)).toBe("no fixes yet");
    expect(lastSeen(new Date(Date.now() - 10_000).toISOString())).toBe("just now");
    expect(lastSeen(new Date(Date.now() - 20 * 60_000).toISOString())).toBe("20m ago");
    expect(lastSeen(new Date(Date.now() - 3 * 3_600_000).toISOString())).toBe("3h ago");
    expect(lastSeen(new Date(Date.now() - 2 * 86_400_000).toISOString())).toBe("2d ago");
  });
});
