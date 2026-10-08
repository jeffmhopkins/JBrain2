// The `code_run` tool-view: what `run_python` and `calculate` actually did
// (docs/archive/SHOW_THE_WORKING_PLAN.md W2 — binding mock
// docs/mocks/code-run/h-worked-ledger.html).
//
// One component, two tools. `calculate` fills the same slots with an expression in place of
// the code and an exact/decimal pair in place of stdout, because they are the same act: a
// number the owner should be able to check. A second component would be a second place for
// them to disagree.
//
// SYNTAX HIGHLIGHTING IS OWNED BY THE CLIENT, not sent. The model fills data-only slots and
// authors no markup, colour or URL (DESIGN.md invariant #1/#9) — `language` selects a
// grammar whose output maps onto a closed set of token classes the stylesheet colours, so a
// snippet cannot smuggle a span into the transcript by printing one. It is the same
// highlighter chat code blocks use (../codeBlock.tsx), which walks a token tree into React
// text and spans: there is no path from tool output to the DOM as markup.

import type { ReactNode } from "react";
import { useHighlighted } from "../codeBlock";
import type { ViewProps } from "./registry";

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
  // Only Python is coloured: `calculate`'s "expression" is arithmetic, not a program.
  const coloured = useHighlighted(body, language === "python" ? "python" : "text", true);
  if (!body.trim()) return null;
  return (
    <>
      <div className="fb-res-lab">{label}</div>
      <pre className={out ? "fb-code out" : "fb-code"}>{language ? coloured : body}</pre>
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
