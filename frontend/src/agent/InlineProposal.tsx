// The inline approval card (docs/mocks/inline-approvals/d-one-tree.html): one staged
// Proposal, acted on IN the conversation. Each leaf is approved, declined with a reason,
// or corrected in place; one double-tap Enact runs the approved, prerequisite-satisfied
// leaves and sends a single server-authored outcome back to the assistant so it follows
// up (docs/reference/ASSISTANT.md "Acting on a Proposal inline"). The side panel remains
// for browsing older / cross-session proposals.

import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { MergeHead } from "./ProposalTree";
import type { Decision, EnactResult, ProposalDetail, ProposalNode } from "./types";

// The kinds acted on inline. wiki-restructure (a large multi-op tree) and intake-link
// (a bespoke mint editor) stay on the Proposals panel for now — see the plan §1.
export const INLINE_KINDS = new Set([
  "correction",
  "knowledge",
  "appointment",
  "merge",
  "egress",
  // A one-leaf library-video removal jerv staged; the owner approves it here and the executor
  // hard-deletes. Renders as a plain labelled leaf ("Remove … from your library").
  "remove-library-video",
]);

// A leaf whose proposed text the owner may correct in place (the executor reads
// preview.body at enact) — the same ops the backend's patch_node_body accepts.
const EDITABLE_OPS = new Set(["add_note", "manage_appointment"]);

interface Props {
  proposalId: string;
  /** Send the server-authored enact outcome back to the assistant as a data-framed
   * follow-up turn (fb.send(text, { proposalOutcome: true })). Resolves TRUE when the
   * turn actually started, FALSE when it was dropped (e.g. another turn is streaming) —
   * so the card never claims "sent" when it wasn't. */
  onOutcome: (outcome: string) => Promise<boolean>;
  /** Refresh dependent views (the home stream an add_note leaf just wrote into). */
  onEnacted?: (() => void) | undefined;
  /** A turn is already streaming in this chat — Enact is disabled so the outcome
   * follow-up turn isn't dropped by the single-in-flight-turn guard. */
  chatBusy?: boolean | undefined;
  getProposal?: (id: string) => Promise<ProposalDetail>;
  decideNode?: (id: string, nodeId: string, decision: Decision, reason?: string) => Promise<void>;
  editNode?: (id: string, nodeId: string, body: string) => Promise<void>;
  enactProposal?: (id: string) => Promise<EnactResult>;
}

type LeafState = "in" | "out";

export function InlineProposal({
  proposalId,
  onOutcome,
  onEnacted,
  chatBusy = false,
  getProposal = api.getProposal,
  decideNode = api.decideNode,
  editNode = api.editNode,
  enactProposal = api.enactProposal,
}: Props): ReactNode {
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [state, setState] = useState<Record<string, LeafState>>({});
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resolved, setResolved] = useState<{ result: EnactResult; sent: boolean } | null>(null);
  const armTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Set synchronously the instant an enact starts, so a rapid third tap can't slip
  // through the pre-flush window and fire the decide→enact→outcome sequence twice.
  const enacting = useRef(false);
  // The edit session that owns the current commit — guards the input's onBlur from
  // re-committing after Enter/Escape already settled it.
  const editingRef = useRef<string | null>(null);

  useEffect(() => {
    let live = true;
    void getProposal(proposalId).then((d) => {
      if (!live) return;
      setDetail(d);
      // Default every leaf to approved — the mock's starting point; the owner declines
      // or corrects the exceptions, then enacts once.
      const init: Record<string, LeafState> = {};
      for (const n of d.nodes) if (n.type === "leaf") init[n.id] = "in";
      setState(init);
    });
    return () => {
      live = false;
    };
  }, [proposalId, getProposal]);

  // Clear the arm timer if the card unmounts mid-arm.
  useEffect(() => () => clearTimeout(armTimer.current), []);

  const nodes = detail?.nodes;
  const leaves = useMemo(() => (nodes ?? []).filter((n) => n.type === "leaf"), [nodes]);
  const byId = useMemo(() => new Map((nodes ?? []).map((n) => [n.id, n])), [nodes]);

  // A leaf is held when it is approved but a prerequisite is declined (or itself held) —
  // presentation only; the server re-derives it authoritatively at enact.
  const held = useCallback(
    (id: string): boolean => {
      const seen = new Set<string>();
      const walk = (nid: string): boolean => {
        if (seen.has(nid)) return false;
        seen.add(nid);
        const node = byId.get(nid);
        if (!node || state[nid] !== "in") return false;
        return node.deps.some((d) => state[d] === "out" || walk(d));
      };
      return walk(id);
    },
    [byId, state],
  );

  const readyCount = leaves.filter((n) => state[n.id] === "in" && !held(n.id)).length;

  function disarm(): void {
    setArmed(false);
    if (armTimer.current) clearTimeout(armTimer.current);
  }

  function toggle(id: string, next: LeafState): void {
    disarm();
    setState((s) => ({ ...s, [id]: next }));
  }

  function startEdit(id: string): void {
    editingRef.current = id;
    setEditing(id);
  }

  function commitEdit(id: string, value: string): void {
    if (editingRef.current !== id) return; // already settled by Enter/Escape
    editingRef.current = null;
    setEditing(null);
    const node = byId.get(id);
    const original = typeof node?.preview.body === "string" ? node.preview.body : "";
    const next = value.trim();
    setEdits((e) => {
      const rest = { ...e };
      // An edit back to the original clears the correction — the owner reverted it, so
      // enact must NOT still file the intermediate value (review finding #2).
      if (next && next !== original.trim()) rest[id] = next;
      else delete rest[id];
      return rest;
    });
    if (next && next !== original.trim()) setState((s) => ({ ...s, [id]: "in" }));
  }

  async function enact(): Promise<void> {
    if (busy || chatBusy || readyCount === 0 || enacting.current) return;
    if (!armed) {
      setArmed(true);
      if (armTimer.current) clearTimeout(armTimer.current);
      armTimer.current = setTimeout(() => setArmed(false), 3000);
      return;
    }
    enacting.current = true;
    disarm();
    setBusy(true);
    try {
      // Per leaf: an edit lands before its approve (both while the proposal is still
      // 'staged', so the executor reads the corrected preview.body at enact).
      for (const n of leaves) {
        if (state[n.id] === "out") {
          await decideNode(proposalId, n.id, "reject", reasons[n.id]?.trim() || undefined);
        } else {
          const edit = edits[n.id];
          if (edit !== undefined) await editNode(proposalId, n.id, edit);
          await decideNode(proposalId, n.id, "approve");
        }
      }
      const result = await enactProposal(proposalId);
      // Only claim "sent" if the follow-up turn actually started (it can be dropped if a
      // turn is mid-stream) — the card must not assert an outcome it didn't deliver.
      const sent = result.outcome ? await onOutcome(result.outcome) : true;
      setResolved({ result, sent });
      onEnacted?.();
    } finally {
      setBusy(false);
      enacting.current = false;
    }
  }

  if (detail === null) {
    return <output className="fb-inline-prop loading">Loading proposal…</output>;
  }

  if (resolved !== null) {
    return (
      <output className="fb-inline-prop done">
        <span className="ip-check" aria-hidden="true">
          ✓
        </span>
        <span>
          Enacted {resolved.result.enacted.length} operation
          {resolved.result.enacted.length === 1 ? "" : "s"}
          {resolved.result.held.length > 0 && ` · ${resolved.result.held.length} held`}
          <span className="ip-sub">
            {resolved.sent
              ? "one message sent to the assistant"
              : "the assistant will hear this after the current reply"}
          </span>
        </span>
      </output>
    );
  }

  const topLevel = detail.nodes.filter((n) => n.parent_id === null);
  const declinedCount = leaves.filter((n) => state[n.id] === "out").length;
  const heldCount = leaves.filter((n) => state[n.id] === "in" && held(n.id)).length;
  const handlers: LeafHandlers = {
    onToggle: toggle,
    onStartEdit: startEdit,
    onCommitEdit: commitEdit,
    onReason: (id, v) => setReasons((r) => ({ ...r, [id]: v })),
  };
  const maps = { state, edits, reasons, editing, held, busy };

  return (
    <section className="fb-inline-prop" aria-label={`Proposal: ${detail.title}`}>
      <div className="ip-head">
        <span className="ip-title">{detail.title}</span>
        <span className={`ip-domain pill ${detail.domain}`}>{detail.domain}</span>
      </div>
      <div className="ip-tree">
        {topLevel.map((n) =>
          n.type === "group" ? (
            <Group key={n.id} group={n} nodes={detail.nodes} {...maps} {...handlers} />
          ) : (
            <Leaf
              key={n.id}
              node={n}
              state={state[n.id] ?? "in"}
              edited={edits[n.id]}
              reason={reasons[n.id] ?? ""}
              editing={editing === n.id}
              held={held(n.id)}
              busy={busy}
              {...handlers}
            />
          ),
        )}
      </div>
      <div className="ip-foot">
        <span className="ip-tally" aria-live="polite">
          <b>{readyCount}</b> of {leaves.length} ready
          {declinedCount > 0 && <span className="ip-no"> · {declinedCount} declined</span>}
          {heldCount > 0 && <span className="ip-held"> · {heldCount} held</span>}
        </span>
        <button
          type="button"
          className={`ip-enact${armed ? " armed" : ""}`}
          disabled={busy || chatBusy || readyCount === 0}
          title={chatBusy ? "Wait for the current reply to finish" : undefined}
          onClick={() => void enact()}
        >
          {armed ? `Tap to enact ${readyCount}` : `Enact${readyCount ? ` ${readyCount}` : ""}`}
        </button>
      </div>
    </section>
  );
}

// The per-leaf callbacks, shared by the flat and grouped render paths.
interface LeafHandlers {
  onToggle: (id: string, next: LeafState) => void;
  onStartEdit: (id: string) => void;
  onCommitEdit: (id: string, value: string) => void;
  onReason: (id: string, value: string) => void;
}

// The state maps a group needs to derive each child leaf's scalar props.
interface LeafMaps {
  state: Record<string, LeafState>;
  edits: Record<string, string>;
  reasons: Record<string, string>;
  editing: string | null;
  held: (id: string) => boolean;
  busy: boolean;
}

function Group({
  group,
  nodes,
  state,
  edits,
  reasons,
  editing,
  held,
  busy,
  ...handlers
}: { group: ProposalNode; nodes: ProposalNode[] } & LeafMaps & LeafHandlers): ReactNode {
  const children = nodes.filter((n) => n.parent_id === group.id);
  return (
    <div className="ip-group">
      <div className="ip-group-head">{group.label}</div>
      <div className="ip-group-body">
        {children.map((n) => (
          <Leaf
            key={n.id}
            node={n}
            state={state[n.id] ?? "in"}
            edited={edits[n.id]}
            reason={reasons[n.id] ?? ""}
            editing={editing === n.id}
            held={held(n.id)}
            busy={busy}
            {...handlers}
          />
        ))}
      </div>
    </div>
  );
}

function Leaf({
  node,
  state,
  edited,
  reason,
  editing,
  held,
  busy,
  onToggle,
  onStartEdit,
  onCommitEdit,
  onReason,
}: {
  node: ProposalNode;
  state: LeafState;
  edited: string | undefined;
  reason: string;
  editing: boolean;
  held: boolean;
  busy: boolean;
} & LeafHandlers): ReactNode {
  const body = typeof node.preview.body === "string" ? node.preview.body : "";
  const value = edited ?? body;
  const editable = EDITABLE_OPS.has(node.op) && body !== "";
  const isMerge = node.op === "merge_entities";
  const out = state === "out";
  const corrected = edited !== undefined;
  const cls = `ip-leaf${out ? " out" : ""}${corrected ? " corrected" : ""}${held ? " held" : ""}`;
  return (
    <div className={cls}>
      <div className="ip-leaf-main">
        <div className="ip-leaf-label">
          {/* A merge reads as its two entity chips (never a uuid sentence), same as the
              panel; every other leaf shows its label. */}
          {isMerge ? <MergeHead preview={node.preview} /> : node.label || node.op}
          {corrected && <span className="ip-edited"> · edited</span>}
        </div>
        {value && (
          <div className="ip-value">
            {editing ? (
              <input
                className="ip-edit-input"
                aria-label={`Correct ${node.label}`}
                defaultValue={value}
                // biome-ignore lint/a11y/noAutofocus: focus follows the owner's tap-to-edit
                autoFocus
                onBlur={(e) => onCommitEdit(node.id, e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onCommitEdit(node.id, e.currentTarget.value);
                  if (e.key === "Escape") onCommitEdit(node.id, value);
                }}
              />
            ) : (
              <button
                type="button"
                className="ip-value-text"
                disabled={busy || !editable}
                aria-label={editable ? `Correct ${node.label}` : undefined}
                onClick={() => onStartEdit(node.id)}
                title={editable ? "Tap to correct" : undefined}
              >
                {value}
                {editable && (
                  <span className="ip-pen" aria-hidden="true">
                    ✎
                  </span>
                )}
              </button>
            )}
          </div>
        )}
        {held && <span className="ip-held-badge">held — a prerequisite is declined</span>}
      </div>
      <div className="ip-ctl">
        <button
          type="button"
          className={`ip-ok${!out ? " on" : ""}${corrected ? " corrected" : ""}`}
          aria-label={`Approve ${node.label}`}
          aria-pressed={!out}
          disabled={busy}
          onClick={() => onToggle(node.id, "in")}
        >
          ✓
        </button>
        <button
          type="button"
          className={`ip-no${out ? " on" : ""}`}
          aria-label={`Decline ${node.label}`}
          aria-pressed={out}
          disabled={busy}
          onClick={() => onToggle(node.id, out ? "in" : "out")}
        >
          ✕
        </button>
      </div>
      {out && (
        <div className="ip-reason">
          <span className="ip-reason-label">Why decline? The assistant learns from this.</span>
          <input
            className="ip-reason-input"
            aria-label={`Reason for declining ${node.label}`}
            placeholder="Add a reason (optional)…"
            value={reason}
            disabled={busy}
            onChange={(e) => onReason(node.id, e.target.value)}
          />
        </div>
      )}
    </div>
  );
}
