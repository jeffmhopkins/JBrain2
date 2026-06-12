import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
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
});
