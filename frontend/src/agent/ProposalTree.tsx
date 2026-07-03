// The Proposal tree: the owner judges each node by its rendered preview and
// approves or rejects it, then enacts. The agent never writes — enacting runs the
// approved, prerequisite-satisfied leaves through the trusted executor; the rest
// are held (docs/reference/ASSISTANT.md "Staging & approval").

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { EntityTypeIcon } from "../entities/kinds";
import { IntakeLinkProposalEditor } from "../intake/IntakeLinkProposalEditor";
import { Markdown } from "./markdown";
import type { Decision, EnactResult, ProposalDetail, ProposalNode } from "./types";

// A leaf's op turned into a short human descriptor for its row head — so a node
// never has to fall back to the raw op string (e.g. "add_note").
const OP_LABEL: Record<string, string> = {
  add_note: "New note",
  egress_call: "External call",
  merge_entities: "Merge entities",
  add_intake_note: "Captured fact",
};

interface Props {
  proposalId: string;
  onClose: () => void;
  /** Fired after a successful enact so the caller can refresh dependent views
   * (the home stream, which an add_note leaf just wrote into). */
  onEnacted?: (() => void) | undefined;
  getProposal?: (id: string) => Promise<ProposalDetail>;
  decideNode?: (proposalId: string, nodeId: string, decision: Decision) => Promise<void>;
  enactProposal?: (id: string) => Promise<EnactResult>;
}

export function ProposalTree({
  proposalId,
  onClose,
  onEnacted,
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
      onEnacted?.();
    } finally {
      setBusy(false);
    }
  }

  // An intake-link Proposal is editable and mints SHOW-ONCE — it takes a bespoke
  // editor (form + recipient preview + Approve&mint) instead of the generic node list.
  if (detail?.kind === "intake-link") {
    const mintNode = detail.nodes.find((n) => n.op === "mint_intake_link") ?? detail.nodes[0];
    if (mintNode) {
      return (
        <IntakeLinkProposalEditor
          proposalId={detail.id}
          node={mintNode}
          onClose={onClose}
          onMinted={onEnacted}
        />
      );
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
          <NodeRow key={node.id} node={node} title={detail.title} busy={busy} onDecide={decide} />
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
  title,
  busy,
  onDecide,
}: {
  node: ProposalNode;
  title: string;
  busy: boolean;
  onDecide: (nodeId: string, decision: Decision) => void;
}): ReactNode {
  const body = typeof node.preview.body === "string" ? node.preview.body : "";
  const isMerge = node.op === "merge_entities";
  // The panel bar already shows the proposal title; a leaf whose label just repeats
  // it (single-leaf corrections do) shows its op descriptor instead of echoing it.
  const heading =
    node.label && node.label !== title ? node.label : (OP_LABEL[node.op] ?? node.op ?? node.type);
  return (
    <div className={`row node-row status-${node.status}`}>
      <div className="r-head">
        <span className="node-status">{node.status}</span>{" "}
        {isMerge ? <MergeHead preview={node.preview} /> : heading}
      </div>
      {body && (
        <div className="r-sub">
          <Markdown text={body} />
        </div>
      )}
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

// A merge leaf shows the two entities as type-tinted chips (their names, not their
// ids) joined by a combine glyph — so the owner judges a readable effect, never a
// uuid-laden sentence (docs/reference/DESIGN.md "Entity-type accents").
function MergeHead({ preview }: { preview: Record<string, unknown> }): ReactNode {
  const nameA = String(preview.name_a ?? "");
  const nameB = String(preview.name_b ?? "");
  const kindA = String(preview.kind_a ?? "Thing");
  const kindB = String(preview.kind_b ?? "Thing");
  return (
    <span className="merge-chips">
      <span className="merge-chip">
        <EntityTypeIcon kind={kindA} size={20} /> {nameA}
      </span>
      <span className="merge-arrow" aria-label="merge into one">
        ＋
      </span>
      <span className="merge-chip">
        <EntityTypeIcon kind={kindB} size={20} /> {nameB}
      </span>
    </span>
  );
}
