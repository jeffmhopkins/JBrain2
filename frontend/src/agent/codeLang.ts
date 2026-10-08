// The cheap, always-loaded half of code-block highlighting: which language a fence names,
// whether a block is small enough to colour at all, and whether its lines carry a
// line-number gutter. The grammars themselves live in `highlighter.ts`, which is
// code-split and only fetched the first time a block actually needs colour.

/** The curated grammar set, by the name `highlighter.ts` registers each under. A fence
 * naming anything else renders plain: a near-miss grammar colours worse than none. */
export const CURATED = [
  "python",
  "typescript",
  "javascript",
  "bash",
  "shell",
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
  "markdown",
  "ini",
  "dockerfile",
] as const;

export type CuratedLang = (typeof CURATED)[number];

/** What a fence's info string resolves to: a curated grammar, `"text"` for a tag that
 * names something we do not colour (explicitly plain — never auto-detected over the
 * author's word), or null for an untagged fence (auto-detect may try). */
export type FenceLang = CuratedLang | "text" | null;

const ALIASES: Record<string, CuratedLang> = {
  python: "python",
  py: "python",
  py3: "python",
  python3: "python",
  ipython: "python",
  gyp: "python",
  typescript: "typescript",
  ts: "typescript",
  tsx: "typescript",
  mts: "typescript",
  cts: "typescript",
  javascript: "javascript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  node: "javascript",
  bash: "bash",
  sh: "bash",
  zsh: "bash",
  ksh: "bash",
  shell: "bash",
  shellscript: "bash",
  // A prompt-and-output transcript: hljs's `shell` grammar colours the `$ cmd` lines only.
  console: "shell",
  "shell-session": "shell",
  shellsession: "shell",
  terminal: "shell",
  json: "json",
  jsonc: "json",
  json5: "json",
  jsonl: "json",
  yaml: "yaml",
  yml: "yaml",
  sql: "sql",
  postgres: "sql",
  postgresql: "sql",
  psql: "sql",
  pgsql: "sql",
  mysql: "sql",
  sqlite: "sql",
  plpgsql: "sql",
  rust: "rust",
  rs: "rust",
  c: "c",
  h: "c",
  cpp: "cpp",
  "c++": "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  hh: "cpp",
  hxx: "cpp",
  "h++": "cpp",
  go: "go",
  golang: "go",
  xml: "xml",
  html: "xml",
  htm: "xml",
  xhtml: "xml",
  svg: "xml",
  rss: "xml",
  plist: "xml",
  css: "css",
  diff: "diff",
  patch: "diff",
  udiff: "diff",
  markdown: "markdown",
  md: "markdown",
  mkd: "markdown",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  conf: "ini",
  properties: "ini",
  editorconfig: "ini",
  dockerfile: "dockerfile",
  docker: "dockerfile",
  containerfile: "dockerfile",
};

/** The language a fence line (```` ```python title="x" ````) names. Only the first word of
 * the info string counts, and a pandoc-style `{.python}` / `.python` is read the same way. */
export function fenceLang(fenceLine: string): FenceLang {
  const info = fenceLine
    .trim()
    .replace(/^`{3,}/, "")
    .trim();
  const word = /^\{?\.?([\w+#.-]+)/.exec(info)?.[1]?.toLowerCase();
  if (!word) return null;
  return ALIASES[word] ?? "text";
}

/** Past these, colour is not worth the work: a block this size is a dump, and tokenizing
 * it on the main thread would stall the chat for no reader's benefit. */
export const MAX_HIGHLIGHT_CHARS = 60_000;
export const MAX_HIGHLIGHT_LINES = 2_000;

export function withinHighlightCaps(code: string): boolean {
  if (code.length > MAX_HIGHLIGHT_CHARS) return false;
  let lines = 1;
  for (let i = code.indexOf("\n"); i !== -1; i = code.indexOf("\n", i + 1)) {
    if (++lines > MAX_HIGHLIGHT_LINES) return false;
  }
  return true;
}

/** A numbered listing split into its gutter and its code. `numbers[i]` is line i's number
 * as written ("" for a line that carried none, e.g. an elision mark). */
export interface Gutter {
  numbers: string[];
  code: string;
}

// A number, then a tab, two-plus spaces, or the end of the line (a blank source line whose
// trailing separator was trimmed). The GitHub reader's blob view writes `{n:>w}  {line}`;
// one space is NOT enough — "1 apple" is a list, not a listing.
const NUMBERED = /^( *)(\d{1,7})(?:\t| {2}|\s*$)/;

/** The gutter, when (almost) every line opens with a line number and the numbers run
 * consecutively. Consecutive is the guard against an ordinary numbered list of data: a
 * table of counts or years almost never steps by exactly one. A line without a number is
 * tolerated (an elision `…`), up to one in ten, and the count may jump past
 * one — the elided lines — but never go backwards. */
export function splitGutter(code: string): Gutter | null {
  const lines = code.split("\n");
  while (lines.length > 0 && (lines[lines.length - 1] ?? "").trim() === "") lines.pop();
  if (lines.length < 2) return null;
  const numbers: string[] = [];
  const body: string[] = [];
  let numbered = 0;
  let unnumbered = 0;
  let prev: number | null = null;
  let afterGap = false;
  for (const line of lines) {
    const m = NUMBERED.exec(line);
    if (!m) {
      if (line.trim() !== "") unnumbered++;
      numbers.push("");
      body.push(line);
      afterGap = true;
      continue;
    }
    const n = Number(m[2]);
    if (prev !== null && (afterGap ? n <= prev : n !== prev + 1)) return null;
    prev = n;
    afterGap = false;
    numbered++;
    numbers.push(m[2] ?? "");
    body.push(line.slice(m[0].length));
  }
  // One elision is allowed in a short quote too, once four numbered lines vouch for it.
  const allowed = Math.max(Math.floor((numbered + unnumbered) / 10), numbered >= 4 ? 1 : 0);
  if (numbered < 2 || unnumbered > allowed) return null;
  return { numbers, code: body.join("\n") };
}

// --- hljs scopes → the closed token set ---------------------------------------------------
//
// The stylesheet owns the colours; a class here is a NAME. These are the same five tokens the
// `code_run` view always used (keyword / string / number / function / comment) plus two:
// a type and a meta token, so a class name and a decorator read as something other than prose.

export type Tok = "k" | "s" | "n" | "f" | "c" | "t" | "m";

const SCOPE_TOKEN: Record<string, Tok> = {
  keyword: "k",
  literal: "k",
  "selector-tag": "k",
  "selector-pseudo": "k",
  "template-tag": "k",
  name: "k",
  string: "s",
  regexp: "s",
  addition: "s",
  link: "s",
  "char.escape": "s",
  number: "n",
  symbol: "n",
  bullet: "n",
  "title.function": "f",
  "title.function.invoke": "f",
  title: "f",
  section: "f",
  attr: "f",
  attribute: "f",
  property: "f",
  "selector-class": "f",
  "selector-id": "f",
  "selector-attr": "f",
  comment: "c",
  quote: "c",
  type: "t",
  built_in: "t",
  "title.class": "t",
  "title.class.inherited": "t",
  class: "t",
  meta: "m",
  deletion: "m",
  variable: "m",
  "template-variable": "m",
  "variable.language": "k",
  "variable.constant": "n",
};

/** The token for one hljs span's class list (`["hljs-title", "function_"]` → `title.function`
 * → "f"). The most specific known scope wins; an unknown one inherits its parent's colour. */
export function tokenFor(classNames: readonly string[]): Tok | null {
  const head = classNames.find((c) => c.startsWith("hljs-"));
  if (!head) return null;
  const parts = [
    head.slice(5),
    ...classNames
      .filter((c) => c !== head && !c.startsWith("hljs-"))
      .map((c) => c.replace(/_+$/, "")),
  ];
  for (let n = parts.length; n > 0; n--) {
    const tok = SCOPE_TOKEN[parts.slice(0, n).join(".")];
    if (tok) return tok;
  }
  return null;
}
