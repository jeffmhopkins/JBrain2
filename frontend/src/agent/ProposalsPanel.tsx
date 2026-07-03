// The Proposals panel (right swipe from Full Brain): staged changes awaiting the
// owner. The agent never writes truth directly — it proposes, and the owner enacts
// (docs/reference/ASSISTANT.md "Staging & approval"). Tapping a row opens its tree, where the
// owner approves/rejects nodes and enacts.

import type { ReactNode } from "react";
import type { ProposalKind, ProposalSummary } from "./types";

const BADGE: Record<ProposalKind, string> = {
  "wiki-restructure": "⤴",
  knowledge: "＋",
  merge: "⧉",
  correction: "✎",
  appointment: "◷",
  egress: "↗",
  "intake-link": "🔗",
  "intake-submission": "📥",
};

function subtitle(p: ProposalSummary): string {
  const ops = `${p.node_count} operation${p.node_count === 1 ? "" : "s"}`;
  return `${p.kind} · ${ops}`;
}

interface Props {
  proposals: ProposalSummary[];
  onOpen: (proposal: ProposalSummary) => void;
  onClose: () => void;
}

export function ProposalsPanel({ proposals, onOpen, onClose }: Props): ReactNode {
  return (
    <section className="panel-content" aria-label="Proposals">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to chat" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">Proposals</span>
        <span className="sub">staged · awaiting you</span>
      </div>
      <div className="panel-body">
        {proposals.length === 0 && (
          <div className="panel-empty">
            Nothing staged. When the agent wants to change something, it appears here for your
            approval — it never writes on its own.
          </div>
        )}
        {proposals.map((p) => (
          <button type="button" className="row proposal-row" key={p.id} onClick={() => onOpen(p)}>
            <div className="r-head">
              <span className="badge">{BADGE[p.kind] ?? "•"}</span> {p.title || "(untitled)"}
            </div>
            <div className="r-sub">{subtitle(p)}</div>
            <div className="r-meta">
              {p.status}
              <span className={`pill ${p.domain}`}>{p.domain}</span>
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}
