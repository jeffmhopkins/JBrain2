// Turn a raw tool activity into a tidy "step" for the collapsed Worked block
// under a response. Pure so it's unit-testable. search/read_note carry their
// results as a known text format (see backend readtools.format_search /
// format_note); we parse those into source refs the UI can list and open. Other
// tools just get a friendly label.

import type { ToolActivity } from "./transcript";

export interface SourceRef {
  noteId: string;
  domain: string;
  /** A one-line, highlight-stripped snippet to show on the card. */
  text: string;
}

export interface ToolStep {
  id: string;
  name: string;
  ok: boolean | undefined;
  label: string;
  sources: SourceRef[];
}

const STEP_LABELS: Record<string, string> = {
  search: "Searched your notes",
  read_note: "Read a note",
  read_entity: "Read an entity",
  recall: "Recalled past notes",
  memory_read: "Read memory",
  memory_edit: "Updated its scratchpad",
  remember: "Staged a memory change",
  propose_correction: "Staged a proposal",
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

export function toolStep(t: ToolActivity): ToolStep {
  let sources: SourceRef[] = [];
  if (t.summary && t.name === "search") sources = searchSources(t.summary);
  else if (t.summary && t.name === "read_note") sources = noteSource(t.summary);
  return { id: t.id, name: t.name, ok: t.ok, label: stepLabel(t.name), sources };
}
