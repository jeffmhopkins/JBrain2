import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type FollowRunning, GrowbotFollow } from "./GrowbotFollow";

// Follow: the face follower from the Body tab, sent as a skill set's look, with its legs-off dry
// run and the camera's two settings, all without a terminal.

const SKILLS = {
  look: "11111111-1111-4111-8111-111111111111",
  forward: "22222222-2222-4222-8222-222222222222",
};

function serve(move: unknown = { ok: true, state: "accepted", why: "" }) {
  const calls: unknown[] = [];
  vi.stubGlobal("fetch", async (_input: RequestInfo | URL, init?: RequestInit) => {
    calls.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify(move), { headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

// The remote holds what is running, so a follow outlives its sheet; this stands in for it.
function Holder(props: Omit<Parameters<typeof GrowbotFollow>[0], "running" | "setRunning">) {
  const [running, setRunning] = useState<FollowRunning>(null);
  return <GrowbotFollow {...props} running={running} setRunning={setRunning} />;
}

function follow() {
  const props = { code: "gb-3fa91c", skills: SKILLS, disabled: false, onSaid: vi.fn() };
  return { props, view: render(<Holder {...props} />) };
}

describe("GrowbotFollow", () => {
  it("follows with the skill set, and stops with a plain stop", async () => {
    const calls = serve();
    const { props } = follow();
    fireEvent.click(screen.getByRole("button", { name: "Follow me" }));
    await screen.findByRole("button", { name: "Stop following" });
    expect(calls[0]).toEqual({ move: "look", secs: 0, skills: SKILLS, follow: "walk" });
    fireEvent.click(screen.getByRole("button", { name: "Stop following" }));
    await waitFor(() => expect(calls[1]).toEqual({ move: "stop", secs: 0 }));
    await screen.findByRole("button", { name: "Follow me" });
    expect(props.onSaid).toHaveBeenLastCalledWith({ text: "Stopped following.", bad: false });
  });

  it("runs a dry run, and sends the camera's settings", async () => {
    const calls = serve();
    follow();
    fireEvent.click(screen.getByRole("button", { name: "Dry run" }));
    await screen.findByRole("button", { name: "Stop dry run" });
    fireEvent.click(screen.getByRole("button", { name: "Measure 1 m" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Flip frames" }));
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls.map((c) => (c as { follow: string }).follow)).toEqual([
      "dry",
      "calibrate",
      "flip",
    ]);
  });

  it("says why when the creature can't", async () => {
    serve({ ok: false, state: "", why: "I can't see a face to measure" });
    const { props } = follow();
    fireEvent.click(screen.getByRole("button", { name: "Measure 1 m" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenCalledWith({
        text: "I can't see a face to measure",
        bad: true,
      }),
    );
    expect(screen.getByRole("button", { name: "Follow me" })).toBeTruthy();
  });

  it("keeps Stop following when the creature didn't stop", async () => {
    serve();
    const { props } = follow();
    fireEvent.click(screen.getByRole("button", { name: "Follow me" }));
    await screen.findByRole("button", { name: "Stop following" });
    serve({ ok: false, state: "", why: "the creature has not loaded its legs yet" });
    fireEvent.click(screen.getByRole("button", { name: "Stop following" }));
    await waitFor(() =>
      expect(props.onSaid).toHaveBeenLastCalledWith({
        text: "the creature has not loaded its legs yet",
        bad: true,
      }),
    );
    expect(screen.getByRole("button", { name: "Stop following" })).toBeTruthy();
  });
});
