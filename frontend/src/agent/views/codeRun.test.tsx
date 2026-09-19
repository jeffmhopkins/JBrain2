// The `code_run` view. Two things are worth testing and they are both about the boundary:
// the component decides the colours, and model text reaches the DOM as TEXT.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolView } from "./registry";

function view(data: Record<string, unknown>) {
  return { view: "code_run", surface: "inline" as const, data, refs: [] };
}

describe("code_run", () => {
  it("renders the code, its output and its result", () => {
    const { container } = render(
      <ToolView
        payload={view({
          language: "python",
          code: "total = 2847 - 2633\ntotal",
          stdout: "214\n",
          result: "214",
          ok: true,
          duration_ms: 44,
          containment: ["no network", "scratch only"],
        })}
      />,
    );
    expect(container.textContent).toContain("total = 2847 - 2633");
    expect(container.textContent).toContain("214");
    expect(screen.getByText(/ran clean/)).toBeTruthy();
    expect(screen.getByText("no network · scratch only")).toBeTruthy();
  });

  it("colours the code from a closed token set the COMPONENT owns", () => {
    const { container } = render(
      <ToolView payload={view({ language: "python", code: "for x in range(3):\n    pass" })} />,
    );
    // `for`/`in`/`pass` are keywords, `range` is a call — classes, never colours, so the
    // theme owns the palette.
    const classes = [...container.querySelectorAll("pre.fb-code span")].map((el) => el.className);
    expect(classes).toContain("k");
    expect(classes).toContain("f");
  });

  it("a snippet cannot smuggle markup into the transcript", () => {
    // The whole reason the highlighter tokenizes text instead of emitting HTML. If this
    // ever renders as markup, model output is authoring the DOM.
    const hostile = '<span class="k">not a keyword</span><img src=x onerror=1>';
    const { container } = render(
      <ToolView payload={view({ language: "python", code: hostile, stdout: hostile })} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=1>");
  });

  it("does not highlight a program's own output", () => {
    // Colouring stdout would let a snippet print something that reads as syntax.
    const { container } = render(
      <ToolView payload={view({ language: "python", code: "", stdout: "for in range def" })} />,
    );
    expect(container.querySelectorAll("pre.fb-code.out span")).toHaveLength(0);
  });

  it("says a call RAISED without calling it wrong", () => {
    // `ok` is whether the code ran, not whether the answer was right.
    render(
      <ToolView payload={view({ code: "1/0", ok: false, error: "ZeroDivisionError (line 1)" })} />,
    );
    expect(screen.getByText("raised")).toBeTruthy();
    expect(screen.getByText("ZeroDivisionError (line 1)")).toBeTruthy();
  });

  it("serves `calculate` from the same component, with its exact/decimal pair", () => {
    const { container } = render(
      <ToolView
        payload={view({
          language: "expression",
          code: "1/3",
          result: "1/3",
          decimal: "0.333333333333333",
          ok: true,
          containment: ["exact arithmetic", "no code executed"],
        })}
      />,
    );
    expect(screen.getByText("expression")).toBeTruthy();
    expect(screen.getByText("0.333333333333333")).toBeTruthy();
    // `calculate` never reaches the sandbox, so it must not borrow the sandbox's chips.
    expect(container.textContent).toContain("exact arithmetic · no code executed");
    expect(container.textContent).not.toContain("no network");
  });

  it("omits a decimal that only repeats the exact value", () => {
    render(
      <ToolView
        payload={view({ language: "expression", code: "2+2", result: "4", decimal: "4" })}
      />,
    );
    expect(screen.queryByText("decimal")).toBeNull();
  });

  it("says when the output was cut short", () => {
    // A fragment shown as the whole output is how a model draws a conclusion from half a run.
    render(<ToolView payload={view({ code: "x", stdout: "...", truncated: true })} />);
    expect(screen.getByText(/cut short/)).toBeTruthy();
  });
});
