// The grammar half of code-block highlighting, code-split: `codeBlock.tsx` imports this
// dynamically the first time a block needs colour, so a chat with no code never fetches it.
//
// Only the curated grammars are registered — highlight.js's full set is ~190 languages and a
// near-miss grammar (auto-detect choosing Lisp for a log file) colours worse than plain text.
// The output is a hast tree, NOT HTML: `codeBlock.tsx` turns it into React elements, so the
// text inside a block reaches the DOM only ever as text.

import type { LanguageFn } from "highlight.js";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { createLowlight } from "lowlight";
import type { CuratedLang } from "./codeLang";

const GRAMMARS: Record<CuratedLang, LanguageFn> = {
  python,
  typescript,
  javascript,
  bash,
  shell,
  json,
  yaml,
  sql,
  rust,
  c,
  cpp,
  go,
  xml,
  css,
  diff,
  markdown,
  ini,
  dockerfile,
};

const lowlight = createLowlight();
lowlight.register(GRAMMARS);

// Auto-detect only among languages a reader would plausibly paste untagged. `shell` (a
// prompt transcript) and `markdown` match prose too readily; `ini` matches any `a = b`.
const AUTO_SUBSET: CuratedLang[] = [
  "python",
  "typescript",
  "javascript",
  "bash",
  "json",
  "yaml",
  "sql",
  "rust",
  "c",
  "cpp",
  "go",
  "xml",
  "css",
  "diff",
  "dockerfile",
];

/** hljs relevance an untagged block must reach before its guess is trusted. Relevance
 * counts distinctive matches (keywords, shapes). Measured on this subset: a short prose
 * line or a file-tree listing scores 1–2 (as SQL or CSS), a two-line Python or TypeScript
 * snippet 3+, so the floor sits between them. */
export const AUTO_RELEVANCE = 3;

/** The hast nodes lowlight emits — only text and spans, which is all this module's
 * consumer will render. */
export type HNode =
  | { type: "text"; value: string }
  | {
      type: "element";
      tagName: string;
      properties?: { className?: unknown } | undefined;
      children: HNode[];
    };

/** The highlighted tree for `code`, or null when it should render plain: a language we do
 * not register, or an untagged block whose best guess is not confident. */
export function highlightTree(code: string, lang: CuratedLang | null): HNode[] | null {
  if (lang !== null) {
    if (!lowlight.registered(lang)) return null;
    return lowlight.highlight(lang, code).children as HNode[];
  }
  const root = lowlight.highlightAuto(code, { subset: AUTO_SUBSET });
  const relevance = root.data?.relevance ?? 0;
  if (!root.data?.language || relevance < AUTO_RELEVANCE) return null;
  return root.children as HNode[];
}
