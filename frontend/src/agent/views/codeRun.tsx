// The `code_run` tool-view: what `run_python` and `calculate` actually did
// (docs/archive/SHOW_THE_WORKING_PLAN.md W2 — binding mock
// docs/mocks/code-run/h-worked-ledger.html).
//
// One component, two tools. `calculate` fills the same slots with an expression in place of
// the code and an exact/decimal pair in place of stdout, because they are the same act: a
// number the owner should be able to check. A second component would be a second place for
// them to disagree.
//
// SYNTAX HIGHLIGHTING IS OWNED HERE, not sent. The model fills data-only slots and authors
// no markup, colour or URL (DESIGN.md invariant #1/#9) — `language` selects a closed set of
// token classes the stylesheet colours, so a snippet cannot smuggle a span into the
// transcript by printing one. That is also why the code is tokenized from plain text rather
// than rendered as HTML: there is no path from tool output to the DOM as markup.

import type { ReactNode } from "react";
import type { ViewProps } from "./registry";

/** The closed token set. A class here is a NAME, never a colour — the stylesheet owns the
 * palette, so a theme change is a stylesheet change (DESIGN.md). */
type Token = "k" | "s" | "n" | "f" | "c" | "";

const PY_KEYWORDS = new Set([
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
  "True",
  "False",
  "None",
]);

// One pass, ordered so the greedy things win: comments and strings before anything can be
// found inside them, then numbers, then words. Nothing here can match across the whole
// input unboundedly — every alternative is anchored to a short shape, so a pathological
// snippet cannot turn this into a hang.
const PY_TOKENS =
  /(#[^\n]*)|('''[\s\S]*?'''|"""[\s\S]*?"""|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")|(\b\d[\d_]*(?:\.\d+)?\b)|([A-Za-z_]\w*)/g;

function classify(match: RegExpExecArray, source: string): Token {
  if (match[1] !== undefined) return "c";
  if (match[2] !== undefined) return "s";
  if (match[3] !== undefined) return "n";
  const word = match[4];
  if (word === undefined) return "";
  if (PY_KEYWORDS.has(word)) return "k";
  // A call, not a name: the character after the word decides, which is enough to make a
  // listing readable without pretending to parse Python.
  return source[match.index + word.length] === "(" ? "f" : "";
}

/** Plain text in, React nodes out. Never `dangerouslySetInnerHTML` — the whole point is
 * that model-authored text reaches the DOM as TEXT. */
function highlight(code: string, language: string): ReactNode[] {
  if (language !== "python") return [code];
  const out: ReactNode[] = [];
  let last = 0;
  PY_TOKENS.lastIndex = 0;
  let match = PY_TOKENS.exec(code);
  while (match !== null) {
    const cls = classify(match, code);
    if (match.index > last) out.push(code.slice(last, match.index));
    const text = match[0];
    out.push(
      cls ? (
        <span key={`${match.index}`} className={cls}>
          {text}
        </span>
      ) : (
        text
      ),
    );
    last = match.index + text.length;
    match = PY_TOKENS.exec(code);
  }
  if (last < code.length) out.push(code.slice(last));
  return out;
}

function str(value: unknown): string {
  return typeof value === "string"
    ? value
    : value === null || value === undefined
      ? ""
      : String(value);
}

function Section({
  label,
  body,
  language,
  out,
}: {
  label: string;
  body: string;
  /** Present on the CODE section only: output is never highlighted, because colouring a
   * program's own stdout would let a snippet print something that looks like syntax. */
  language?: string;
  out?: boolean;
}): ReactNode {
  if (!body.trim()) return null;
  return (
    <>
      <div className="fb-res-lab">{label}</div>
      <pre className={out ? "fb-code out" : "fb-code"}>
        {language ? highlight(body, language) : body}
      </pre>
    </>
  );
}

export function CodeRun({ data }: ViewProps): ReactNode {
  const language = str(data.language) || "python";
  const code = str(data.code);
  const ok = data.ok !== false;
  const duration = typeof data.duration_ms === "number" ? data.duration_ms : undefined;
  const seals = Array.isArray(data.containment) ? data.containment.map(String) : [];
  const error = str(data.error);
  const result = str(data.result);
  const decimal = str(data.decimal);

  // The run's own verdict chip. `ok` is whether the code RAN, not whether it was right —
  // so the wording says "ran clean", never "correct".
  const verdict = ok ? "ran clean" : "raised";
  const timing = duration === undefined ? "" : ` · ${duration}ms`;

  return (
    <div className="tv-coderun">
      <Section
        label={language === "expression" ? "expression" : "code"}
        body={code}
        language={language}
      />
      <Section label="stdout" body={str(data.stdout)} out />
      <Section label="stderr" body={str(data.stderr)} out />
      {error && (
        <>
          <div className="fb-res-lab">error</div>
          <div className="fb-res-txt err">{error}</div>
        </>
      )}
      {result && (
        <>
          <div className="fb-res-lab">result</div>
          <div className="fb-res-txt">{result}</div>
        </>
      )}
      {/* `calculate`'s second half: the decimal beside the exact value, shown only when it
          says something the exact form does not (the backend omits it otherwise). */}
      {decimal && decimal !== result && (
        <>
          <div className="fb-res-lab">decimal</div>
          <div className="fb-res-txt">{decimal}</div>
        </>
      )}
      {data.truncated === true && (
        <div className="fb-res-txt err">the output was long and was cut short</div>
      )}
      <div className="fb-sealrow">
        <span className={ok ? "sealchip ok" : "sealchip bad"}>
          {verdict}
          {timing}
        </span>
        {/* Where it ran. Backend-supplied, and each phrase is tied to a real declaration by
            test_pysandbox_server.py — a containment claim nobody checks is worth less than
            no claim at all. */}
        {seals.length > 0 && <span className="sealchip">{seals.join(" · ")}</span>}
      </div>
    </div>
  );
}
