import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SubagentFan } from "./SubagentFan";
import type { SubagentFan as Fan, SubagentChild } from "./transcript";

function child(over: Partial<SubagentChild> & { childId: string }): SubagentChild {
  return { persona: "research", label: "L", depth: 1, phase: "queued", status: "running", ...over };
}

function fan(children: SubagentChild[], over: Partial<Fan> = {}): Fan {
  return { children, treeSpent: 0, treeBudget: 0, ...over };
}

describe("SubagentFan", () => {
  it("renders a row per child with neutral persona tags and state status words", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({ childId: "k1", label: "Pricing", phase: "researching", status: "running" }),
          child({
            childId: "k2",
            label: "Security",
            persona: "research",
            status: "failed",
            phase: "error",
          }),
          child({
            childId: "k3",
            label: "Cross-check",
            persona: "review",
            status: "done",
            stopReason: "end_turn",
          }),
        ])}
      />,
    );
    expect(screen.getByText("Pricing")).toBeInTheDocument();
    expect(screen.getByText("researching")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    // Persona is a neutral tag (text), never a color class.
    expect(screen.getByText("review")).toBeInTheDocument();
    // Header counts running agents while live.
    expect(screen.getByText(/3 agents/)).toBeInTheDocument();
  });

  it("groups a staged (feeding-waves) fan by wave with live feed edges", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "p",
            label: "research",
            persona: "research",
            status: "done",
            stopReason: "end_turn",
            wave: 0,
            fedFrom: [],
          }),
          child({
            childId: "c1",
            label: "checklist",
            persona: "summarize",
            wave: 1,
            fedFrom: ["research"],
          }),
          child({
            childId: "c2",
            label: "critique",
            persona: "review",
            wave: 1,
            fedFrom: ["research"],
          }),
        ])}
      />,
    );
    // Wave dividers, the second naming its feed source — live, not only in the final card.
    expect(screen.getByText(/Wave 1 · research/)).toBeInTheDocument();
    expect(screen.getByText(/Wave 2 · summarize, review — fed by wave 1/)).toBeInTheDocument();
    // The feed edge renders as text on each fed consumer (both wave-2 children).
    expect(screen.getAllByText(/← fed by research/)).toHaveLength(2);
  });

  it("shows a child's live context fill as a per-row meter", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            usedTokens: 18_000,
            contextWindow: 131_072,
          }),
          // A child with no usage yet shows no meter (nothing to read).
          child({ childId: "k2", label: "Security", phase: "queued", status: "running" }),
        ])}
      />,
    );
    expect(screen.getByText("18k/131k")).toBeInTheDocument();
    expect(screen.getAllByText(/\/131k/)).toHaveLength(1);
  });

  it("rolls up done · ran · failed when all children have settled", () => {
    render(
      <SubagentFan
        running={false}
        fan={fan([
          child({ childId: "k1", status: "done" }),
          child({ childId: "k2", status: "failed" }),
        ])}
      />,
    );
    expect(screen.getByText(/done · 2 ran · 1 failed/)).toBeInTheDocument();
  });

  it("expands a child to show its summary", () => {
    render(
      <SubagentFan
        running={false}
        fan={fan([child({ childId: "k1", label: "Pricing", status: "done", summary: "3 tiers" })])}
      />,
    );
    expect(screen.queryByText("3 tiers")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Pricing"));
    expect(screen.getByText("3 tiers")).toBeInTheDocument();
  });

  it("shows a live step count on a running child", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            step: 4,
          }),
        ])}
      />,
    );
    expect(screen.getByText("researching · 4 steps")).toBeInTheDocument();
  });

  it("marks a minted-but-unstarted child as italic 'queued'", () => {
    render(
      <SubagentFan
        running
        fan={fan([child({ childId: "k1", label: "Pending", phase: "queued", status: "running" })])}
      />,
    );
    expect(screen.getByText("queued")).toHaveClass("queued");
  });

  it("shows a STATIC (non-animated) progress bar for a queued child", () => {
    const { container } = render(
      <SubagentFan
        running
        fan={fan([child({ childId: "k1", label: "Pending", phase: "queued", status: "running" })])}
      />,
    );
    // The bar carries `queued` (static fill), not `running` (the animated sweep).
    const bar = container.querySelector(".fb-sa-bar");
    expect(bar?.className).toContain("queued");
    expect(bar?.className.split(/\s+/)).not.toContain("running");
    // …and the glyph dots are `queued` (static), not `run` (the animated bounce).
    const glyph = container.querySelector(".fb-sa-g");
    expect(glyph?.className).toContain("queued");
    expect(glyph?.className.split(/\s+/)).not.toContain("run");
  });

  it("auto-collapses a child on settle, even if it was expanded while running", () => {
    const { rerender } = render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            liveTrace: [{ kind: "reasoning", text: "digging in" }],
          }),
        ])}
      />,
    );
    // Tap to mark it manually expanded while it streams; its trace is visible.
    fireEvent.click(screen.getByText("Pricing"));
    expect(screen.getByText("digging in")).toBeInTheDocument();
    // It settles → the row folds back on its own, despite the manual expand.
    rerender(
      <SubagentFan
        running={false}
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            status: "done",
            stopReason: "end_turn",
            summary: "final answer",
            liveTrace: [{ kind: "reasoning", text: "digging in" }],
          }),
        ])}
      />,
    );
    expect(screen.queryByText("digging in")).not.toBeInTheDocument();
    expect(screen.queryByText("final answer")).not.toBeInTheDocument();
  });

  it("folds a child's thinking once it starts answering, streaming the answer below", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            step: 3,
            liveTrace: [{ kind: "reasoning", text: "let me search" }],
            liveText: "found 3 tiers",
          }),
        ])}
      />,
    );
    // The answer streams, visible without a tap.
    expect(screen.getByText("found 3 tiers")).toBeInTheDocument();
    // Thinking is done → the trace folded to past-tense "Thought" and the reasoning is
    // hidden (the answer took its place)…
    expect(screen.getByText(/Thought/)).toBeInTheDocument();
    expect(screen.queryByText("let me search")).not.toBeInTheDocument();
    // …but the folded thinking is still there to re-open by hand.
    fireEvent.click(screen.getByText(/Thought/));
    expect(screen.getByText("let me search")).toBeInTheDocument();
  });

  it("injects the child's tool calls inline in its trace, interleaved with reasoning", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            liveTrace: [
              { kind: "reasoning", text: "checking the local paper" },
              { kind: "tool", name: "web_search", arg: "port saint john news", ok: true },
              { kind: "tool", name: "web_fetch", arg: "https://floridatoday.test", ok: false },
            ],
          }),
        ])}
      />,
    );
    // The reasoning and both tool calls render together in the one trace.
    expect(screen.getByText("checking the local paper")).toBeInTheDocument();
    expect(screen.getByText("search")).toBeInTheDocument();
    expect(screen.getByText("port saint john news")).toBeInTheDocument();
    expect(screen.getByText("fetch")).toBeInTheDocument();
    // The toggle summarises how many tools are folded inside.
    expect(screen.getByText(/2 tools/)).toBeInTheDocument();
  });

  it("lets the live trace collapse (folding away heavy tool use)", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            liveTrace: [
              { kind: "reasoning", text: "deep thoughts" },
              { kind: "tool", name: "web_search", arg: "q", ok: true },
            ],
            // No answer yet — still thinking, so the trace stays open by default.
          }),
        ])}
      />,
    );
    // Open by default while still thinking — both the reasoning and the tool show…
    expect(screen.getByText("deep thoughts")).toBeInTheDocument();
    expect(screen.getByText("q")).toBeInTheDocument();
    // …and one toggle collapses the whole trace, tools and all.
    fireEvent.click(screen.getByText(/Thinking/));
    expect(screen.queryByText("deep thoughts")).not.toBeInTheDocument();
    expect(screen.queryByText("q")).not.toBeInTheDocument();
  });

  it("does not offer 'Open session' for a still-running child (would be blank)", () => {
    const onOpen = vi.fn();
    render(
      <SubagentFan
        running
        onOpen={onOpen}
        fan={fan([
          child({
            childId: "k1",
            label: "Pricing",
            phase: "researching",
            status: "running",
            liveText: "working",
          }),
        ])}
      />,
    );
    // The row is expanded (streaming) and shows live text, but no open-session link yet.
    expect(screen.getByText("working")).toBeInTheDocument();
    expect(screen.queryByText(/Open sub-agent session/)).not.toBeInTheDocument();
  });

  it("opens a child's own session from its expanded row", () => {
    const onOpen = vi.fn();
    render(
      <SubagentFan
        running={false}
        onOpen={onOpen}
        fan={fan([child({ childId: "k1", label: "Pricing", status: "done", summary: "3 tiers" })])}
      />,
    );
    fireEvent.click(screen.getByText("Pricing"));
    fireEvent.click(screen.getByText(/Open sub-agent session/));
    expect(onOpen).toHaveBeenCalledWith("k1");
  });

  it("auto-expands a failed child's error without a click", () => {
    render(
      <SubagentFan
        running={false}
        fan={fan([
          child({
            childId: "k1",
            label: "Security",
            status: "failed",
            phase: "error",
            summary: "ERROR: web_fetch timed out",
          }),
        ])}
      />,
    );
    // Visible immediately — no tap needed.
    expect(screen.getByText("ERROR: web_fetch timed out")).toBeInTheDocument();
  });

  it("labels a cancelled child 'cancelled'", () => {
    render(
      <SubagentFan
        running={false}
        fan={fan([child({ childId: "k1", status: "failed", stopReason: "cancelled" })])}
      />,
    );
    expect(screen.getByText("cancelled")).toBeInTheDocument();
  });

  it("shows a budget meter that goes danger at the ceiling", () => {
    render(
      <SubagentFan
        running
        fan={fan([child({ childId: "k1" })], { treeSpent: 1200, treeBudget: 1200 })}
      />,
    );
    const meter = screen.getByRole("meter");
    expect(meter).toHaveClass("danger");
    expect(screen.getByText("budget exhausted")).toBeInTheDocument();
  });

  it("shows a cascade Stop only while running and fires onStop", () => {
    const onStop = vi.fn();
    const { rerender } = render(
      <SubagentFan running fan={fan([child({ childId: "k1" })])} onStop={onStop} />,
    );
    fireEvent.click(screen.getByText("■ Stop"));
    expect(onStop).toHaveBeenCalledOnce();
    // Once settled the Stop is gone.
    rerender(
      <SubagentFan
        running={false}
        fan={fan([child({ childId: "k1", status: "done" })])}
        onStop={onStop}
      />,
    );
    expect(screen.queryByText("■ Stop")).not.toBeInTheDocument();
  });

  it("contains a long fan behind 'show N more'", () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      child({ childId: `k${i}`, label: `Child ${i}` }),
    );
    render(<SubagentFan running fan={fan(many)} />);
    expect(screen.queryByText("Child 11")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("show 4 more"));
    expect(screen.getByText("Child 11")).toBeInTheDocument();
  });
});
