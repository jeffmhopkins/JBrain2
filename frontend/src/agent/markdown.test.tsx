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

  it("renders a GFM pipe table as a real table grid", () => {
    const out = html(
      "| Time | Temp | Notes |\n|------|------|-------|\n| 6 PM | 93 | Strongest rain |\n| 8 PM | 87 | Easing |",
    );
    expect(out).toContain('<table class="md-table">');
    // Header cells live in a thead, body rows in a tbody.
    expect(out).toContain("<thead>");
    expect((out.match(/<th[ >]/g) ?? []).length).toBe(3);
    expect((out.match(/<tbody>/g) ?? []).length).toBe(1);
    expect((out.match(/<tr>/g) ?? []).length).toBe(3); // 1 header + 2 body
    expect((out.match(/<td[ >]/g) ?? []).length).toBe(6); // 2 rows × 3 cols
    expect(out).toContain("Strongest rain");
    // The pipes are gone — no raw "| 6 PM |" leaking into prose.
    expect(out).not.toContain("| 6 PM |");
  });

  it("parses tables without outer border pipes and applies column alignment", () => {
    const out = html("a | b | c\n:--- | :--: | ---:\n1 | 2 | 3");
    expect(out).toContain('<table class="md-table">');
    expect(out).toContain("text-align: center");
    expect(out).toContain("text-align: right");
  });

  it("pads short rows and drops overflow to the header's column count", () => {
    const out = html("| A | B | C |\n|---|---|---|\n| only-one |\n| w | x | y | z |");
    // Every body row renders exactly three cells regardless of how many it supplied.
    expect((out.match(/<td[ >]/g) ?? []).length).toBe(6);
    expect(out).toContain("only-one");
    expect(out).not.toContain("z"); // the 4th cell of the over-long row is dropped
  });

  it("scans inline markup and entities inside table cells", () => {
    const onEntity = vi.fn();
    render(
      <Markdown
        text={"| Who | Note |\n|-----|------|\n| Celine | **busy** |"}
        onEntity={onEntity}
        entities={[{ entity_id: "e2", label: "Celine", domain: "health" }]}
      />,
    );
    expect(document.querySelector(".md-table")).not.toBeNull();
    expect(document.querySelector("td strong")?.textContent).toBe("busy");
    fireEvent.click(screen.getByRole("button", { name: "Celine" }));
    expect(onEntity).toHaveBeenCalledWith("e2");
  });

  it("starts a table even when prose precedes it with no blank line", () => {
    const out = html("Here is the forecast:\n| Time | Temp |\n|------|------|\n| 6 PM | 93 |");
    expect(out).toContain('<table class="md-table">');
    expect(out).toContain("Here is the forecast:");
  });

  it("does not treat a horizontal-rule-like line as a table", () => {
    // A pipe-free line followed by dashes is not a table (no header pipe).
    const out = html("just prose\n---\nmore prose");
    expect(out).not.toContain("<table");
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

  it("linkifies a browsing model's bare-URL citation 【https://…】 (no dagger)", () => {
    // gpt-oss sometimes wraps a real source URL in its fullwidth citation brackets
    // with no dagger — unlike the 【N†…】 cursor form, this points at a followable
    // page, so it becomes a tappable link (brackets dropped) rather than leaking.
    const out = html("contact page: 【https://www.coghillfarm.com/contact】 for details.");
    expect(out).toContain(
      '<a class="md-link" href="https://www.coghillfarm.com/contact" target="_blank" rel="noreferrer noopener">https://www.coghillfarm.com/contact</a>',
    );
    // The fullwidth brackets are consumed — not leaked into the prose.
    expect(document.body.textContent).not.toContain("【");
    expect(document.body.textContent).not.toContain("】");
    // The surrounding prose still flows around the link.
    expect(document.body.textContent).toContain("contact page:");
    expect(document.body.textContent).toContain("for details.");
  });

  it("still strips the 【N†…】 cursor citation and never linkifies it", () => {
    // The dagger form must keep its existing strip behavior even now that a sibling
    // bare-URL form linkifies — a cursor citation is not a followable link.
    const out = html("about 18 hours 【13†L9-L13】 away.");
    expect(out).not.toContain("<a");
    expect(out).not.toContain("†");
    expect(out).not.toContain("【");
  });

  it("renders [^n] as a tappable favicon link when the cite target is a web source", () => {
    // jerv cites a web claim with [^n]; the target resolves to the source URL, so
    // the marker becomes a favicon that opens the page (served on-box, not the host).
    const { container } = render(
      <Markdown
        text="Bluey: The Videogame released in 2023.[^1]"
        cites={[{ kind: "web", url: "https://www.xbox.com/games/store/bluey/9N2", title: "Xbox" }]}
      />,
    );
    const link = container.querySelector("a.md-link, .md-webcite a") as HTMLAnchorElement;
    expect(link?.getAttribute("href")).toBe("https://www.xbox.com/games/store/bluey/9N2");
    expect(link?.getAttribute("target")).toBe("_blank");
    const img = link?.querySelector("img.md-favicon") as HTMLImageElement;
    // The favicon is fetched from our own API by the source host — never the host itself.
    expect(img?.getAttribute("src")).toBe("/api/agent/favicon?host=www.xbox.com");
  });

  it("keeps the numbered chip (onCite) when the cite target is a note", () => {
    const onCite = vi.fn();
    render(
      <Markdown
        text="From your note.[^1]"
        onCite={onCite}
        cites={[{ kind: "note", noteId: "note-7" }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "1" }));
    expect(onCite).toHaveBeenCalledWith(1);
  });

  it("strips a browsing model's 【source: …】 prose citation (no URL, not tappable)", () => {
    // The old leak: a narrated source with no followable link. We now ask jerv for
    // [^n] markers, but strip this stray form so it never shows as raw brackets.
    const out = html("Bluey: The Videogame released in 2023 【source: Wikipedia page for Bluey】.");
    expect(out).toContain("Bluey: The Videogame released in 2023.");
    expect(out).not.toContain("【");
    expect(out).not.toContain("source:");
  });

  it("leaves legitimate 【…】 brackets that aren't source citations untouched", () => {
    // The strip is keyed to the "source" keyword, so it never eats fullwidth brackets
    // used for ordinary content (e.g. a CJK heading).
    const out = html("見出し: 【重要】 のお知らせ。");
    expect(out).toContain("【重要】");
  });

  it("does not touch a real [^n] source citation (no dagger)", () => {
    const onCite = vi.fn();
    render(<Markdown text="You were born then.[^1]" onCite={onCite} />);
    fireEvent.click(screen.getByRole("button", { name: "1" }));
    expect(onCite).toHaveBeenCalledWith(1);
  });

  it("places a Google Maps pin after a US postal address", () => {
    const out = html("The farm's address is P.O. Box 2782, Clanton, Alabama 35046, USA today.");
    const pin = document.querySelector("a.md-place");
    expect(pin).not.toBeNull();
    expect(pin?.getAttribute("aria-label")).toBe("Open in Google Maps");
    const href = pin?.getAttribute("href") ?? "";
    expect(href.startsWith("https://www.google.com/maps/search/?api=1&query=")).toBe(true);
    // The whole address (whitespace-collapsed) is the map query.
    expect(decodeURIComponent(href)).toContain("P.O. Box 2782, Clanton, Alabama 35046, USA");
    // The address text still reads as prose around the pin, and the pin opens
    // externally.
    expect(document.body.textContent).toContain("Clanton, Alabama 35046");
    expect(pin?.getAttribute("target")).toBe("_blank");
    expect(pin?.getAttribute("rel")).toBe("noreferrer noopener");
    expect(out).toContain('class="md-place"');
  });

  it("places a pin from a bare 'City, ST 12345' code form", () => {
    const out = html("Mailing: Clanton, AL 35046.");
    expect(out).toContain('class="md-place"');
    expect(
      decodeURIComponent(document.querySelector("a.md-place")?.getAttribute("href") ?? ""),
    ).toContain("Clanton, AL 35046");
  });

  it("pins a ZIP-less street address anchored by its street suffix", () => {
    // The shape the showtimes answer emitted — a street number + suffix, city, and
    // state code, but no ZIP. The number+suffix pair is enough to pin it.
    const out = html(
      "Now playing at Epic Theatres – Titusville (2505 South Hopkins Ave, Titusville, FL) today.",
    );
    expect(out).toContain('class="md-place"');
    expect(
      decodeURIComponent(document.querySelector("a.md-place")?.getAttribute("href") ?? ""),
    ).toContain("2505 South Hopkins Ave, Titusville, FL");
  });

  it("does not pin a number+comma prose run lacking a street suffix", () => {
    // A leading number and a stray uppercase code, but no street suffix — must not
    // masquerade as an address.
    expect(html("I waited 20 minutes, Tom, IN a hurry to leave.")).not.toContain("md-place");
  });

  it("links a signed decimal GPS pair to its lat,lng query", () => {
    const out = html("HQ is at 40.7128, -74.0060 near the river.");
    const pin = document.querySelector("a.md-place");
    expect(pin).not.toBeNull();
    const href = decodeURIComponent(pin?.getAttribute("href") ?? "");
    expect(href).toContain("query=40.7128,-74.006");
    // The surrounding prose still flows.
    expect(document.body.textContent).toContain("near the river.");
    expect(out).toContain("40.7128, -74.0060");
  });

  it("links a hemisphere GPS pair (N/S, E/W) with correct signs", () => {
    const href = decodeURIComponent(
      render(<Markdown text="Site: 34.0522° N, 118.2437° W." />)
        .container.querySelector("a.md-place")
        ?.getAttribute("href") ?? "",
    );
    // S/W hemispheres become negative; N stays positive.
    expect(href).toContain("query=34.0522,-118.2437");
  });

  it("does not pin a plain decimal list, a price pair, or an out-of-range pair", () => {
    // No minus and no hemisphere — a number list, not a coordinate.
    expect(html("scores were 5.50, 10.25 overall")).not.toContain("md-place");
    // Currency stays untouched.
    expect(html("It costs between $5 and $10 today.")).not.toContain("md-place");
    // A signed pair whose latitude exceeds 90° is rejected.
    expect(html("readings of -95.5, 200.4 logged")).not.toContain("md-place");
  });

  it("does not pin a state name without a ZIP", () => {
    expect(html("She moved to Alabama last spring.")).not.toContain("md-place");
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

  it("typesets inline $…$ math with KaTeX", () => {
    const out = html("The mass-energy law is $E = mc^2$ in physics.");
    // KaTeX emits its own .katex markup inside our .md-math span.
    expect(out).toContain('class="md-math"');
    expect(out).toContain("katex");
    // The dollar delimiters are consumed (not leaked as literal text).
    expect(document.body.textContent).not.toContain("$E = mc^2$");
    // The surrounding prose still flows around the equation.
    expect(document.body.textContent).toContain("The mass-energy law is");
    expect(document.body.textContent).toContain("in physics.");
  });

  it("typesets inline \\(…\\) math with KaTeX", () => {
    const out = html("Pythagoras: \\(a^2 + b^2 = c^2\\) holds.");
    expect(out).toContain('class="md-math"');
    expect(out).toContain("katex");
    // The \(…\) delimiters are consumed, not shown verbatim.
    expect(document.body.textContent).not.toContain("\\(");
    expect(document.body.textContent).not.toContain("\\)");
  });

  it("renders $$…$$ as a centered display-math block", () => {
    const out = html("The integral:\n\n$$\\int_0^1 x^2 \\, dx = \\tfrac{1}{3}$$\n\ndone.");
    expect(out).toContain('class="md-math-wrap"');
    expect(out).toContain("katex-display");
    expect(document.body.textContent).toContain("done.");
  });

  it("renders \\[…\\] as a display-math block, including multi-line", () => {
    const out = html("\\[\nx = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}\n\\]");
    expect(out).toContain('class="md-math-wrap"');
    expect(out).toContain("katex-display");
  });

  it("does not treat currency like $5 and $10 as math", () => {
    const out = html("It costs between $5 and $10 today.");
    expect(out).not.toContain('class="md-math"');
    expect(document.body.textContent).toContain("It costs between $5 and $10 today.");
  });

  it("leaves an unclosed $ as plain text (streamed partial answer)", () => {
    const out = html("The cost is $5 and the formula $x = ");
    expect(out).not.toContain('class="md-math"');
    expect(document.body.textContent).toContain("$x =");
  });

  it("shows a subdued placeholder for not-yet-complete display math while streaming", () => {
    // The closing $$ has arrived but the body is mid-token (\frac wants two args) —
    // while streaming this reads as "building", never KaTeX's red parse error.
    const { container } = render(<Markdown text={"$$\\frac{a}{"} streaming />);
    const pending = container.querySelector(".md-math-pending");
    expect(pending).not.toBeNull();
    expect(pending?.textContent).toBe("building math render…");
    expect(container.querySelector(".katex-error")).toBeNull();
  });

  it("settles a still-malformed formula to its raw source (not a placeholder)", () => {
    // Once the turn is done (not streaming), an unrenderable formula degrades to its
    // raw source rather than holding the placeholder forever.
    const { container } = render(<Markdown text={"$$\\frac{a}{"} />);
    expect(container.querySelector(".md-math-pending")).toBeNull();
    expect(container.querySelector(".katex-error")).toBeNull();
    expect(document.body.textContent).toContain("\\frac{a}{");
  });

  it("typesets complete math normally even while streaming", () => {
    const { container } = render(<Markdown text={"$E = mc^2$"} streaming />);
    expect(container.querySelector(".md-math-pending")).toBeNull();
    expect(container.querySelector(".katex")).not.toBeNull();
  });

  it("keeps a $ inside inline code literal (no math typesetting)", () => {
    const out = html("Run `echo $PATH` to print it.");
    expect(out).toContain('<code class="md-code">echo $PATH</code>');
    expect(out).not.toContain('class="md-math"');
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
