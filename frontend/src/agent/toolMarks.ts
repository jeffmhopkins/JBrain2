// Inline tool marks: where the model wrote some text, called tools, then wrote more, the
// answer carries a quiet mark at that point (binding mock
// docs/mocks/browse-trace/inline-tool-marks.html, owner request 2026-10-06). Pure, so the
// placement and the wording are testable without rendering a bubble.

import { stepLabel } from "./toolSummary";
import type { ToolActivity } from "./transcript";

export interface ToolMarkGroup {
  /** Stable within a turn: the first tool's id. */
  key: string;
  /** Where in the answer text the tools ran (`ToolActivity.textOffset`). */
  offset: number;
  /** The steps the mark opens, in call order. */
  ids: string[];
  /** The tool every step in the group shares, for the glyph; undefined when they differ. */
  name: string | undefined;
  label: string;
  /** A browse step's verdict beside its label ("· verified"). */
  brief?: { text: string; ok: boolean };
}

/** The marks for a turn's tools. A tool at offset 0 (before any text) gets none: the ledger
 * already lists it. One at the end of the text shown so far gets its mark at once (owner,
 * 2026-10-06: the latest tool should show the same way while the model goes on thinking),
 * and one past it waits until the paced reveal reaches it. Consecutive tools at the same offset share one mark:
 * "Searched the web ×2" when they are the same tool, "3 tools used" when not. */
export function toolMarks(tools: readonly ToolActivity[], textLength: number): ToolMarkGroup[] {
  const groups: { offset: number; tools: ToolActivity[] }[] = [];
  for (const tool of tools) {
    const offset = tool.textOffset;
    if (offset === undefined || offset <= 0 || offset > textLength) continue;
    const last = groups.at(-1);
    if (last && last.offset === offset) last.tools.push(tool);
    else groups.push({ offset, tools: [tool] });
  }
  return groups.map(({ offset, tools: group }) => {
    const first = group[0] as ToolActivity;
    const same = group.every((t) => t.name === first.name);
    const label =
      group.length === 1
        ? stepLabel(first.name)
        : same
          ? `${stepLabel(first.name)} ×${group.length}`
          : `${group.length} tools used`;
    const brief = group.length === 1 ? browseBrief(first) : undefined;
    return {
      key: first.id,
      offset,
      ids: group.map((t) => t.id),
      name: same ? first.name : undefined,
      label,
      ...(brief ? { brief } : {}),
    };
  });
}

/** A browse step's verdict, off the handler's own one-line answer (`result_brief`,
 * "verified · 6 steps") — never the model-facing text. */
function browseBrief(tool: ToolActivity): { text: string; ok: boolean } | undefined {
  if (tool.name !== "browse" || !tool.result) return undefined;
  const head = tool.result.split(" · ")[0]?.trim();
  if (!head) return undefined;
  return { text: head, ok: head === "verified" };
}
