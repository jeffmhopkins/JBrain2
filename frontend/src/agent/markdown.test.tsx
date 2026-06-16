import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Markdown } from "./markdown";

function html(text: string): string {
  return render(<Markdown text={text} />).container.innerHTML;
}

describe("Markdown", () => {
  it("renders inline bold, italic, and code", () => {
    const out = html("a **bold** and *soft* and `x = 1`");
    expect(out).toContain("<strong>bold</strong>");
    expect(out).toContain("<em>soft</em>");
    expect(out).toContain('<code class="md-code">x = 1</code>');
  });

  it("renders a safe link and drops an unsafe scheme to text", () => {
    expect(html("see [docs](https://example.com)")).toContain(
      '<a class="md-link" href="https://example.com" target="_blank" rel="noreferrer noopener">docs</a>',
    );
    const unsafe = html("[click](javascript:alert(1))");
    expect(unsafe).not.toContain("<a");
    expect(unsafe).toContain("click");
  });

  it("renders headings, lists, blockquotes, and code blocks", () => {
    const out = html(
      "# Title\n\n- one\n- two\n\n1. first\n2. second\n\n> a quote\n\n```\ncode line\n```",
    );
    expect(out).toMatch(/<h3 class="md-h">Title<\/h3>/);
    expect(out).toContain('<ul class="md-ul">');
    expect((out.match(/<li>/g) ?? []).length).toBe(4);
    expect(out).toContain('<ol class="md-ol">');
    expect(out).toContain('<blockquote class="md-quote">a quote</blockquote>');
    expect(out).toContain('<pre class="md-pre"><code>code line</code></pre>');
  });

  it("splits paragraphs and keeps soft breaks within one", () => {
    const out = html("line a\nline b\n\nsecond para");
    expect((out.match(/<p class="md-p">/g) ?? []).length).toBe(2);
    expect(out).toContain("<br>");
  });

  it("marks explicit dates as temporal chips with a normalized title", () => {
    const iso = html("born on 2026-03-19 sharp");
    expect(iso).toMatch(/<time class="md-temporal"[^>]*>2026-03-19<\/time>/);
    // ISO dates are read as a calendar day (UTC) so the weekday is stable.
    expect(iso).toContain('title="Thursday, March 19, 2026"');

    expect(html("met her March 19, 1986 at noon")).toMatch(
      /<time class="md-temporal"[^>]*>March 19, 1986<\/time>/,
    );
  });

  it("does not mangle unclosed emphasis or stray asterisks", () => {
    expect(html("a **bold start with no end")).not.toContain("<strong>");
    expect(html("3 * 4 * 5 = 60")).not.toContain("<em>");
  });

  it("renders an unclosed code fence as a code block to the end", () => {
    expect(html("```\nstuck open")).toContain('<pre class="md-pre"><code>stuck open</code></pre>');
  });

  it("renders [^n] as a tappable citation that calls onCite with the number", () => {
    const onCite = vi.fn();
    render(<Markdown text="You were born then.[^1] More.[^2]" onCite={onCite} />);
    fireEvent.click(screen.getByRole("button", { name: "2" }));
    expect(onCite).toHaveBeenCalledWith(2);
  });

  it("linkifies an entity label in the prose and opens it on tap", () => {
    const onEntity = vi.fn();
    render(
      <Markdown
        text="You are **Jeff Hopkins**, married to Celine."
        onEntity={onEntity}
        entities={[
          { entity_id: "e1", label: "Jeff Hopkins", domain: "general" },
          { entity_id: "e2", label: "Celine", domain: "health" },
          // surfaced but never named in the text — no inline link to make
          { entity_id: "e3", label: "Acme Corp", domain: "general" },
        ]}
      />,
    );
    // The name inside bold is still linked (matching recurses through emphasis).
    fireEvent.click(screen.getByRole("button", { name: "Jeff Hopkins" }));
    expect(onEntity).toHaveBeenCalledWith("e1");
    fireEvent.click(screen.getByRole("button", { name: "Celine" }));
    expect(onEntity).toHaveBeenCalledWith("e2");
    // An entity that isn't mentioned makes no link.
    expect(screen.queryByRole("button", { name: "Acme Corp" })).toBeNull();
  });

  it("links a prose name that is an alias, not the canonical label", () => {
    const onEntity = vi.fn();
    render(
      <Markdown
        text="You are Jeff Hopkins."
        onEntity={onEntity}
        // canonical label "Me" never appears; the alias does and must link.
        entities={[{ entity_id: "me", label: "Me", domain: "general", aliases: ["Jeff Hopkins"] }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Jeff Hopkins" }));
    expect(onEntity).toHaveBeenCalledWith("me");
  });

  it("anchors an amber ⚠ flag after an ungrounded claim and reveals its reason on tap", () => {
    const onFlag = vi.fn();
    const { rerender } = render(
      <Markdown
        text="You were born in 1986. The roof needs replacing."
        flags={[{ id: "ug-0", claim: "The roof needs replacing.", reason: "Not in your notes." }]}
        onFlag={onFlag}
      />,
    );
    const flag = screen.getByRole("button", { name: "unverified claim" });
    expect(flag).toHaveClass("md-flag");
    // The note isn't shown until tapped.
    expect(screen.queryByText("Not in your notes.")).toBeNull();
    fireEvent.click(flag);
    expect(onFlag).toHaveBeenCalledWith("ug-0");
    // Drive the open state back in (the surface owns it) — the reason appears.
    rerender(
      <Markdown
        text="You were born in 1986. The roof needs replacing."
        flags={[{ id: "ug-0", claim: "The roof needs replacing.", reason: "Not in your notes." }]}
        onFlag={onFlag}
        openFlag="ug-0"
      />,
    );
    expect(screen.getByText("Not in your notes.")).toBeInTheDocument();
  });

  it("anchors a flag through inline markdown (a bolded claim still matches)", () => {
    render(
      <Markdown
        text="**The roof needs replacing.**"
        flags={[{ id: "ug-0", claim: "**The roof needs replacing.**", reason: "no source" }]}
      />,
    );
    // The claim's markdown source matches the rendered (marker-stripped) prose.
    expect(screen.getByRole("button", { name: "unverified claim" })).toBeInTheDocument();
    expect(document.querySelector(".md-flag-fallback")).toBeNull();
  });

  it("falls back to an end-of-bubble flag when a claim can't be located in the prose", () => {
    render(
      <Markdown
        text="An entirely different answer than the verdict expected."
        flags={[
          { id: "ug-0", claim: "A sentence that is nowhere in the prose.", reason: "no source" },
        ]}
      />,
    );
    // No inline anchor was possible, so a single end-of-bubble flag stands in.
    const fallback = document.querySelector(".md-flag-fallback");
    expect(fallback).not.toBeNull();
    expect(fallback?.querySelector(".md-flag")).not.toBeNull();
  });

  it("renders no flag when there are none (unchanged prose)", () => {
    render(<Markdown text="A perfectly grounded answer." />);
    expect(screen.queryByRole("button", { name: "unverified claim" })).toBeNull();
    expect(document.querySelector(".md-flag-fallback")).toBeNull();
  });

  it("prefers the longest entity label and respects word boundaries", () => {
    const onEntity = vi.fn();
    render(
      <Markdown
        text="Celine Hopkins waved. Unceline is not a match."
        onEntity={onEntity}
        entities={[
          { entity_id: "short", label: "Celine", domain: "general" },
          { entity_id: "long", label: "Celine Hopkins", domain: "general" },
        ]}
      />,
    );
    // "Celine Hopkins" wins over the bare "Celine"; "Unceline" is not matched.
    fireEvent.click(screen.getByRole("button", { name: "Celine Hopkins" }));
    expect(onEntity).toHaveBeenCalledWith("long");
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });
});
