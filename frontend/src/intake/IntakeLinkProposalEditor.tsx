// The editable intake-link Proposal (W6, docs/mocks/guided-intake/proposal-b-preview.html):
// the owner edits a staged mint-a-link config, flips to a live recipient preview, then
// approves → mints the secret SHOW-ONCE. Replaces the generic node list in ProposalTree
// for `intake-link` proposals. Subject + domain are FIXED at staging (re-validated at the
// mint FK), so they render read-only here — only the soft fields patch (§7).

import { useState } from "react";
import { ApiError, api } from "../api/client";
import { DOMAIN_TITLE } from "../notes/modes";
import { intakeShareUrl } from "./share";
import type { IntakeConfigPatch, IntakeMintResult } from "./types";
import "./owner.css";

interface ProposalNodeLike {
  id: string;
  preview: Record<string, unknown>;
  status: string;
}

export interface IntakeLinkEditorDeps {
  patchConfig: (nodeId: string, patch: IntakeConfigPatch) => Promise<void>;
  mintFromProposal: (proposalId: string) => Promise<IntakeMintResult>;
  rejectNode: (proposalId: string, nodeId: string) => Promise<void>;
}

interface Props {
  proposalId: string;
  node: ProposalNodeLike;
  onClose: () => void;
  /** Fired after a successful mint so the panel can refresh the proposal list. */
  onMinted?: (() => void) | undefined;
  deps?: IntakeLinkEditorDeps;
}

// TTL presets the mock offers (hours). A staged value off the grid still mints — the
// nearest preset just shows un-highlighted.
const TTL_PRESETS: { label: string; hours: number }[] = [
  { label: "1h", hours: 1 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
  { label: "30d", hours: 720 },
];

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}
function num(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}
function bool(v: unknown, fallback: boolean): boolean {
  return typeof v === "boolean" ? v : fallback;
}

export function IntakeLinkProposalEditor({ proposalId, node, onClose, onMinted, deps }: Props) {
  const patchConfig = deps?.patchConfig ?? api.patchIntakeProposalConfig;
  const mintFromProposal = deps?.mintFromProposal ?? api.mintIntakeLinkFromProposal;
  const rejectNode =
    deps?.rejectNode ?? ((pid: string, nid: string) => api.decideNode(pid, nid, "reject"));

  const cfg = node.preview;
  const subjectId = str(cfg.subject_id);
  const domain = str(cfg.domain, "general");

  const [tab, setTab] = useState<"edit" | "preview">("edit");
  const [personaBrief, setPersonaBrief] = useState(str(cfg.persona_brief));
  const [fieldsBrief, setFieldsBrief] = useState(str(cfg.fields_brief));
  const [openingBlurb, setOpeningBlurb] = useState(str(cfg.opening_blurb));
  const [label, setLabel] = useState(str(cfg.label));
  const [bindOnFirst, setBindOnFirst] = useState(bool(cfg.bind_on_first, false));
  const [maxRuns, setMaxRuns] = useState(num(cfg.max_runs, 1));
  const [maxOpens, setMaxOpens] = useState(num(cfg.max_opens, num(cfg.max_runs, 1) * 4));
  const [ttlHours, setTtlHours] = useState(num(cfg.ttl_hours, 24));
  const [captureName, setCaptureName] = useState(bool(cfg.capture_enterer_name, true));
  const [discloseOwner, setDiscloseOwner] = useState(bool(cfg.disclose_owner_identity, false));

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [armReject, setArmReject] = useState(false);
  const [minted, setMinted] = useState<IntakeMintResult | null>(null);
  const [copied, setCopied] = useState(false);

  const rejected = node.status === "rejected";

  async function mint(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      // Persist every editable field, then mint from the (now-current) staged config.
      await patchConfig(node.id, {
        persona_brief: personaBrief,
        fields_brief: fieldsBrief.trim(),
        opening_blurb: openingBlurb,
        label,
        max_runs: maxRuns,
        max_opens: maxOpens,
        bind_on_first: bindOnFirst,
        ttl_hours: ttlHours,
        capture_enterer_name: captureName,
        disclose_owner_identity: discloseOwner,
      });
      setMinted(await mintFromProposal(proposalId));
      onMinted?.();
    } catch (e) {
      // Surface the server's actual reason (e.g. an invalid subject) — the generic
      // "check the details" is a dead end when the failing field is read-only here.
      setError(e instanceof ApiError ? e.message : "Couldn't mint the link — please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function reject(): Promise<void> {
    if (!armReject) {
      setArmReject(true);
      return;
    }
    setBusy(true);
    try {
      await rejectNode(proposalId, node.id);
      onClose();
    } catch {
      setError("Couldn't reject — try again.");
      setBusy(false);
    }
  }

  async function copyLink(secret: string): Promise<void> {
    try {
      await navigator.clipboard?.writeText(intakeShareUrl(secret));
      setCopied(true);
    } catch {
      // Clipboard may be unavailable; the URL is shown for manual copy.
    }
  }

  // After minting, the panel becomes the show-once secret card — the only time the
  // link is recoverable. Re-mint (from the management screen) to re-send.
  if (minted !== null) {
    const url = intakeShareUrl(minted.secret);
    return (
      <section className="intake-prop" aria-label="Intake link minted">
        <div className="panel-bar">
          <button type="button" className="back" aria-label="Done" onClick={onClose}>
            ‹
          </button>
          <span className="ttl">Link minted</span>
        </div>
        <div className="intake-mint-done">
          <div className="intake-mint-mark" aria-hidden="true">
            ✓
          </div>
          <h2>Your link is ready</h2>
          <p className="intake-mint-warn">
            This is the only time the link is shown. Copy it now and send it to the recipient
            yourself — to re-send later, re-mint from Intake Links.
          </p>
          <div className="intake-mint-url">{url}</div>
          <button
            type="button"
            className="intake-copy"
            onClick={() => void copyLink(minted.secret)}
          >
            {copied ? "Copied ✓" : "Copy link"}
          </button>
        </div>
      </section>
    );
  }

  function setTtlPreset(hours: number): void {
    setTtlHours(hours);
    // Total opens defaults to 4× submissions; opens scaling is independent of TTL.
  }

  return (
    <section className="intake-prop" aria-label="Intake link proposal">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to proposals" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">Intake link</span>
        <span className="sub">staged · awaiting you</span>
      </div>

      <div className="intake-prop-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "edit"}
          className={tab === "edit" ? "on" : ""}
          onClick={() => setTab("edit")}
        >
          Edit
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "preview"}
          className={tab === "preview" ? "on" : ""}
          onClick={() => setTab("preview")}
        >
          Preview
        </button>
      </div>

      {tab === "edit" ? (
        <div className="intake-prop-body">
          <p className="intake-prop-prov">
            Drafted by the assistant. Edit, flip to Preview to see the recipient's view, then
            approve to mint.
          </p>

          <label className="intake-prop-card">
            <span className="ic-h">Agent prompt</span>
            <textarea
              aria-label="Agent prompt"
              rows={4}
              value={personaBrief}
              placeholder="Optional — tone and manner (e.g. “warm and patient”). The interviewer already has a professional voice; this just tunes it."
              onChange={(e) => setPersonaBrief(e.target.value)}
            />
            <span className="ic-note">
              Optional framing for the interviewer's tone — it already knows what to collect (below)
              and follows fixed rules. Nothing secret; a visitor could read it back.
            </span>
          </label>

          <label className="intake-prop-card">
            <span className="ic-h">What to collect</span>
            <textarea
              aria-label="What to collect"
              rows={3}
              value={fieldsBrief}
              onChange={(e) => setFieldsBrief(e.target.value)}
            />
          </label>

          <label className="intake-prop-card">
            <span className="ic-h">Opening blurb</span>
            <textarea
              aria-label="Opening blurb"
              rows={3}
              value={openingBlurb}
              onChange={(e) => setOpeningBlurb(e.target.value)}
            />
          </label>

          <label className="intake-prop-card">
            <span className="ic-h">Label</span>
            <input
              type="text"
              aria-label="Label"
              placeholder="A short name for this link"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </label>

          <div className="intake-prop-card">
            <span className="ic-h">Who &amp; where</span>
            {/* Subject + domain are fixed at staging (re-validated at mint), so read-only. */}
            <div className="ic-fixed">
              <span className="ic-fixed-k">About</span>
              <span className="ic-fixed-v">
                {subjectId ? subjectId : "No specific person"} · {DOMAIN_TITLE[domain] ?? domain}
              </span>
            </div>
            <span className="ic-note">
              {subjectId
                ? "The subject and domain are locked for this link — they can't be changed here."
                : "A general collection, not about a specific person. The domain is locked here."}
            </span>
          </div>

          <div className="intake-prop-card">
            <span className="ic-h">Limits</span>
            <div className="ic-seg" aria-label="Binding">
              <button
                type="button"
                aria-pressed={bindOnFirst}
                className={bindOnFirst ? "on" : ""}
                onClick={() => setBindOnFirst(true)}
              >
                One person
              </button>
              <button
                type="button"
                aria-pressed={!bindOnFirst}
                className={!bindOnFirst ? "on" : ""}
                onClick={() => setBindOnFirst(false)}
              >
                Open / many
              </button>
            </div>
            <div className="ic-two">
              <Stepper label="Submissions" value={maxRuns} min={1} onChange={setMaxRuns} />
              <Stepper label="Total opens" value={maxOpens} min={1} onChange={setMaxOpens} />
            </div>
            <div className="ic-seg" aria-label="Expires after">
              {TTL_PRESETS.map((p) => (
                <button
                  type="button"
                  key={p.hours}
                  aria-pressed={ttlHours === p.hours}
                  className={ttlHours === p.hours ? "on" : ""}
                  onClick={() => setTtlPreset(p.hours)}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          <div className="intake-prop-card">
            <span className="ic-h">Recipient</span>
            <Toggle
              label="Ask the recipient's name"
              note="Captured as provenance."
              on={captureName}
              onToggle={() => setCaptureName((v) => !v)}
            />
            <Toggle
              label="Show your name to the recipient"
              note="Off → a generic link. On → names you as the person asking."
              on={discloseOwner}
              onToggle={() => setDiscloseOwner((v) => !v)}
            />
          </div>
        </div>
      ) : (
        <div className="intake-prop-body">
          <p className="intake-prop-prov">What the recipient will see when they open the link.</p>
          <div className="intake-preview">
            <div className="ipv-head">
              <span className="ipv-mark" aria-hidden="true">
                ◆
              </span>
              <div>
                <div className="ipv-ttl">A private collection link</div>
                <div className="ipv-sub">You've been asked to share some information</div>
              </div>
            </div>
            <div className="ipv-body">
              <h3>You've been invited to share some details</h3>
              <p className="ipv-blurb">{openingBlurb.trim() || "Please answer a few questions."}</p>
              {captureName && (
                <div className="ipv-field">
                  <span className="ipv-label">Your name</span>
                  <span className="ipv-fake">e.g. Carol Hopkins</span>
                </div>
              )}
              <div className="ipv-consent">
                <span aria-hidden="true">🛡️</span>
                <span>
                  {discloseOwner
                    ? "Sent privately to the person who asked, for their own records, and reviewed before anything is kept."
                    : "Sent privately to the link's owner for their own records, and reviewed before anything is kept."}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}

      {error && <p className="intake-prop-error">{error}</p>}
      {rejected && <p className="intake-prop-error">This proposal was rejected.</p>}
      {!fieldsBrief.trim() && !rejected && (
        <p className="intake-prop-error">Add what the interviewer should collect before minting.</p>
      )}

      <div className="intake-prop-actions">
        <button
          type="button"
          className={`intake-reject${armReject ? " armed" : ""}`}
          disabled={busy || rejected}
          onClick={() => void reject()}
        >
          {armReject ? "Tap again — reject" : "Reject"}
        </button>
        <button
          type="button"
          className="intake-mint"
          disabled={busy || rejected || !fieldsBrief.trim()}
          onClick={() => void mint()}
        >
          Approve &amp; mint
        </button>
      </div>
    </section>
  );
}

function Stepper({
  label,
  value,
  min,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="ic-stepper">
      <span className="ic-stepper-l">{label}</span>
      <div className="ic-num">
        <button
          type="button"
          aria-label={`decrease ${label}`}
          onClick={() => onChange(Math.max(min, value - 1))}
        >
          −
        </button>
        <input
          type="number"
          aria-label={label}
          value={value}
          min={min}
          onChange={(e) => onChange(Math.max(min, Number.parseInt(e.target.value, 10) || min))}
        />
        <button type="button" aria-label={`increase ${label}`} onClick={() => onChange(value + 1)}>
          +
        </button>
      </div>
    </div>
  );
}

function Toggle({
  label,
  note,
  on,
  onToggle,
}: {
  label: string;
  note: string;
  on: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="ic-toggle">
      <span className="ic-toggle-l">
        <span className="ic-toggle-t">{label}</span>
        <span className="ic-toggle-d">{note}</span>
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={label}
        className={`ic-sw${on ? " on" : ""}`}
        onClick={onToggle}
      >
        <span className="ic-knob" />
      </button>
    </div>
  );
}
