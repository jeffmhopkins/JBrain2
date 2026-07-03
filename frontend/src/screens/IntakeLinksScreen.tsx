// Intake Links — owner management (W6, docs/mocks/guided-intake/manage-b-grouped.html).
// The owner's minted guided-intake links, grouped Needs review → Active → Closed. A link
// opens a detail (stats + its conversations) and a conversation opens a READ-ONLY intake
// transcript, kept deliberately separate from the owner's own chats (a stranger authored
// it; the owner can read it but never reply or resume). New links are drafted by the
// assistant and approved as a Proposal — this screen never mints from scratch; it re-mints
// (clone an existing link's config to a fresh show-once secret) and revokes.

import { useCallback, useEffect, useState } from "react";
import { Markdown } from "../agent/markdown";
import type { Decision, EnactResult, ProposalDetail } from "../agent/types";
import { api } from "../api/client";
import { intakeShareUrl } from "../intake/share";
import type {
  IntakeLink,
  IntakeMintRequest,
  IntakeMintResult,
  IntakeSessionRow,
  IntakeSubmission,
  IntakeSubmissionDetail,
} from "../intake/types";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import "../intake/owner.css";

export interface IntakeLinksDeps {
  listLinks: () => Promise<IntakeLink[]>;
  listSubmissions: (linkId: string) => Promise<IntakeSubmission[]>;
  listSessions: (linkId: string) => Promise<IntakeSessionRow[]>;
  getSubmission: (id: string) => Promise<IntakeSubmissionDetail>;
  materialize: (id: string) => Promise<{ proposal_id: string }>;
  revokeLink: (id: string) => Promise<void>;
  mintLink: (body: IntakeMintRequest) => Promise<IntakeMintResult>;
  // Inline approval — the submission's Proposal is decided and enacted right here in
  // the intake screen (still the owner-review trust gate under the hood), so the owner
  // never has to hop to the separate Proposals panel to add a captured note.
  getProposal: (id: string) => Promise<ProposalDetail>;
  decideNode: (proposalId: string, nodeId: string, decision: Decision) => Promise<void>;
  enactProposal: (id: string) => Promise<EnactResult>;
}

interface Props {
  deps?: IntakeLinksDeps;
}

type View =
  | { kind: "list" }
  | { kind: "detail"; linkId: string }
  | { kind: "convo"; linkId: string; submissionId: string }
  | { kind: "abandoned"; linkId: string };

const HOUR = 3600 * 1000;

// A link's lifecycle bucket from its server status. 'active' shows in the Active group;
// everything terminal (revoked / exhausted / expired) is Closed.
function isActive(l: IntakeLink): boolean {
  return l.status === "active";
}

function relTime(iso: string, now: number): string {
  const ms = new Date(iso).getTime() - now;
  const abs = Math.abs(ms);
  const past = ms < 0;
  let s: string;
  if (abs < HOUR) s = `${Math.max(1, Math.round(abs / 60000))}m`;
  else if (abs < 24 * HOUR) s = `${Math.round(abs / HOUR)}h`;
  else s = `${Math.round(abs / (24 * HOUR))}d`;
  return past ? `${s} ago` : `in ${s}`;
}

function statusBadge(l: IntakeLink): { cls: string; text: string } {
  if (l.status === "active") return { cls: "green", text: "Active" };
  if (l.status === "exhausted") return { cls: "amber", text: "Exhausted" };
  if (l.status === "revoked") return { cls: "rose", text: "Revoked" };
  return { cls: "steel", text: l.status };
}

export function IntakeLinksScreen({ deps }: Props) {
  const d: IntakeLinksDeps = {
    listLinks: deps?.listLinks ?? api.listIntakeLinks,
    listSubmissions: deps?.listSubmissions ?? api.listIntakeSubmissions,
    listSessions: deps?.listSessions ?? api.listIntakeSessions,
    getSubmission: deps?.getSubmission ?? api.getIntakeSubmission,
    materialize: deps?.materialize ?? api.materializeIntakeSubmission,
    revokeLink: deps?.revokeLink ?? api.revokeIntakeLink,
    mintLink: deps?.mintLink ?? api.mintIntakeLink,
    getProposal: deps?.getProposal ?? api.getProposal,
    decideNode: deps?.decideNode ?? api.decideNode,
    enactProposal: deps?.enactProposal ?? api.enactProposal,
  };

  const [links, setLinks] = useState<IntakeLink[] | null>(null);
  const [awaiting, setAwaiting] = useState<Record<string, number>>({});
  const [error, setError] = useState(false);
  const [view, setView] = useState<View>({ kind: "list" });
  const [now, setNow] = useState(0);
  // The show-once secret surfaced by a re-mint — the only time it's recoverable.
  const [minted, setMinted] = useState<IntakeMintResult | null>(null);
  const [showNew, setShowNew] = useState(false);

  // `d` is rebuilt each render from stable api refs / props; reload is invoked only
  // explicitly (mount, after revoke / re-mint), so it intentionally omits `d`.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see above.
  const reload = useCallback(async () => {
    setNow(Date.now());
    try {
      const list = await d.listLinks();
      setLinks(list);
      // Per-link awaiting-review counts drive the "Needs review" group. A personal
      // box has few links, so the per-link fan-out is cheap; a failed lane just
      // reads as zero-awaiting (the link still shows under its status group).
      const counts = await Promise.all(
        list.map(async (l) => {
          try {
            const subs = await d.listSubmissions(l.id);
            return [l.id, subs.filter((s) => s.status === "submitted").length] as const;
          } catch {
            return [l.id, 0] as const;
          }
        }),
      );
      setAwaiting(Object.fromEntries(counts));
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // If the open detail's link drops out of the list (revoked + reloaded, or a
  // re-mint replaced it), fall back to the list — in an effect, never mid-render.
  useEffect(() => {
    if (view.kind === "detail" && links !== null && !links.some((l) => l.id === view.linkId)) {
      setView({ kind: "list" });
    }
  }, [view, links]);

  if (view.kind === "detail") {
    const link = links?.find((l) => l.id === view.linkId);
    if (!link) return null;
    return (
      <LinkDetail
        link={link}
        now={now}
        deps={d}
        onBack={() => setView({ kind: "list" })}
        onOpenConvo={(submissionId) => setView({ kind: "convo", linkId: link.id, submissionId })}
        onOpenAbandoned={() => setView({ kind: "abandoned", linkId: link.id })}
        onRevoked={() => {
          void reload();
          setView({ kind: "list" });
        }}
        onReminted={(m) => {
          // The old link is now revoked; surface the fresh secret on the list, where
          // the show-once reveal lives.
          setMinted(m);
          setView({ kind: "list" });
          void reload();
        }}
      />
    );
  }

  if (view.kind === "convo") {
    return (
      <ConversationView
        submissionId={view.submissionId}
        deps={d}
        onBack={() => setView({ kind: "detail", linkId: view.linkId })}
        onMaterialized={() => void reload()}
      />
    );
  }

  if (view.kind === "abandoned") {
    return (
      <main className="screen-body intake-mgmt">
        <ConvoBar
          title="(in progress)"
          onBack={() => setView({ kind: "detail", linkId: view.linkId })}
        />
        <div className="intake-convo-note">
          <span aria-hidden="true">🛡️</span>
          <span>
            This visitor opened the link and started, but never confirmed a draft — so nothing was
            submitted and nothing entered your brain. The partial conversation isn't kept.
          </span>
        </div>
      </main>
    );
  }

  const active = (links ?? []).filter(isActive);
  const closed = (links ?? []).filter((l) => !isActive(l));
  const needsReview = (links ?? []).filter((l) => (awaiting[l.id] ?? 0) > 0);

  return (
    <main className="screen-body intake-mgmt">
      {minted && <MintReveal minted={minted} onClose={() => setMinted(null)} />}

      <button type="button" className="intake-newbtn" onClick={() => setShowNew((v) => !v)}>
        <span className="intake-plus" aria-hidden="true">
          ＋
        </span>
        <span className="intake-newtxt">
          <span className="nt">New intake link</span>
          <span className="ns">Drafted with the assistant · approved in Proposals</span>
        </span>
      </button>
      {showNew && (
        <p className="intake-new-explain">
          New links are drafted by the assistant. In Full Brain, ask it to “make an intake link” for
          what you want to collect — a staged Proposal opens here and in your Proposals panel, where
          you edit and approve it to mint the link.
        </p>
      )}

      {error && <p className="analysis-quiet">couldn't load intake links — reopen to retry.</p>}
      {links === null && !error && <p className="analysis-quiet">loading intake links…</p>}
      {links !== null && links.length === 0 && (
        <p className="analysis-quiet">no intake links yet — ask the assistant to draft one.</p>
      )}

      {needsReview.length > 0 && (
        <>
          <Section label="Needs review" count={needsReview.length} accent />
          {needsReview.map((l) => (
            <button
              type="button"
              key={`r-${l.id}`}
              className="intake-row flagged"
              onClick={() => setView({ kind: "detail", linkId: l.id })}
            >
              <Disc domain={l.domain_code} />
              <span className="intake-row-grow">
                <span className="intake-row-label">{l.label || "Intake link"}</span>
                <span className="intake-row-meta">
                  {awaiting[l.id]} submission{awaiting[l.id] === 1 ? "" : "s"} awaiting review
                </span>
              </span>
              <span className="intake-reviewpill">Review →</span>
            </button>
          ))}
        </>
      )}

      {active.length > 0 && (
        <>
          <Section label="Active" count={active.length} />
          {active.map((l) => (
            <LinkRow
              key={l.id}
              link={l}
              now={now}
              onOpen={() => setView({ kind: "detail", linkId: l.id })}
            />
          ))}
        </>
      )}

      {closed.length > 0 && (
        <>
          <Section label="Closed" count={closed.length} />
          {closed.map((l) => (
            <LinkRow
              key={l.id}
              link={l}
              now={now}
              onOpen={() => setView({ kind: "detail", linkId: l.id })}
            />
          ))}
        </>
      )}
    </main>
  );
}

function Section({ label, count, accent }: { label: string; count: number; accent?: boolean }) {
  return (
    <div className={`intake-sec${accent ? " accent" : ""}`}>
      <span className="intake-sec-h">{label}</span>
      <span className="intake-sec-ct">{count}</span>
    </div>
  );
}

function Disc({ domain }: { domain: string }) {
  return (
    <span
      className="intake-disc"
      style={{
        background: `${DOMAIN_COLOR[domain] ?? "var(--steel)"}22`,
        color: DOMAIN_COLOR[domain] ?? "var(--steel)",
      }}
      aria-hidden="true"
    >
      ◆
    </span>
  );
}

function LinkRow({ link, now, onOpen }: { link: IntakeLink; now: number; onOpen: () => void }) {
  const badge = statusBadge(link);
  const expired = new Date(link.expires_at).getTime() <= now;
  const expiry =
    link.status !== "active"
      ? ""
      : expired
        ? " · expired"
        : ` · ${relTime(link.expires_at, now).replace("in ", "")} left`;
  return (
    <button type="button" className="intake-row" onClick={onOpen}>
      <Disc domain={link.domain_code} />
      <span className="intake-row-grow">
        <span className="intake-row-label">{link.label || "Intake link"}</span>
        <span className="intake-row-meta">
          <span className={`intake-badge ${badge.cls}`}>{badge.text}</span> · {link.runs_used}/
          {link.max_runs} submitted · {link.opens_used}/{link.max_opens} opened
          {expiry}
        </span>
      </span>
      <span className="intake-chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}

// ===== Link detail =====

function LinkDetail({
  link,
  now,
  deps,
  onBack,
  onOpenConvo,
  onOpenAbandoned,
  onRevoked,
  onReminted,
}: {
  link: IntakeLink;
  now: number;
  deps: IntakeLinksDeps;
  onBack: () => void;
  onOpenConvo: (submissionId: string) => void;
  onOpenAbandoned: () => void;
  onRevoked: () => void;
  onReminted: (m: IntakeMintResult) => void;
}) {
  const [sessions, setSessions] = useState<IntakeSessionRow[] | null>(null);
  const [submissions, setSubmissions] = useState<IntakeSubmission[] | null>(null);
  const [armRevoke, setArmRevoke] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let stale = false;
    Promise.all([deps.listSessions(link.id), deps.listSubmissions(link.id)])
      .then(([ss, subs]) => {
        if (!stale) {
          setSessions(ss);
          setSubmissions(subs);
        }
      })
      .catch(() => {
        if (!stale) setError("Couldn't load this link's conversations.");
      });
    return () => {
      stale = true;
    };
  }, [deps, link.id]);

  const badge = statusBadge(link);
  const subBySession = new Map((submissions ?? []).map((s) => [s.session_id, s]));

  async function revoke(): Promise<void> {
    if (!armRevoke) {
      setArmRevoke(true);
      return;
    }
    setBusy(true);
    try {
      await deps.revokeLink(link.id);
      onRevoked();
    } catch {
      setError("Couldn't revoke the link — try again.");
      setBusy(false);
    }
  }

  async function remint(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      // Re-grant the link's original TTL window, clamped to the backend's bounds
      // (0.25h..720h) so float drift on a 30-day link can't trip the 422.
      const ttlHours = Math.min(
        720,
        Math.max(
          0.25,
          (new Date(link.expires_at).getTime() - new Date(link.created_at).getTime()) / HOUR,
        ),
      );
      const body: IntakeMintRequest = {
        subject_id: link.subject_id,
        domain_code: link.domain_code,
        fields_brief: link.fields_brief || " ",
        persona_brief: link.persona_brief,
        opening_blurb: link.opening_blurb,
        label: link.label,
        max_runs: link.max_runs,
        max_opens: link.max_opens,
        bind_on_first: link.bind_on_first,
        capture_enterer_name: link.capture_enterer_name,
        disclose_owner_identity: link.disclose_owner_identity,
        ttl_hours: ttlHours,
      };
      const m = await deps.mintLink(body);
      // The old link stops working once a fresh one is out (matches the mock). Mint
      // first so a mint failure never leaves the owner with no working link.
      await deps.revokeLink(link.id).catch(() => {});
      onReminted(m);
    } catch {
      setError("Couldn't re-mint the link — try again.");
    } finally {
      setBusy(false);
    }
  }

  // Conversation rows: every opened session, joined to its submission (if confirmed).
  const rows = (sessions ?? []).map((s) => {
    const sub = subBySession.get(s.id);
    if (sub) {
      const awaiting = sub.status === "submitted";
      return {
        key: s.id,
        name: sub.enterer_name || "Anonymous",
        badge: awaiting
          ? { cls: "steel", text: "Awaiting review" }
          : { cls: "green", text: "In review" },
        onOpen: () => onOpenConvo(sub.id),
      };
    }
    if (s.status === "abandoned") {
      return {
        key: s.id,
        name: "(in progress)",
        badge: { cls: "amber", text: "Abandoned" },
        onOpen: onOpenAbandoned,
      };
    }
    return {
      key: s.id,
      name: "(in progress)",
      badge: { cls: "steel", text: "Open" },
      onOpen: null,
    };
  });

  return (
    <main className="screen-body intake-mgmt">
      <ConvoBar title={link.label || "Intake link"} onBack={onBack} />

      <div className="intake-dcard">
        <KV k="Status" v={<span className={`intake-badge ${badge.cls}`}>{badge.text}</span>} />
        <KV
          k="About"
          v={`${link.subject_id ?? "No specific person"} · ${DOMAIN_TITLE[link.domain_code] ?? link.domain_code}`}
        />
        <KV k="Binding" v={link.bind_on_first ? "One person (bound)" : "Open / many people"} />
        <KV k="Expires" v={link.status === "active" ? relTime(link.expires_at, now) : "—"} />
        <div className="intake-stats">
          <Stat n={`${link.runs_used}/${link.max_runs}`} c="submitted" />
          <Stat n={`${link.opens_used}/${link.max_opens}`} c="opened" />
        </div>
      </div>

      <div className="intake-sech">Conversations</div>
      {error && <p className="intake-prop-error">{error}</p>}
      {sessions === null && !error && <p className="analysis-quiet">loading conversations…</p>}
      {sessions !== null && rows.length === 0 && (
        <p className="analysis-quiet">no one has opened this link yet.</p>
      )}
      {rows.map((r) => (
        <button
          type="button"
          key={r.key}
          className="intake-subrow"
          disabled={r.onOpen === null}
          onClick={() => r.onOpen?.()}
        >
          <span className="intake-subav" aria-hidden="true">
            {initials(r.name)}
          </span>
          <span className="intake-row-grow">
            <span className="intake-row-label">{r.name}</span>
            <span className="intake-row-meta">
              <span className={`intake-badge ${r.badge.cls}`}>{r.badge.text}</span>
            </span>
          </span>
          {r.onOpen !== null && (
            <span className="intake-chev" aria-hidden="true">
              ›
            </span>
          )}
        </button>
      ))}

      <div className="intake-detail-actions">
        {link.status === "active" && (
          <button
            type="button"
            className="intake-btn steel"
            disabled={busy}
            onClick={() => void remint()}
          >
            Re-mint &amp; copy link
          </button>
        )}
        {link.status === "active" && (
          <button
            type="button"
            className={`intake-btn danger${armRevoke ? " armed" : ""}`}
            disabled={busy}
            onClick={() => void revoke()}
          >
            {armRevoke ? "Tap again — revoke this link" : "Revoke link"}
          </button>
        )}
      </div>
    </main>
  );
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="intake-kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

function Stat({ n, c }: { n: string; c: string }) {
  return (
    <div className="intake-stat">
      <span className="n">{n}</span>
      <span className="c">{c}</span>
    </div>
  );
}

// ===== Read-only conversation view =====

function ConversationView({
  submissionId,
  deps,
  onBack,
  onMaterialized,
}: {
  submissionId: string;
  deps: IntakeLinksDeps;
  onBack: () => void;
  onMaterialized: () => void;
}) {
  const [detail, setDetail] = useState<IntakeSubmissionDetail | null>(null);
  const [proposal, setProposal] = useState<ProposalDetail | null>(null);
  const [outcome, setOutcome] = useState<"added" | "rejected" | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);

  // `deps` is rebuilt each parent render (stable api refs behind it), and a
  // materialize/approve triggers a parent reload — so these fetch effects key on the
  // ids alone, never on `deps`, or the reload would re-run them and clobber the
  // optimistic status we just set (matches the parent `reload`'s dep discipline).
  // biome-ignore lint/correctness/useExhaustiveDependencies: see above.
  useEffect(() => {
    let stale = false;
    deps
      .getSubmission(submissionId)
      .then((s) => {
        if (!stale) setDetail(s);
      })
      .catch(() => {
        if (!stale) setError(true);
      });
    return () => {
      stale = true;
    };
  }, [submissionId]);

  // Once the submission has a Proposal, load it so the owner can review the single
  // note it became and approve it right here (no Proposals-panel hop).
  const proposalId = detail?.proposal_id ?? null;
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on proposalId only — see above.
  useEffect(() => {
    if (!proposalId) return;
    let stale = false;
    deps
      .getProposal(proposalId)
      .then((p) => {
        if (!stale) setProposal(p);
      })
      .catch(() => {
        if (!stale) setError(true);
      });
    return () => {
      stale = true;
    };
  }, [proposalId]);

  // Turn the captured submission into its staged note (owner step — #10). The button
  // then swaps for the note preview + approve, so the whole flow stays on this screen.
  async function prepare(): Promise<void> {
    setBusy(true);
    try {
      const { proposal_id } = await deps.materialize(submissionId);
      setDetail((s) => (s ? { ...s, status: "proposed", proposal_id } : s));
      onMaterialized();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  async function approve(): Promise<void> {
    const node = proposal?.nodes[0];
    if (!proposal || !node) return;
    setBusy(true);
    try {
      await deps.decideNode(proposal.id, node.id, "approve");
      await deps.enactProposal(proposal.id);
      setOutcome("added");
      onMaterialized();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  async function reject(): Promise<void> {
    const node = proposal?.nodes[0];
    if (!proposal || !node) return;
    setBusy(true);
    try {
      await deps.decideNode(proposal.id, node.id, "reject");
      setOutcome("rejected");
      onMaterialized();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  const name = detail?.enterer_name || "Anonymous";
  const summary = typeof detail?.draft?.summary === "string" ? detail.draft.summary : "";
  const noteNode = proposal?.nodes[0] ?? null;
  const noteBody = typeof noteNode?.preview.body === "string" ? noteNode.preview.body : "";
  // Reflect the just-made decision, or the persisted state on a revisit (a note was
  // created, or the leaf already enacted/rejected in a prior visit).
  const added =
    outcome === "added" || (detail?.note_ids.length ?? 0) > 0 || noteNode?.status === "enacted";
  const rejected = outcome === "rejected" || noteNode?.status === "rejected";

  return (
    <main className="screen-body intake-mgmt">
      <ConvoBar title={name} onBack={onBack} tag="intake · read-only" />

      <div className="intake-convo-note">
        <span aria-hidden="true">🛡️</span>
        <span>
          An intake conversation — kept separate from your own chats. You can read the full history,
          but not reply or resume it.
        </span>
      </div>

      {error && <p className="intake-prop-error">Couldn't load this conversation.</p>}
      {detail === null && !error && <p className="analysis-quiet">loading conversation…</p>}

      {detail && (
        <>
          {/* Before materializing: a preview of what the recipient confirmed. */}
          {detail.status === "submitted" && summary && (
            <div className="intake-convosum">
              <div className="ttl">Draft shown to {name}</div>
              <div className="body">
                <Markdown text={summary} />
              </div>
            </div>
          )}

          {detail.status === "submitted" && (
            <button
              type="button"
              className="intake-btn steel"
              disabled={busy}
              onClick={() => void prepare()}
            >
              Review as a note →
            </button>
          )}

          {/* After materializing: the single note it became, approvable inline. */}
          {detail.status === "proposed" &&
            (added ? (
              <p className="intake-inbox-note">✓ Added to your notes.</p>
            ) : rejected ? (
              <p className="intake-inbox-note">Rejected — nothing was kept.</p>
            ) : noteNode ? (
              <>
                <div className="intake-convosum">
                  <div className="ttl">
                    Note to add{proposal?.title ? ` · ${proposal.title}` : ""}
                  </div>
                  <div className="body">
                    <Markdown text={noteBody} />
                  </div>
                </div>
                <p className="intake-inbox-note">
                  Review the note above — approve to add it to your notes, or reject to keep
                  nothing.
                </p>
                <div className="intake-detail-actions">
                  <button
                    type="button"
                    className="intake-btn steel"
                    disabled={busy}
                    onClick={() => void approve()}
                  >
                    Approve &amp; add to notes
                  </button>
                  <button
                    type="button"
                    className="intake-btn"
                    disabled={busy}
                    onClick={() => void reject()}
                  >
                    Reject
                  </button>
                </div>
              </>
            ) : proposal ? (
              <p className="intake-inbox-note">Nothing usable to keep from this submission.</p>
            ) : (
              <p className="analysis-quiet">loading the note…</p>
            ))}

          <div className="intake-sech">Full conversation</div>
          <div className="intake-transcript">
            {(detail.transcript ?? []).map((t, i) => {
              const you = t.role === "recipient";
              return (
                // biome-ignore lint/suspicious/noArrayIndexKey: a frozen, read-only transcript — index is a stable id.
                <div key={i} className={`intake-tmsg${you ? " you" : ""}`}>
                  <div className="intake-twho">{you ? name : "Guide"}</div>
                  <div className="intake-tbub">
                    <Markdown text={t.text ?? ""} />
                  </div>
                </div>
              );
            })}
            {(detail.transcript ?? []).length === 0 && (
              <p className="analysis-quiet">no transcript recorded.</p>
            )}
          </div>
        </>
      )}
    </main>
  );
}

// ===== Shared bits =====

function ConvoBar({ title, onBack, tag }: { title: string; onBack: () => void; tag?: string }) {
  return (
    <div className="intake-convobar">
      <button type="button" className="intake-back" aria-label="Back" onClick={onBack}>
        ‹
      </button>
      <span className="intake-convottl">{title}</span>
      {tag && <span className="intake-convotag">{tag}</span>}
    </div>
  );
}

function MintReveal({ minted, onClose }: { minted: IntakeMintResult; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const url = intakeShareUrl(minted.secret);
  return (
    <section className="intake-reveal" aria-label="Re-minted link">
      <div className="intake-reveal-h">Re-minted — copy the fresh link</div>
      <p className="intake-reveal-warn">
        Shown once. The old link has stopped working; send this one to the recipient yourself.
      </p>
      <div className="intake-mint-url">{url}</div>
      <div className="intake-reveal-actions">
        <button
          type="button"
          className="intake-copy"
          onClick={() => {
            void navigator.clipboard?.writeText(url).then(
              () => setCopied(true),
              () => {},
            );
          }}
        >
          {copied ? "Copied ✓" : "Copy link"}
        </button>
        <button type="button" className="intake-btn" onClick={onClose}>
          Done
        </button>
      </div>
    </section>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0] ?? "";
  const last = parts[parts.length - 1] ?? "";
  if (parts.length === 1) return first.slice(0, 2).toUpperCase();
  return (first.slice(0, 1) + last.slice(0, 1)).toUpperCase();
}
