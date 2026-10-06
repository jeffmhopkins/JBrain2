// The `browse_trace` step view (binding mock docs/mocks/browse-trace/a-timeline.html). What is
// worth pinning: the rail and its panels render from the stored payload, and EVERYTHING a page
// shaped reaches the DOM as text — no markup, no links, no images.

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { highlightCall, seconds } from "./browseTrace";
import { ToolView } from "./registry";

const HOSTILE = '<img src=x onerror=alert(1)><a href="https://evil.example">tap</a> **bold**';

function trace(over: Record<string, unknown> = {}) {
  return {
    view: "browse_trace",
    surface: "inline" as const,
    refs: [],
    data: {
      site: "epictheatres.com",
      loop: "fast",
      elapsed_ms: 26_400,
      pages: 3,
      steps: [
        {
          n: 0,
          kind: "host",
          summary: "Opened “epictheatres.com”",
          status: "ok",
          badges: [{ kind: "warn", text: "JS shell" }],
          title: "Epic Theatres | Movie Times",
          url: "https://www.epictheatres.com/",
          ms: 3100,
          commands: [
            { status: "ok", text: "Went to epictheatres.com", element: "", note: "done", ms: 3100 },
          ],
          nums: { browser_ms: 3100, page_tokens: 2140 },
        },
        {
          n: 4,
          kind: "model",
          summary: "Clicked “Tue Oct 6”, clicked “Showtimes” +1 more",
          status: "part",
          badges: [
            { kind: "re", text: "1 re-found" },
            { kind: "ref", text: "1 refused" },
            { kind: "warn", text: "1 not run" },
          ],
          title: HOSTILE,
          url: "https://www.epictheatres.com/theatres/titusville",
          ms: 4800,
          commands: [
            {
              status: "ok",
              text: "Clicked “Tue Oct 6”",
              element: '[41] button "Tue Oct 6"',
              note: "done",
              ms: 400,
              moved_to: "/theatres/titusville",
            },
            {
              status: "ok",
              text: "Clicked “Showtimes”",
              element: '[44] tab "Showtimes"',
              note: "done",
              ms: 500,
              refound: true,
              settle_ms: 300,
            },
            {
              status: "refused",
              text: "Clicked “Epic XL only”",
              element: '[57] button "Epic XL only"',
              note: "[57] is not on the current page",
              ms: 0,
            },
            { status: "not_run", text: 'read "Showtimes"', element: "" },
          ],
          nums: {
            model_ms: 3600,
            browser_ms: 1200,
            settle_ms: 600,
            page_tokens: 1320,
            prompt_tokens: 6840,
            cached_tokens: 5100,
            output_tokens: 62,
          },
          reasoning: "",
          call: 'act({"commands": [{"do": "click", "index": 41}]})',
          page: {
            title: HOSTILE,
            url: "https://www.epictheatres.com/theatres/titusville",
            changes: true,
            tokens: 1320,
            excerpt: `~ [41] button "Tue Oct 6" [pressed]\n- text: ${HOSTILE}`,
            lines_shown: 2,
            lines_total: 61,
          },
        },
      ],
      check: {
        summary: "Checked the answer against the page",
        outcome: "answered",
        answered: true,
        verified: true,
        ms: 40,
        answer: `Harbor Lights: 12:10, 3:20\n${HOSTILE}`,
        rows: [
          { label: "done's answer", text: "verified: 35 of 35 times and prices", ok: true },
          { label: "extraction", text: "not needed — done's answer passed", ok: null },
        ],
        extracted: false,
        error: "",
      },
      ...over,
    },
  };
}

function rung(name: RegExp): HTMLElement {
  return screen.getByRole("button", { name }).closest(".bt-rung") as HTMLElement;
}

describe("browse_trace", () => {
  it("draws the rail: host work hollow, a partly refused turn amber, the check ringed", () => {
    const { container } = render(<ToolView payload={trace()} />);
    expect(container.querySelector(".bt-head")?.textContent).toBe(
      "epictheatres.com2 steps · 3 pages · 26.4 sfast loop · thinking off",
    );
    const dots = [...container.querySelectorAll(".bt-dot")].map((d) => d.className);
    expect(dots).toEqual(["bt-dot host", "bt-dot part", "bt-dot check"]);
    expect(screen.getByText("JS shell")).toHaveClass("bt-badge", "warn");
    expect(screen.getByText("1 re-found")).toHaveClass("re");
    // The host's quoted names are set as <q> — text nodes still.
    expect(container.querySelector(".bt-lab q")?.textContent).toBe("epictheatres.com");
  });

  it("opens a rung onto its commands, timings and tokens", () => {
    render(<ToolView payload={trace()} />);
    const row = screen.getByRole("button", { name: /Tue Oct 6/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(row);
    expect(row).toHaveAttribute("aria-expanded", "true");
    const li = rung(/Tue Oct 6/);
    expect(li).toHaveClass("open");
    const cmds = [...li.querySelectorAll(".bt-cmd")];
    expect(cmds.map((c) => c.className)).toEqual(["bt-cmd", "bt-cmd", "bt-cmd bad", "bt-cmd skip"]);
    expect(cmds[0]?.querySelector(".el i")?.textContent).toBe("[41]");
    expect(cmds[1]?.textContent).toContain("found again by name");
    expect(cmds[2]?.querySelector(".why")?.textContent).toBe("[57] is not on the current page");
    expect(cmds[3]?.querySelector(".tt")?.textContent).toBe("not run");
    expect(li.querySelector(".bt-nums")?.textContent).toContain("prompt 6,840 · 5,100 cached");
  });

  it("swaps one panel between Thinking, Actions and Page it saw; the open chip closes it", () => {
    render(<ToolView payload={trace()} />);
    const li = rung(/Tue Oct 6/);
    const chip = (name: string) => within(li).getByRole("button", { name: new RegExp(name) });
    fireEvent.click(chip("Thinking"));
    expect(within(li).getByText(/No thinking — fast mode/)).toBeInTheDocument();
    expect(chip("Thinking")).toHaveClass("on");

    fireEvent.click(chip("Actions"));
    expect(within(li).queryByText(/No thinking/)).toBeNull();
    const pre = li.querySelector(".bt-pre") as HTMLElement;
    expect(pre.textContent).toBe('act({"commands": [{"do": "click", "index": 41}]})');
    expect([...pre.querySelectorAll("span")].map((s) => s.className)).toContain("k");
    const host = [...li.querySelectorAll(".bt-host li")].map((l) => l.textContent);
    expect(host.some((t) => t?.includes("re-found by role and name"))).toBe(true);
    expect(host.some((t) => t?.includes("refused: [57] is not on the current page"))).toBe(true);
    expect(host.some((t) => t?.includes("not run"))).toBe(true);
    expect(host.some((t) => t?.includes("page moved to /theatres/titusville"))).toBe(true);
    expect(host.some((t) => t?.includes("settle 0.30 s"))).toBe(true);

    fireEvent.click(chip("Page it saw"));
    expect(li.querySelector(".bt-cap")?.textContent).toBe("first 2 of 61 lines of what changed");
    fireEvent.click(chip("Page it saw"));
    expect(li.querySelector(".bt-pan")).toBeNull();
  });

  it("renders every page-shaped string as plain text — no markup, links or images", () => {
    const { container } = render(<ToolView payload={trace()} />);
    const li = rung(/Tue Oct 6/);
    fireEvent.click(within(li).getByRole("button", { name: /Page it saw/ }));
    fireEvent.click(screen.getByRole("button", { name: /Checked the answer/ }));
    expect(container.querySelector("img, a, strong, script")).toBeNull();
    expect(li.querySelector(".bt-pre")?.textContent).toContain(HOSTILE);
    expect(li.querySelector(".bt-sec")?.textContent).toContain(HOSTILE);
    expect(container.querySelector(".bt-ans")?.textContent).toContain(HOSTILE);
    expect(container.textContent).toContain(`→ ${HOSTILE}`);
  });

  it("shows the check rung's answer and the host's own check", () => {
    const { container } = render(<ToolView payload={trace()} />);
    fireEvent.click(screen.getByRole("button", { name: /Checked the answer/ }));
    expect(screen.getByText("verified")).toHaveClass("bt-badge", "ok");
    const rows = [...container.querySelectorAll(".bt-check dd")];
    expect(rows.map((r) => r.className)).toEqual(["ok", ""]);
    expect(rows[1]?.textContent).toBe("not needed — done's answer passed");
  });

  it("reads a B1 run's reasoning, a stopped run's error, and a trimmed payload", () => {
    const data = trace({
      loop: "b1",
      trimmed: true,
      check: {
        summary: "Stopped: time budget spent before an answer",
        answered: false,
        verified: false,
        rows: [],
        error: "the model call failed",
      },
    });
    const steps = data.data.steps as Record<string, unknown>[];
    (steps[1] as Record<string, unknown>).reasoning = "The date strip re-rendered.";
    const { container } = render(<ToolView payload={data} />);
    expect(screen.getByText("B1 loop · thinking on")).toHaveClass("violet");
    expect(screen.getByText("trimmed to fit")).toBeInTheDocument();
    const li = rung(/Tue Oct 6/);
    fireEvent.click(within(li).getByRole("button", { name: /Thinking/ }));
    expect(within(li).getByText("The date strip re-rendered.")).toHaveClass("fb-thinking-trace");
    expect(screen.getByText("no answer")).toHaveClass("ref");
    expect(container.querySelector(".bt-dot.check.bad")).not.toBeNull();
    expect(container.querySelector(".bt-cap.bad")?.textContent).toBe("the model call failed");
  });

  it("says plainly when a host rung had no model call and no page view", () => {
    render(<ToolView payload={trace()} />);
    const li = rung(/Opened/);
    fireEvent.click(within(li).getByRole("button", { name: /Thinking/ }));
    expect(within(li).getByText(/No model call/)).toBeInTheDocument();
    fireEvent.click(within(li).getByRole("button", { name: /Actions/ }));
    expect(within(li).getByText("none — host step")).toBeInTheDocument();
    fireEvent.click(within(li).getByRole("button", { name: /Page it saw/ }));
    expect(within(li).getByText(/The host loaded this page/)).toBeInTheDocument();
  });

  it("survives a payload missing its fields", () => {
    const { container } = render(
      <ToolView
        payload={{ view: "browse_trace", surface: "inline", refs: [], data: { steps: [{}] } }}
      />,
    );
    expect(container.querySelectorAll(".bt-rung")).toHaveLength(2);
    fireEvent.click(container.querySelectorAll(".bt-row")[0] as Element);
    fireEvent.click(screen.getAllByRole("button", { name: /Page it saw/ })[0] as Element);
    expect(screen.getByText("No page view was kept for this turn.")).toBeInTheDocument();
  });
});

describe("browse_trace helpers", () => {
  it("formats time on one unit", () => {
    expect(seconds(40)).toBe("0.04 s");
    expect(seconds(26_400)).toBe("26.4 s");
  });

  it("colours a call from a closed token set and keeps every character", () => {
    const call = 'click({"ref": "e27", "n": -2})';
    const { container } = render(<pre>{highlightCall(call)}</pre>);
    expect(container.textContent).toBe(call);
    expect(
      [...container.querySelectorAll("span")].map((s) => [s.className, s.textContent]),
    ).toEqual([
      ["k", '"ref"'],
      ["s", '"e27"'],
      ["k", '"n"'],
      ["n", "-2"],
    ]);
  });
});
