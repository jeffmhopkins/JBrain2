// Fenced code in a chat answer, end to end through the markdown renderer. The highlighter is
// lazy, so the shape of every test is: render, then wait for colour (or wait for a block that
// IS coloured and check that this one is not).

import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { resetHighlighterForTest } from "./codeBlock";
import { Markdown } from "./markdown";

const PY = "```python\ndef advance_floor(x):\n    return x + 1  # one up\n```";

function pres(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>("pre.md-pre")];
}

/** Wait until the FIRST block is coloured — proof the highlighter has loaded and run — then
 * hand back every block, so a test can assert that another one stayed plain. */
async function settled(container: HTMLElement): Promise<HTMLElement[]> {
  await waitFor(() => expect(pres(container)[0]?.querySelector("span.hl-k")).toBeTruthy());
  return pres(container);
}

describe("code blocks", () => {
  beforeEach(() => resetHighlighterForTest());

  it("renders plain first, then coloured once the highlighter loads", async () => {
    const { container } = render(<Markdown text={PY} />);
    const pre = pres(container)[0];
    expect(pre?.querySelector("span")).toBeNull();
    expect(pre?.textContent).toContain("def advance_floor(x):");
    await settled(container);
    const classes = [...(pre?.querySelectorAll("span") ?? [])].map((s) => s.className);
    expect(classes).toEqual(expect.arrayContaining(["hl-k", "hl-f", "hl-n", "hl-c"]));
    // Colour adds spans, never text: the code reads exactly as written.
    expect(pre?.textContent).toBe("def advance_floor(x):\n    return x + 1  # one up");
  });

  it("emits text and spans only — markup in a block is never markup", async () => {
    const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    const { container } = render(
      <Markdown text={`${PY}\n\n\`\`\`html\n${hostile}\n\`\`\`\n\n\`\`\`\n${hostile}\n\`\`\``} />,
    );
    const [, html, untagged] = await settled(container);
    await waitFor(() => expect(html?.querySelector("span")).toBeTruthy());
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(html?.textContent).toBe(hostile);
    expect(untagged?.textContent).toBe(hostile);
    for (const el of container.querySelectorAll("pre.md-pre *")) {
      expect(["CODE", "SPAN"]).toContain(el.tagName);
      expect([...el.attributes].map((a) => a.name).filter((n) => n !== "class")).toEqual([]);
    }
  });

  it("auto-detects an untagged block it is confident about", async () => {
    const { container } = render(
      <Markdown text={"```\nimport os\n\ndef main():\n    print(os.getcwd())\n```"} />,
    );
    await settled(container);
  });

  it("renders plain an untagged block it cannot place, and an explicit plain tag", async () => {
    const { container } = render(
      <Markdown
        text={`${PY}\n\n\`\`\`\nhello there\n\`\`\`\n\n\`\`\`text\nfor x in y: pass\n\`\`\``}
      />,
    );
    const [, prose, text] = await settled(container);
    expect(prose?.querySelector("span")).toBeNull();
    expect(text?.querySelector("span")).toBeNull();
  });

  it("falls back to plain past the size caps", async () => {
    const huge = Array(2_001).fill("x = 1").join("\n");
    const { container } = render(<Markdown text={`${PY}\n\n\`\`\`python\n${huge}\n\`\`\``} />);
    const [, big] = await settled(container);
    expect(big?.querySelector("span")).toBeNull();
    expect(big?.textContent).toBe(huge);
  });

  it("keeps a streaming block plain until its closing fence arrives", async () => {
    const partial = `${PY}\n\n\`\`\`python\ndef still_typing(`;
    const { container, rerender } = render(<Markdown text={partial} streaming />);
    const [, open] = await settled(container);
    expect(open?.querySelector("span")).toBeNull();
    rerender(<Markdown text={`${partial})\n\`\`\``} streaming />);
    await waitFor(() => expect(pres(container)[1]?.querySelector("span.hl-k")).toBeTruthy());
  });

  it("colours an unclosed fence once the answer has finished", async () => {
    const { container } = render(<Markdown text={"```python\ndef f():\n    return 1"} />);
    await settled(container);
  });

  it("renders a numbered listing's numbers as a separate gutter", async () => {
    const { container } = render(
      <Markdown text={"```python\n 9  def f():\n10      return 1\n11  \n12  x = f()\n```"} />,
    );
    const [pre] = await settled(container);
    const gutter = pre?.querySelector(".md-gutter");
    expect(pre?.className).toContain("md-pre-numbered");
    expect(gutter?.getAttribute("aria-hidden")).toBe("true");
    expect(gutter?.textContent).toBe("9\n10\n11\n12");
    expect(pre?.querySelector("code")?.textContent).toBe("def f():\n    return 1\n\nx = f()");
  });

  it("leaves numbered data alone", () => {
    const { container } = render(<Markdown text={"```\n1990  12.5\n2000  15.1\n```"} />);
    expect(container.querySelector(".md-gutter")).toBeNull();
    expect(pres(container)[0]?.textContent).toBe("1990  12.5\n2000  15.1");
  });
});
