// The computation popover (SHOW_THE_WORKING_PLAN.md W3, G). What is worth pinning is the
// shape the owner asked for after seeing it on the box: it opens ON the working, and it
// does not say the same thing twice on the way there.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ComputationPopover } from "./ComputationPopover";

const anchor = { left: 40, top: 200, bottom: 214, width: 12, height: 14 } as DOMRect;

function target(data: Record<string, unknown>) {
  return { payload: { view: "code_run", surface: "inline" as const, data, refs: [] } };
}

describe("ComputationPopover", () => {
  it("opens on the working — no second tap to reach it", () => {
    render(
      <ComputationPopover
        target={target({ language: "expression", code: "0.1 + 0.2", result: "3/10" })}
        anchor={anchor}
        onClose={vi.fn()}
      />,
    );
    // The body is there from the first render: the labelled rungs, not a summary of them.
    expect(screen.getByText("expression")).toBeTruthy();
    expect(screen.getByText("3/10")).toBeTruthy();
    // ...and the tap that used to be required is gone.
    expect(screen.queryByText("show the working")).toBeNull();
  });

  it("states the expression and the answer ONCE", () => {
    // The collapsed head restated both, and on an always-open panel that read as the
    // view repeating itself — the same duplication the `code_run` step had against its
    // own prose.
    const { container } = render(
      <ComputationPopover
        target={target({ language: "expression", code: "0.1 + 0.2", result: "3/10" })}
        anchor={anchor}
        onClose={vi.fn()}
      />,
    );
    const text = container.textContent ?? "";
    expect(text.split("0.1 + 0.2")).toHaveLength(2);
    expect(text.split("3/10")).toHaveLength(2);
  });

  it("serves a python run from the same panel", () => {
    // One marker namespace, one panel: `run_python` is cited exactly as `calculate` is,
    // so a program's answer is as checkable as an expression's.
    render(
      <ComputationPopover
        target={target({
          language: "python",
          code: "primes = [n for n in range(500)]\nlen(primes)",
          result: "95",
          ok: true,
          containment: ["no network", "scratch only"],
        })}
        anchor={anchor}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("code")).toBeTruthy();
    expect(screen.getByText("95")).toBeTruthy();
    expect(screen.getByText("no network · scratch only")).toBeTruthy();
  });
});
