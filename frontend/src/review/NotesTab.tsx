// The review inbox's notes tab (binding mock: docs/mocks/agent-ingest-inbox/
// a-inbox-holds.html, variant A). Ingestion questions and staged approvals, oldest wait
// first so the list drains from the top.
//
// Every row is a REDIRECT and nothing else (D4). There are no answer controls here —
// they were in the reviewed draft and the owner's ruling removed them, because
// answering in the inbox as well as in the thread would make this a second place note
// ingestion gets decided. The row's whole job is to carry enough (which note, what is
// asked, how long, how much already landed) to be worth the tap that opens the thread.

import type { ReactNode } from "react";
import type { NotesInboxRow } from "../api/client";
import { DomainDot } from "./DomainDot";

/** How long it has waited, in the register the mock uses ("asked 9 days ago"). Coarse
 * on purpose: an exact clock on an unanswered question is nagging, which D5 rules out. */
export function waitedFor(since: string, now: Date = new Date()): string {
  const ms = now.getTime() - new Date(since).getTime();
  const hours = Math.floor(ms / 3_600_000);
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function whenLabel(row: NotesInboxRow): string {
  const verb = row.kind === "approval" ? "staged" : row.live ? "started" : "asked";
  return `${verb} ${waitedFor(row.waiting_since)}`;
}

function sourceLabel(row: NotesInboxRow): string {
  if (row.kind === "approval") return "preferences";
  if (row.captured_at === null) return "note";
  const d = new Date(row.captured_at);
  return `note · ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
}

function NotesRow({ row, onOpen }: { row: NotesInboxRow; onOpen: () => void }): ReactNode {
  return (
    <div className="rrow2 nrow">
      <button type="button" className="rrow-open" onClick={onOpen}>
        <span className="rrow-line">
          <DomainDot domain={row.domain} />
          <span className="nrow-src">{sourceLabel(row)}</span>
          <span className="rrow-when">{whenLabel(row)}</span>
        </span>
        <span className="nrow-quote">{row.quote}</span>
        {row.ask !== null && <span className="nrow-ask">{row.ask}</span>}
        <span className="rrow-meta nrow-meta">
          {row.live ? (
            <span className="nchip nchip-work">still reading · nothing written yet</span>
          ) : (
            <>
              <span className="nchip nchip-ask">{row.kind}</span>
              {row.committed > 0 && (
                <span className="nchip nchip-ok">{row.committed} committed</span>
              )}
            </>
          )}
        </span>
      </button>
      <span className="rrow-chev" aria-hidden="true">
        ›
      </span>
    </div>
  );
}

export function NotesTab({
  rows,
  loadError,
  onOpenConversation,
}: {
  rows: NotesInboxRow[] | null;
  loadError: boolean;
  onOpenConversation: (sessionId: string, agent: string) => void;
}): ReactNode {
  if (loadError) return <p className="analysis-quiet">couldn't load — reopen to retry.</p>;
  if (rows === null) return <p className="analysis-quiet">loading…</p>;
  if (rows.length === 0) {
    // The honest calm state: no zero to clear, no dot, no colour — the absence is
    // the state (the mock's frame 3).
    return (
      <p className="analysis-quiet rlane-empty">
        nothing is waiting on you — the agent has been committing as it reads.
      </p>
    );
  }
  return (
    <div className="rlist2">
      {rows.map((row) => (
        <NotesRow
          key={`${row.kind}:${row.session_id}`}
          row={row}
          onOpen={() => onOpenConversation(row.session_id, row.agent)}
        />
      ))}
    </div>
  );
}
