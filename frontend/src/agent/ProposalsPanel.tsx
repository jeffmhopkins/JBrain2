// The Proposals panel (right swipe from Full Brain): staged changes awaiting the
// owner. The agent never writes truth directly — it proposes, and the owner
// enacts (docs/ASSISTANT.md "Staging & approval"). The Proposal data model and
// the full per-node approval tree land in P4.8; this panel renders the summary
// list (empty until then), so the swipe shortcut exists now and fills in later.

import type { ReactNode } from "react";

export type ProposalKind =
  | "correction"
  | "knowledge"
  | "wiki-restructure"
  | "prompt-edit"
  | "egress";

export interface ProposalSummary {
  id: string;
  kind: ProposalKind;
  title: string;
  subtitle: string;
  meta: string;
  domain?: string;
}

const BADGE: Record<ProposalKind, string> = {
  "wiki-restructure": "⤴",
  knowledge: "＋",
  correction: "✎",
  "prompt-edit": "⌥",
  egress: "↗",
};

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
              <span className="badge">{BADGE[p.kind]}</span> {p.title}
            </div>
            <div className="r-sub">{p.subtitle}</div>
            <div className="r-meta">
              {p.meta}
              {p.domain && <span className={`pill ${p.domain}`}>{p.domain}</span>}
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}
