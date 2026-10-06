// The `browse_trace` step view: what the browse sub-agent did on a site (binding mock
// docs/mocks/browse-trace/a-timeline.html, owner pick 2026-10-06). It renders inside the
// expanded "Browsed a site" step, above the shipped sources rung.
//
// Level 1 is the rail: one rung per model turn, in the host's plain words, with the host's
// own work (the start page, the fact check) drawn as hollow / ringed dots so it never reads
// as the model's. Level 2, inside a rung: each command with the element it acted on, the
// timings and tokens, then the shipped Thinking/Worked chip strip as Thinking / Actions /
// Page it saw over one panel.
//
// EVERY STRING HERE IS PLAIN TEXT. Titles, element names, excerpts, the model's reasoning and
// its call all came from a web page or a model reading one; the backend quarantines them
// (`browse_trace.py`) and this component only ever puts them in text nodes — no markdown, no
// HTML, no links. The call's colouring is a closed token set the component applies, the same
// rule as `code_run`.

import { type ReactNode, useState } from "react";
import { BrainGlyph } from "../glyphs";
import type { ViewProps } from "./registry";

type Rec = Record<string, unknown>;

function rec(value: unknown): Rec {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Rec) : {};
}
function recs(value: unknown): Rec[] {
  return Array.isArray(value) ? value.map(rec) : [];
}
function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}
function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** "3.1 s", "0.04 s" — the rail's one time unit. */
export function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(ms < 1000 ? 2 : 1)} s`;
}
function count(n: number): string {
  return n.toLocaleString("en-US");
}

/** A host summary with its quoted names set as `<q>`, so the name reads in the text colour
 * the mock gives it — split on the host's own “…” quotes, still text nodes throughout. */
function quoted(text: string): ReactNode[] {
  return text.split(/“([^”]*)”/).map((part, i) =>
    i % 2 === 1 ? (
      // biome-ignore lint/suspicious/noArrayIndexKey: split order is stable
      <q key={i}>{part}</q>
    ) : (
      part
    ),
  );
}

/** The call, coloured from a closed set: a JSON key, a string, a number. */
export function highlightCall(call: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /("(?:\\.|[^"\\\n])*")(\s*:)?|(-?\b\d+(?:\.\d+)?\b)/g;
  let last = 0;
  for (const m of call.matchAll(re)) {
    const at = m.index ?? 0;
    if (at > last) out.push(call.slice(last, at));
    if (m[1] !== undefined) {
      out.push(
        <span key={at} className={m[2] !== undefined ? "k" : "s"}>
          {m[1]}
        </span>,
      );
      if (m[2]) out.push(m[2]);
    } else {
      out.push(
        <span key={at} className="n">
          {m[0]}
        </span>,
      );
    }
    last = at + m[0].length;
  }
  if (last < call.length) out.push(call.slice(last));
  return out;
}

function Caret(): ReactNode {
  return (
    <svg className="bt-car" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
function ActIcon(): ReactNode {
  return (
    <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 3l14 9-6 1.5L10 20z" />
    </svg>
  );
}
function PageIcon(): ReactNode {
  return (
    <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="4" y="3" width="16" height="18" rx="2" />
      <path d="M8 8h8M8 12h8M8 16h5" />
    </svg>
  );
}

const MARK: Record<string, string> = { ok: "✓", refused: "✕", failed: "✕", not_run: "·" };

/** `[41] button "Tue Oct 6"` with its handle set apart, as the snapshot names it. */
function Element({ text }: { text: string }): ReactNode {
  const m = /^(\[\d+\]|e\d+)\s(.*)$/.exec(text);
  if (!m) return <span className="el">{text}</span>;
  return (
    <span className="el">
      <i>{m[1]}</i> {m[2]}
    </span>
  );
}

function Command({ c }: { c: Rec }): ReactNode {
  const status = str(c.status);
  const cls =
    status === "refused" || status === "failed" ? " bad" : status === "not_run" ? " skip" : "";
  const element = str(c.element);
  const note = str(c.note);
  return (
    <li className={`bt-cmd${cls}`}>
      <span className="m">{MARK[status] ?? "·"}</span>
      <span>
        {quoted(str(c.text))}
        {element && <Element text={element} />}
        {c.refound === true && (
          <span className="re">↻ the page re-rendered under it — found again by name</span>
        )}
        {cls === " bad" && note && <span className="why">{note}</span>}
        {str(c.moved_to) && <span className="re">→ landed on {str(c.moved_to)}</span>}
      </span>
      <span className="tt">
        {status === "not_run" ? "not run" : status === "ok" ? seconds(num(c.ms)) : "—"}
      </span>
    </li>
  );
}

/** What the host did with each command, the Actions panel's second half. */
function hostLines(commands: Rec[]): { cls: string; mark: string; text: string }[] {
  const out: { cls: string; mark: string; text: string }[] = [];
  commands.forEach((c, i) => {
    const status = str(c.status);
    const n = `${i + 1} `;
    const element = str(c.element);
    const settle = num(c.settle_ms);
    if (settle)
      out.push({ cls: "dim", mark: "·", text: `  settle ${seconds(settle)}, look again` });
    if (status === "not_run") {
      out.push({
        cls: "dim",
        mark: "·",
        text: `${n}${str(c.text)} → not run (the batch stops at a refusal or a new address)`,
      });
      return;
    }
    const target = element ? ` ${element}` : "";
    if (status === "ok") {
      const re = c.refound === true ? " · stale, re-found by role and name" : "";
      out.push({
        cls: c.refound === true ? "re" : "ok",
        mark: c.refound === true ? "↻" : "✓",
        text: `${n}${str(c.text)} →${target}${re} · ${str(c.note) || "done"} · ${seconds(num(c.ms))}`,
      });
      if (str(c.moved_to)) {
        out.push({ cls: "ok", mark: "✓", text: `→ page moved to ${str(c.moved_to)}` });
      }
      return;
    }
    out.push({
      cls: "bad",
      mark: "✕",
      text: `${n}${str(c.text)}${target ? ` →${target}` : ""} → ${status}: ${str(c.note)}`,
    });
  });
  return out;
}

type Panel = "think" | "act" | "page" | null;

function Rung({ rung, loop }: { rung: Rec; loop: string }): ReactNode {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<Panel>(null);
  const host = str(rung.kind) === "host";
  const status = str(rung.status);
  const dot = host ? " host" : status === "bad" ? " bad" : status === "part" ? " part" : "";
  const commands = recs(rung.commands);
  const badges = recs(rung.badges);
  const nums = rec(rung.nums);
  const page = rec(rung.page);
  const reasoning = str(rung.reasoning);
  const call = str(rung.call);
  const title = str(rung.title);
  const landed = commands
    .map((c) => str(c.moved_to))
    .filter(Boolean)
    .at(-1);
  const toggle = (want: Exclude<Panel, null>) => setPanel((cur) => (cur === want ? null : want));
  const ran = commands.filter((c) => str(c.status) !== "not_run").length;

  const numChips: [string, string][] = [];
  if (num(nums.model_ms)) numChips.push(["model", seconds(num(nums.model_ms))]);
  if (num(nums.browser_ms)) numChips.push(["browser", seconds(num(nums.browser_ms))]);
  if (num(nums.settle_ms)) numChips.push(["settle", seconds(num(nums.settle_ms))]);
  if (num(nums.page_tokens)) numChips.push(["page", `${count(num(nums.page_tokens))} tok`]);
  if (num(nums.prompt_tokens)) {
    const cached = num(nums.cached_tokens);
    numChips.push([
      "prompt",
      `${count(num(nums.prompt_tokens))}${cached ? ` · ${count(cached)} cached` : ""}`,
    ]);
  }
  if (num(nums.output_tokens)) numChips.push(["out", count(num(nums.output_tokens))]);

  const noThinking = host
    ? "No model call — the host opens the start address before the first turn."
    : loop === "fast"
      ? "No thinking — fast mode. The fast loop asks for one act call with reasoning off, so this turn has nothing to show here."
      : "No thinking came back with this turn.";

  return (
    <li className={`bt-rung${open ? " open" : ""}`}>
      <button
        type="button"
        className="bt-row"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`bt-dot${dot}`} />
        <span className="bt-main">
          <span className="bt-lab">{quoted(str(rung.summary))}</span>
          {(badges.length > 0 || landed) && (
            <span className="bt-sub">
              {badges.map((b, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: badge order is stable
                <span key={i} className={`bt-badge ${str(b.kind)}`}>
                  {str(b.text)}
                </span>
              ))}
              {landed && (
                <span>
                  landed on <span className="url">{landed}</span>
                </span>
              )}
            </span>
          )}
          {title && (
            <span className="bt-sub">
              <span>→ {title}</span>
            </span>
          )}
        </span>
        <span className="bt-t">{seconds(num(rung.ms))}</span>
        <Caret />
      </button>
      <div className="bt-det">
        <div className="bt-di">
          {commands.length > 0 && (
            <ul className="bt-cmds">
              {commands.map((c, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: command order is stable
                <Command key={i} c={c} />
              ))}
            </ul>
          )}
          {(numChips.length > 0 || str(rung.url)) && (
            <div className="bt-nums">
              {numChips.map(([k, v]) => (
                <span key={k}>
                  {k} <b>{v}</b>
                </span>
              ))}
              {str(rung.url) && (
                <span>
                  <b className="url">{str(rung.url)}</b>
                </span>
              )}
            </div>
          )}
          <div className="bt-l2">
            <button
              type="button"
              className={`fb-act-chip fb-act-think${panel === "think" ? " on" : ""}`}
              aria-expanded={panel === "think"}
              onClick={() => toggle("think")}
            >
              <BrainGlyph className="fb-act-ic" />
              <span className="fb-act-lab">Thinking</span>
              {!reasoning && <span className="fb-act-count"> · none</span>}
            </button>
            <button
              type="button"
              className={`fb-act-chip fb-act-work${panel === "act" ? " on" : ""}`}
              aria-expanded={panel === "act"}
              onClick={() => toggle("act")}
            >
              <ActIcon />
              <span className="fb-act-lab">Actions</span>
              {ran > 0 && <span className="fb-act-count"> · {ran}</span>}
            </button>
            <button
              type="button"
              className={`fb-act-chip fb-act-page${panel === "page" ? " on" : ""}`}
              aria-expanded={panel === "page"}
              onClick={() => toggle("page")}
            >
              <PageIcon />
              <span className="fb-act-lab">Page it saw</span>
            </button>
          </div>
          {panel === "think" && (
            <div className="bt-pan show">
              {reasoning ? (
                <div className="fb-thinking-trace bt-think">{reasoning}</div>
              ) : (
                <div className="bt-none">{noThinking}</div>
              )}
            </div>
          )}
          {panel === "act" && (
            <div className="bt-pan show">
              <div className="bt-sec">the call</div>
              {call ? (
                <pre className="bt-pre">{highlightCall(call)}</pre>
              ) : (
                <div className="bt-none">none — host step</div>
              )}
              <div className="bt-sec">what the host did</div>
              <ul className="bt-host">
                {hostLines(commands).map((l, i) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: line order is stable
                  <li key={i}>
                    <span>{l.mark}</span>
                    <span className={l.cls}>{l.text}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {panel === "page" && (
            <div className="bt-pan show">
              {str(page.excerpt) ? (
                <>
                  <div className="bt-sec">
                    {str(page.title) || "untitled"} · {count(num(page.tokens))} tok
                    {page.changes === true ? " · changes only" : ""}
                  </div>
                  <pre className="bt-pre">{str(page.excerpt)}</pre>
                  <div className="bt-cap">
                    first {num(page.lines_shown)} of {num(page.lines_total)} lines
                    {page.changes === true ? " of what changed" : ""}
                  </div>
                </>
              ) : (
                <div className="bt-none">
                  {host
                    ? "The host loaded this page; the model had not seen it yet."
                    : "No page view was kept for this turn."}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

function CheckRung({ check }: { check: Rec }): ReactNode {
  const [open, setOpen] = useState(false);
  const verified = check.verified === true;
  const answered = check.answered === true;
  const rows = recs(check.rows);
  const answer = str(check.answer);
  const error = str(check.error);
  const bad = !answered || !verified;
  return (
    <li className={`bt-rung${open ? " open" : ""}`}>
      <button
        type="button"
        className="bt-row"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`bt-dot check${bad ? " bad" : ""}`} />
        <span className="bt-main">
          <span className="bt-lab bt-lab-strong">{str(check.summary)}</span>
          <span className="bt-sub">
            {answered ? (
              <span className={`bt-badge ${verified ? "ok" : "ref"}`}>
                {verified ? "verified" : "unverified"}
              </span>
            ) : (
              <span className="bt-badge ref">no answer</span>
            )}
            {check.extracted === true ? " the answer was read off the page" : ""}
          </span>
        </span>
        {/* A check with no extraction took no measurable time: no figure rather than 0.00 s. */}
        <span className="bt-t">{num(check.ms) > 0 ? seconds(num(check.ms)) : ""}</span>
        <Caret />
      </button>
      <div className="bt-det">
        <div className="bt-di">
          {answer && <div className="bt-ans">{answer}</div>}
          {rows.length > 0 && (
            <dl className="bt-check">
              {rows.map((r, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: row order is stable
                <div key={i} className="bt-check-row">
                  <dt>{str(r.label)}</dt>
                  <dd className={r.ok === true ? "ok" : r.ok === false ? "bad" : ""}>
                    {str(r.text)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {error && <div className="bt-cap bad">{error}</div>}
          <div className="bt-cap">
            The check is the host's, not the model's: every time and price must be on the page, and
            most names.
          </div>
        </div>
      </div>
    </li>
  );
}

export function BrowseTrace({ data }: ViewProps): ReactNode {
  const steps = recs(data.steps);
  const loop = str(data.loop);
  const pages = num(data.pages);
  return (
    <div className="bt">
      <div className="bt-head">
        {str(data.site) && <b>{str(data.site)}</b>}
        <span>
          {steps.length} step{steps.length === 1 ? "" : "s"} · {pages} page
          {pages === 1 ? "" : "s"} · {seconds(num(data.elapsed_ms))}
        </span>
        {loop && (
          <span className={`bt-pill${loop === "b1" ? " violet" : ""}`}>
            {loop === "b1" ? "B1 loop · thinking on" : "fast loop · thinking off"}
          </span>
        )}
        {data.trimmed === true && <span className="bt-pill">trimmed to fit</span>}
      </div>
      <ol className="bt-rail">
        {steps.map((r, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: rung order is stable
          <Rung key={i} rung={r} loop={loop} />
        ))}
        <CheckRung check={rec(data.check)} />
      </ol>
    </div>
  );
}
