// Turn a raw tool activity into a tidy "step" for the collapsed Worked block
// under a response. Pure so it's unit-testable. Structured `sources` from the
// result event are preferred; we fall back to parsing the known search/read_note
// summary text (backend readtools) so older streams still render. Other tools
// just get a friendly label.

import type { SourceRef, ToolActivity } from "./transcript";
import type { EntityRef } from "./types";

export type { SourceRef };

export interface ToolStep {
  id: string;
  name: string;
  ok: boolean | undefined;
  label: string;
  sources: SourceRef[];
  /** Entities the tool resolved — rendered as tappable links in the expanded
   * step, so a name reaches its page without exposing the raw id. */
  entities: EntityRef[];
  /** The call's arguments, for the expanded-step "arguments" list. */
  args: Record<string, unknown> | undefined;
  /** The verbatim result text, for the expanded step's result/raw rung. */
  summary: string | undefined;
  /** A humanized stand-in for `summary` when the verbatim text is machine-shaped
   * (the appointment tools speak ids + bracket syntax so the model can chain).
   * Shown as the step's result; the raw text stays behind the "raw result" rung. */
  display: string | undefined;
}

const STEP_LABELS: Record<string, string> = {
  search: "Searched your notes",
  read_note: "Read a note",
  read_entity: "Read an entity",
  find_entity: "Found an entity",
  relate: "Followed a relationship",
  recall: "Recalled past notes",
  memory_read: "Read memory",
  memory_edit: "Updated its scratchpad",
  remember: "Staged a memory change",
  propose_correction: "Staged a proposal",
  read_appointments: "Checked your calendar",
  read_appointment: "Read an appointment",
  manage_appointment: "Staged an appointment change",
  queued: "Queued a job",
};

function stepLabel(name: string): string {
  if (STEP_LABELS[name]) return STEP_LABELS[name];
  if (name.startsWith("lookup_")) return `Checked ${name.slice(7).replace(/_/g, " ")}`;
  return name;
}

// "- note <id> [<domain>] <YYYY-MM-DD>: <snippet>"
const SEARCH_LINE = /^- note (\S+) \[(\w+)\] \d{4}-\d{2}-\d{2}: (.*)$/;
// "note <id> [<domain>] <YYYY-MM-DD>" then body on following lines
const NOTE_HEAD = /^note (\S+) \[(\w+)\] \d{4}-\d{2}-\d{2}/;

const stripMarks = (s: string): string => s.replace(/<\/?mark>/g, "").trim();

function searchSources(summary: string): SourceRef[] {
  const out: SourceRef[] = [];
  for (const line of summary.split("\n")) {
    const m = SEARCH_LINE.exec(line);
    if (m?.[1] && m[2]) out.push({ noteId: m[1], domain: m[2], text: stripMarks(m[3] ?? "") });
  }
  return out;
}

function noteSource(summary: string): SourceRef[] {
  const lines = summary.split("\n");
  const m = lines[0] ? NOTE_HEAD.exec(lines[0]) : null;
  if (!m?.[1] || !m[2]) return [];
  const body = lines.slice(1).find((l) => l.trim()) ?? "";
  return [{ noteId: m[1], domain: m[2], text: stripMarks(body) || "(empty note)" }];
}

// The appointment read tools speak a compact machine line — title, when, domain
// in brackets, and an id the model chains on (read_appointments → read_appointment).
// None of that reads as human: strip the id, soften the bracket/label syntax, and
// leave a plain summary. The verbatim text stays one tap down under "raw result".
// "- <title> — <when> [<domain>]<tags> id=<uuid>"
const APPT_LIST_LINE = /^- (.+?) — (.+?) \[(\w+)\](.*?) id=\S+$/;
// "<title> [<domain>]" — the head of a single appointment read.
const APPT_HEAD = /^(.+?) \[(\w+)\]$/;
// "when:|status:|location:|repeats:|with: <value>" — its labelled detail lines.
const APPT_FIELD = /^(when|status|location|repeats|with): (.*)$/;
const APPT_FIELD_LABEL: Record<string, string> = {
  when: "When",
  status: "Status",
  location: "Location",
  repeats: "Repeats",
  with: "With",
};

function humanizeAppointments(summary: string): string {
  if (summary.trim() === "No appointments in scope.") return "No appointments found.";
  return summary
    .split("\n")
    .map((line) => {
      const list = APPT_LIST_LINE.exec(line);
      if (list) return `${list[1]} — ${list[2]} (${list[3]})${list[4]}`;
      const head = APPT_HEAD.exec(line);
      if (head) return `${head[1]} (${head[2]})`;
      const field = APPT_FIELD.exec(line);
      if (field?.[1]) return `${APPT_FIELD_LABEL[field[1]]}: ${field[2]}`;
      return line; // an error string or anything unrecognized passes through
    })
    .join("\n");
}

export function toolStep(t: ToolActivity): ToolStep {
  let sources: SourceRef[];
  if (t.sources && t.sources.length > 0) {
    // Structured from the result event — just strip search highlight marks.
    sources = t.sources.map((s) => ({ ...s, text: stripMarks(s.text) }));
  } else if (t.summary && t.name === "search") {
    sources = searchSources(t.summary);
  } else if (t.summary && t.name === "read_note") {
    sources = noteSource(t.summary);
  } else {
    sources = [];
  }
  const isAppt = t.name === "read_appointments" || t.name === "read_appointment";
  return {
    id: t.id,
    name: t.name,
    ok: t.ok,
    label: stepLabel(t.name),
    sources,
    entities: t.entities ?? [],
    args: t.args,
    summary: t.summary,
    display: isAppt && t.summary ? humanizeAppointments(t.summary) : undefined,
  };
}
