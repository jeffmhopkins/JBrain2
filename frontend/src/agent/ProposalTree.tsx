// The Proposal tree: the owner judges each node by its rendered preview and
// approves or rejects it, then enacts. The agent never writes — enacting runs the
// approved, prerequisite-satisfied leaves through the trusted executor; the rest
// are held (docs/ASSISTANT.md "Staging & approval").

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { Decision, EnactResult, ProposalDetail, ProposalNode } from "./types";

interface Props {
  proposalId: string;
  onClose: () => void;
  getProposal?: (id: string) => Promise<ProposalDetail>;
  decideNode?: (proposalId: string, nodeId: string, decision: Decision) => Promise<void>;
  enactProposal?: (id: string) => Promise<EnactResult>;
}

export function ProposalTree({
  proposalId,
  onClose,
  getProposal = api.getProposal,
  decideNode = api.decideNode,
  enactProposal = api.enactProposal,
}: Props): ReactNode {
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [enacted, setEnacted] = useState<EnactResult | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setDetail(await getProposal(proposalId));
  }, [proposalId, getProposal]);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(nodeId: string, decision: Decision): Promise<void> {
    setBusy(true);
    try {
      await decideNode(proposalId, nodeId, decision);
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function enact(): Promise<void> {
    setBusy(true);
    try {
      setEnacted(await enactProposal(proposalId));
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="proposal-tree" aria-label="Proposal">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to proposals" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">{detail?.title || "Proposal"}</span>
      </div>
      <div className="panel-body">
        {detail === null && <div className="panel-empty">Loading…</div>}
        {detail?.nodes.map((node) => (
          <NodeRow key={node.id} node={node} busy={busy} onDecide={decide} />
        ))}
        {detail !== null && (
          <button type="button" className="start" disabled={busy} onClick={() => void enact()}>
            Enact approved
          </button>
        )}
        {enacted !== null && (
          <div className="r-meta enact-result">
            Enacted {enacted.enacted.length} · held {enacted.held.length}
          </div>
        )}
        <p className="writes-note">
          The agent never writes — enacting runs only the approved, prerequisite-satisfied leaves.
        </p>
      </div>
    </section>
  );
}

function NodeRow({
  node,
  busy,
  onDecide,
}: {
  node: ProposalNode;
  busy: boolean;
  onDecide: (nodeId: string, decision: Decision) => void;
}): ReactNode {
  const body = typeof node.preview.body === "string" ? node.preview.body : "";
  return (
    <div className={`row node-row status-${node.status}`}>
      <div className="r-head">
        <span className="node-status">{node.status}</span> {node.label || node.op || node.type}
      </div>
      {body && <div className="r-sub">{body}</div>}
      {node.type === "leaf" && node.status === "pending" && (
        <div className="node-actions">
          <button
            type="button"
            disabled={busy}
            aria-label={`Approve ${node.label}`}
            onClick={() => onDecide(node.id, "approve")}
          >
            Approve
          </button>
          <button
            type="button"
            disabled={busy}
            aria-label={`Reject ${node.label}`}
            onClick={() => onDecide(node.id, "reject")}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
