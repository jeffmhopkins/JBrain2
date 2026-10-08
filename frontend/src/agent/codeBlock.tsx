// A fenced code block in a chat answer, coloured (docs/reference/DESIGN.md "Code blocks").
//
// The grammars are code-split (`highlighter.ts`): a block renders plain at once, fetches the
// highlighter the first time any block needs it, and re-renders coloured when it lands. A
// block still streaming (its closing fence not yet arrived) stays plain, so an answer being
// typed out is not re-tokenized on every token — it colours once, when it is whole.
//
// SAFETY: the highlighter returns a hast TREE and this file walks it into React elements —
// text nodes and <span>s with a class from a closed set. There is no innerHTML anywhere on
// this path, so text in a block (the model's, or a repository's) reaches the DOM as text.

import { type ReactNode, useEffect, useMemo, useState } from "react";
import {
  type CuratedLang,
  type FenceLang,
  type Tok,
  splitGutter,
  tokenFor,
  withinHighlightCaps,
} from "./codeLang";
import type { HNode } from "./highlighter";

type Highlighter = typeof import("./highlighter");

let loaded: Highlighter | null = null;
let loading: Promise<Highlighter> | null = null;

function loadHighlighter(): Promise<Highlighter> {
  loading ??= import("./highlighter").then((m) => {
    loaded = m;
    return m;
  });
  return loading;
}

/** Test seam: forget the loaded module so a test can watch the plain-first render again. */
export function resetHighlighterForTest(): void {
  loaded = null;
  loading = null;
}

/** The class each token renders with. Prefixed so a page's own `.k`/`.s` can't restyle it. */
const TOKEN_CLASS: Record<Tok, string> = {
  k: "hl-k",
  s: "hl-s",
  n: "hl-n",
  f: "hl-f",
  c: "hl-c",
  t: "hl-t",
  m: "hl-m",
};

function classList(node: Extract<HNode, { type: "element" }>): string[] {
  const cls = node.properties?.className;
  return Array.isArray(cls) ? cls.filter((c): c is string => typeof c === "string") : [];
}

/** The hast tree as React nodes: text stays text, every element becomes a <span> whose
 * class is mapped from the closed token set (or none, inheriting its parent's colour). */
export function renderTree(nodes: readonly HNode[], key = "h"): ReactNode[] {
  return nodes.map((node, i) => {
    if (node.type === "text") return node.value;
    const tok = tokenFor(classList(node));
    const k = `${key}.${i}`;
    return (
      <span key={k} className={tok ? TOKEN_CLASS[tok] : undefined}>
        {renderTree(node.children, k)}
      </span>
    );
  });
}

/** The coloured nodes for `code`, or the plain string until the highlighter is loaded,
 * while `ready` is false (still streaming), over the size caps, or when there is nothing
 * confident to colour it with. `lang` "text" is an explicit plain request. */
export function useHighlighted(code: string, lang: FenceLang, ready: boolean): ReactNode {
  const wanted = ready && lang !== "text" && code.trim() !== "" && withinHighlightCaps(code);
  const [mod, setMod] = useState<Highlighter | null>(loaded);
  useEffect(() => {
    if (!wanted || mod) return;
    let alive = true;
    loadHighlighter().then(
      (m) => {
        if (alive) setMod(m);
      },
      // A failed chunk fetch (offline, a stale deploy) leaves the block plain — the code is
      // still all there, just uncoloured — and lets a later block try again.
      () => {
        loading = null;
      },
    );
    return () => {
      alive = false;
    };
  }, [wanted, mod]);
  return useMemo(() => {
    if (!wanted || !mod) return code;
    const tree = mod.highlightTree(code, lang as CuratedLang | null);
    return tree ? renderTree(tree) : code;
  }, [wanted, mod, code, lang]);
}

/** A fenced block. `ready` is false while its closing fence has not streamed in yet. */
export function CodeBlock({
  code,
  lang,
  ready,
}: {
  code: string;
  lang: FenceLang;
  ready: boolean;
}): ReactNode {
  const gutter = useMemo(() => splitGutter(code), [code]);
  const body = useHighlighted(gutter ? gutter.code : code, lang, ready);
  if (!gutter) {
    return (
      <pre className="md-pre">
        <code>{body}</code>
      </pre>
    );
  }
  // Two columns: the numbers (dim, unselectable, so a copy takes only the code) and the
  // code. One line per row in each, so they align without wrapping either.
  return (
    <pre className="md-pre md-pre-numbered">
      <span className="md-gutter" aria-hidden="true">
        {gutter.numbers.join("\n")}
      </span>
      <code>{body}</code>
    </pre>
  );
}
