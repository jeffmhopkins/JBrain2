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

  it("strips a browsing model's 【N†…】 citations, keeping the prose clean", () => {
    // gpt-oss emits fullwidth-bracket citations with a † dagger; they point at its
    // own browse state and can't map to our chips, so they're removed (with the one
    // leading space) rather than leaked as raw text.
    const out = html("about 18 hours 27 minutes 【13†L9-L13】 under typical traffic.");
    expect(out).toContain("18 hours 27 minutes under typical traffic.");
    expect(out).not.toContain("†");
    expect(out).not.toContain("13");
  });

  it("strips the ASCII [N†…] citation form and multiple in a row", () => {
    const out = html("The drive is long [13†L9-L13][14†L1-L5] today.");
    expect(out).toContain("The drive is long today.");
    expect(out).not.toContain("†");
  });

  it("does not touch a real [^n] source citation (no dagger)", () => {
    const onCite = vi.fn();
    render(<Markdown text="You were born then.[^1]" onCite={onCite} />);
    fireEvent.click(screen.getByRole("button", { name: "1" }));
    expect(onCite).toHaveBeenCalledWith(1);
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

  it("highlights the flagged claim text with .md-claim alongside the ⚠", () => {
    render(
      <Markdown
        text="You were born in 1986. The roof needs replacing."
        flags={[{ id: "ug-0", claim: "The roof needs replacing.", reason: "Not in your notes." }]}
      />,
    );
    const claim = document.querySelector(".md-claim");
    expect(claim).not.toBeNull();
    // The highlight wraps the flagged TEXT verbatim (subtle amber treatment), and
    // the ⚠ flag still sits alongside it.
    expect(claim?.textContent).toBe("The roof needs replacing.");
    expect(screen.getByRole("button", { name: "unverified claim" })).toBeInTheDocument();
    // The grounded prefix sentence is not highlighted.
    expect(document.body.textContent).toContain("You were born in 1986.");
  });

  it("does not highlight any text on a grounded / no-verdict turn", () => {
    render(<Markdown text="You were born in 1986. All grounded here." />);
    expect(document.querySelector(".md-claim")).toBeNull();
  });

  it("gives the end-of-bubble fallback flag no .md-claim highlight", () => {
    render(
      <Markdown
        text="An entirely different answer than the verdict expected."
        flags={[
          { id: "ug-0", claim: "A sentence that is nowhere in the prose.", reason: "no source" },
        ]}
      />,
    );
    // The fallback drops only the ⚠ — no claim text exists to highlight.
    expect(document.querySelector(".md-flag-fallback")).not.toBeNull();
    expect(document.querySelector(".md-claim")).toBeNull();
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

  it("does not flag a grounded sentence that merely repeats an ungrounded claim as a prefix", () => {
    // The same phrasing recurs: once as a standalone ungrounded sentence, then as
    // the PREFIX of a longer grounded sentence. Only the standalone one is flagged —
    // the boundary guard must not inject a false warning into the grounded prose.
    render(
      <Markdown
        text="The roof needs replacing. The roof needs replacing soon and was paid for."
        flags={[{ id: "ug-0", claim: "The roof needs replacing", reason: "no source" }]}
      />,
    );
    expect(screen.getAllByRole("button", { name: "unverified claim" })).toHaveLength(1);
    expect(document.querySelector(".md-flag-fallback")).toBeNull();
    // The grounded second sentence is still present as unflagged prose.
    expect(document.body.textContent).toContain("soon and was paid for");
  });

  it("degrades to the end-of-bubble fallback instead of anchoring a claim mid-sentence", () => {
    // The claim occurs only mid-sentence (not on a sentence boundary), so no inline
    // anchor is valid — it must fall back rather than mis-anchor inside the sentence.
    render(
      <Markdown
        text="We discussed that the roof needs replacing soon, per the contractor."
        flags={[{ id: "ug-0", claim: "the roof needs replacing soon", reason: "no source" }]}
      />,
    );
    expect(document.querySelector(".md-flag-fallback")).not.toBeNull();
    // The only flag is the fallback's — no inline mid-sentence anchor was placed.
    expect(document.querySelectorAll(".md-flag-wrap")).toHaveLength(1);
    expect(document.querySelector(".md-flag-fallback .md-flag-wrap")).not.toBeNull();
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
