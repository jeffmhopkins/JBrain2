// The Body tab's trained walks (owner, 2026-11-14: "load these policies ... pull it directly
// from the tasks that train them"). One sheet off the Move pane's "Walk with" row: the box's
// policy library, then the finished Jobs training runs it can add from. Each policy can be
// renamed, deleted, switched off, given a motion, and picked as its family's one (owner,
// 2026-11-14: "in manage we should be able to change policy availability ... or set motion
// types ... for each one in a family"); `offered` is what the pad then shows.

import { useEffect, useState } from "react";
import {
  type GrowbotKeptSync,
  type GrowbotMotion,
  type GrowbotPolicy,
  type GrowbotPolicyPatch,
  type GrowbotPolicySource,
  type GrowbotSkill,
  type GrowbotSkillSet,
  api,
} from "../api/client";
import { Sheet } from "./Sheet";
import { useArm } from "./growbotArm";

interface Props {
  policies: GrowbotPolicy[];
  // An update, not a value: a rename can land after an add, and must not drop it.
  onChange: (update: (all: GrowbotPolicy[]) => GrowbotPolicy[]) => void;
  onClose: () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const DAY = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : DAY.format(d);
}

const MOTIONS: { id: GrowbotMotion; label: string }[] = [
  { id: "walk", label: "Walk" },
  { id: "turn", label: "Turn" },
  { id: "trick", label: "Trick" },
];

export const motionOf = (p: GrowbotPolicy): GrowbotMotion => p.motion ?? "walk";
const enabledOf = (p: GrowbotPolicy): boolean => p.enabled !== false;

/** The offered policy standing for `id`: itself, else the one its family now offers for the
 *  same motion (the owner picked another, or switched it off), so a pick follows Manage. */
export function offeredFor(
  all: GrowbotPolicy[],
  shown: GrowbotPolicy[],
  id: string,
): GrowbotPolicy | null {
  const was = all.find((p) => p.id === id);
  if (!was) return null;
  return (
    shown.find((p) => p.id === id) ?? shown.find((p) => p.source_spec === was.source_spec) ?? null
  );
}

/** The policies the pad offers for `motion`: the enabled ones, one per family (source_spec),
 *  the family's picked one, else its newest. In the library's order. */
export function offered(all: GrowbotPolicy[], motion: GrowbotMotion): GrowbotPolicy[] {
  const pick = new Map<string, GrowbotPolicy>();
  for (const p of all) {
    if (!enabledOf(p) || motionOf(p) !== motion) continue;
    const had = pick.get(p.source_spec);
    if (
      !had ||
      (p.active && !had.active) ||
      (!!p.active === !!had.active && p.created_at > had.created_at)
    ) {
      pick.set(p.source_spec, p);
    }
  }
  const chosen = new Set(pick.values());
  return all.filter((p) => chosen.has(p));
}

const SKILLS: GrowbotSkill[] = ["look", "forward", "back"];

/** The skill set Drive can steer (growbot-drive.js): for each skill, the enabled policy that
 *  serves it (for back, a nose-first one first), active first, then newest. Null unless it covers at least two skills, since one
 *  file alone is just that walk. */
export function skillSet(all: GrowbotPolicy[]): GrowbotSkillSet | null {
  const set: GrowbotSkillSet = {};
  for (const skill of SKILLS) {
    let best: GrowbotPolicy | null = null;
    for (const p of all) {
      if (!enabledOf(p) || !(p.contract.modes ?? []).includes(skill)) continue;
      // Backing is the nose walk's own gait (owner, 2026-11-15), over a camera walk's reverse.
      const nose = (q: GrowbotPolicy) => skill === "back" && q.contract.facing === "nose";
      if (
        !best ||
        (nose(p) && !nose(best)) ||
        (nose(p) === nose(best) &&
          ((p.active && !best.active) ||
            (!!p.active === !!best.active && p.created_at > best.created_at)))
      ) {
        best = p;
      }
    }
    if (best) set[skill] = best.id;
  }
  return Object.keys(set).length >= 2 ? set : null;
}

// What each teacher taught its master, in the pad's words.
const ROLES: Record<string, string> = { look: "Turns", walk: "Forward", back: "Back" };

export interface Family {
  master: GrowbotPolicy;
  kids: { role: string; p: GrowbotPolicy }[];
}

/** The sheet's grouping (owner, 2026-11-16, mock B in docs/mocks/growbot-master-family): each
 *  master with the walks it learned from that are on the box, and the walks no master names.
 *  A walk that taught a master shows only under it (under each, when several share it). */
export function families(all: GrowbotPolicy[]): { masters: Family[]; rest: GrowbotPolicy[] } {
  const bySlug = new Map<string, GrowbotPolicy>();
  for (const p of all) if (p.kept) bySlug.set(p.kept, p);
  const taught = new Set<string>();
  const masters: Family[] = [];
  // A master that taught another stays a master of its own rather than sit at two levels.
  const isMaster = new Set(all.filter((p) => p.teachers?.length).map((p) => p.id));
  for (const master of all) {
    if (!master.teachers?.length) continue;
    const kids: Family["kids"] = [];
    for (const t of master.teachers) {
      const p = bySlug.get(t.slug);
      if (!p || isMaster.has(p.id)) continue;
      kids.push({ role: ROLES[t.role] ?? t.role, p });
      taught.add(p.id);
    }
    masters.push({ master, kids });
  }
  return { masters, rest: all.filter((p) => !isMaster.has(p.id) && !taught.has(p.id)) };
}

/** What kind of walk the pad drives, in words: which end leads and what it takes. */
export function walkKind(p: GrowbotPolicy): string {
  const lead = p.contract.facing === "camera" ? "camera end first" : "nose first";
  return p.contract.speed ? `${lead}, with speed` : `${lead}, heading only`;
}

/** One line on what a refresh from the training repo did. */
export function keptText(r: GrowbotKeptSync): string {
  const parts = [
    r.added.length ? `Added ${r.added.join(", ")}` : "",
    r.updated.length ? `Updated ${r.updated.join(", ")}` : "",
    r.removed.length ? `Removed ${r.removed.join(", ")}` : "",
    r.skipped.length ? `Couldn't take ${r.skipped.join("; ")}` : "",
  ].filter(Boolean);
  const at = r.commit ? ` (training repo at ${r.commit.slice(0, 7)})` : "";
  return parts.length ? `${parts.join(". ")}${at}.` : `Up to date with the training repo${at}.`;
}

// A trained walk's starting name: the job's kind and the day it finished.
function startName(s: GrowbotPolicySource): string {
  const kind = s.spec.replace(/^growbot_/, "").replace(/_/g, " ");
  const day = when(s.finished_at);
  const name = `${kind.charAt(0).toUpperCase()}${kind.slice(1)}${day ? `, ${day}` : ""}`;
  return name.slice(0, 60);
}

function Row({
  p,
  inUse,
  siblings,
  onRenamed,
  onPicked,
  onDeleted,
  say,
}: {
  p: GrowbotPolicy;
  // The pad offers this one now.
  inUse: boolean;
  // Others of its family with the same motion, enabled: only then is there a pick to make.
  siblings: number;
  onRenamed: (p: GrowbotPolicy) => void;
  onPicked: (p: GrowbotPolicy) => void;
  onDeleted: () => void;
  say: (text: string, bad?: boolean) => void;
}) {
  const [name, setName] = useState<string | null>(null);
  const { isArmed, arm } = useArm();
  const key = `pol:${p.id}`;

  async function rename() {
    const next = (name ?? "").trim();
    setName(null);
    if (!next || next === p.name) return;
    try {
      onRenamed(await api.renameGrowbotPolicy(p.id, next));
    } catch (e) {
      say(message(e), true);
    }
  }

  async function patch(change: GrowbotPolicyPatch) {
    try {
      const got = await api.updateGrowbotPolicy(p.id, change);
      if (change.active) onPicked(got);
      else onRenamed(got);
    } catch (e) {
      say(message(e), true);
    }
  }

  async function remove() {
    if (!arm(key)) return;
    try {
      await api.deleteGrowbotPolicy(p.id);
      onDeleted();
      say(`Deleted ${p.name}.`);
    } catch (e) {
      say(message(e), true);
    }
  }

  return (
    <div className="gb-li gb-pol-row">
      {name === null ? (
        <span className="gb-tx">
          <span className="gb-t1">{p.name}</span>
          <span className="gb-t2">
            {walkKind(p)} · {Math.max(1, Math.round(p.bytes / 1024))} KB · added{" "}
            {when(p.created_at)}
            {!enabledOf(p) ? " · off" : inUse ? " · on the pad" : ""}
          </span>
          <span className="gb-chips gb-pol-switches">
            <button
              type="button"
              className={`gb-chip${enabledOf(p) ? " on" : ""}`}
              aria-pressed={enabledOf(p)}
              aria-label={`Offer ${p.name} on the pad`}
              onClick={() => void patch({ enabled: !enabledOf(p) })}
            >
              {enabledOf(p) ? "On" : "Off"}
            </button>
            {MOTIONS.map((m) => (
              <button
                key={m.id}
                type="button"
                className={`gb-chip${motionOf(p) === m.id ? " on" : ""}`}
                aria-pressed={motionOf(p) === m.id}
                aria-label={`${p.name} is a ${m.label.toLowerCase()}`}
                onClick={() => motionOf(p) !== m.id && void patch({ motion: m.id })}
              >
                {m.label}
              </button>
            ))}
            {enabledOf(p) && siblings > 0 && !inUse && (
              <button
                type="button"
                className="gb-chip add"
                aria-label={`Use ${p.name} for its family`}
                onClick={() => void patch({ active: true })}
              >
                Use this one
              </button>
            )}
          </span>
        </span>
      ) : (
        <input
          className="gb-pol-name"
          aria-label={`New name for ${p.name}`}
          value={name}
          maxLength={60}
          // biome-ignore lint/a11y/noAutofocus: the field opens on the owner's own tap.
          autoFocus
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void rename();
            if (e.key === "Escape") setName(null);
          }}
          onBlur={() => void rename()}
        />
      )}
      {name === null && (
        <button
          type="button"
          className="gb-btn"
          aria-label={`Rename ${p.name}`}
          onClick={() => setName(p.name)}
        >
          Rename
        </button>
      )}
      <button
        type="button"
        className={`gb-btn danger${isArmed(key) ? " armed" : ""}`}
        aria-label={isArmed(key) ? `Tap again to delete ${p.name}` : `Delete ${p.name}`}
        onClick={() => void remove()}
      >
        {isArmed(key) ? "Sure?" : "Delete"}
      </button>
    </div>
  );
}

export function GrowbotPoliciesSheet({ policies, onChange, onClose }: Props) {
  const [sources, setSources] = useState<GrowbotPolicySource[] | null>(null);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);
  // Which masters are open to show their teachers; all start closed.
  const [open, setOpen] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let gone = false;
    api.growbotPolicySources().then(
      (got) => !gone && setSources(got),
      (e: unknown) => {
        if (gone) return;
        setSources([]);
        setNote({ text: message(e), bad: true });
      },
    );
    return () => {
      gone = true;
    };
  }, []);

  const say = (text: string, bad = false) => setNote({ text, bad });

  // Walks kept in the training repo (owner, 2026-11-14: "finished policies should get saved
  // to our training repo, and the growbot GUI ... should pull from that on a refresh").
  async function pullKept() {
    if (busy) return;
    setBusy("kept");
    try {
      const r = await api.refreshGrowbotKept();
      const fresh = await api.growbotPolicies();
      onChange(() => fresh);
      say(keptText(r), r.skipped.length > 0);
    } catch (e) {
      say(message(e), true);
    } finally {
      setBusy("");
    }
  }

  async function add(s: GrowbotPolicySource) {
    if (busy) return;
    setBusy(s.job_id);
    try {
      const made = await api.addGrowbotPolicy(s.job_id, startName(s));
      onChange((all) => [made, ...all]);
      setSources((all) =>
        (all ?? []).map((x) => (x.job_id === s.job_id ? { ...x, added: true } : x)),
      );
      say(`Added ${made.name}. Pick it under "Walk with" to drive it.`);
    } catch (e) {
      say(message(e), true);
    } finally {
      setBusy("");
    }
  }

  const { masters, rest } = families(policies);

  function row(p: GrowbotPolicy) {
    return (
      <Row
        key={p.id}
        p={p}
        inUse={offered(policies, motionOf(p)).includes(p)}
        siblings={
          policies.filter(
            (x) =>
              x.id !== p.id &&
              enabledOf(x) &&
              x.source_spec === p.source_spec &&
              motionOf(x) === motionOf(p),
          ).length
        }
        say={say}
        onRenamed={(n) => onChange((all) => all.map((x) => (x.id === n.id ? n : x)))}
        onPicked={(n) =>
          onChange((all) =>
            all.map((x) =>
              x.id === n.id
                ? n
                : x.source_spec === n.source_spec && motionOf(x) === motionOf(n)
                  ? { ...x, active: false }
                  : x,
            ),
          )
        }
        onDeleted={() => {
          onChange((all) => all.filter((x) => x.id !== p.id));
          // Its job can be added again, unless another copy from it is still on the box.
          const kept = policies.some((x) => x.id !== p.id && x.source_job === p.source_job);
          if (!kept) {
            setSources((all) =>
              (all ?? []).map((s) => (s.job_id === p.source_job ? { ...s, added: false } : s)),
            );
          }
        }}
      />
    );
  }

  return (
    <Sheet title="Trained walks" onClose={onClose}>
      {policies.length === 0 ? (
        <>
          <div className="gb-sec-h">On the box</div>
          <p className="gb-hint dim">None yet. Add one from a finished training job below.</p>
        </>
      ) : (
        <>
          {masters.length > 0 && (
            <>
              <div className="gb-sec-h">Masters and what they learned from</div>
              <div className="gb-list">
                {masters.map((f) => (
                  <div key={f.master.id} className="gb-fam">
                    {row(f.master)}
                    {f.kids.length > 0 && (
                      <button
                        type="button"
                        className="gb-fam-toggle"
                        aria-expanded={!!open[f.master.id]}
                        aria-controls={`gb-fam-${f.master.id}`}
                        onClick={() => setOpen((o) => ({ ...o, [f.master.id]: !o[f.master.id] }))}
                      >
                        <span className="gb-fam-car" aria-hidden="true">
                          ›
                        </span>
                        Learned from {f.kids.length} {f.kids.length === 1 ? "walk" : "walks"}
                      </button>
                    )}
                    {/* Hidden, not unmounted: a half-typed rename or an armed delete in a teacher's row
                        survives closing its master. */}
                    <div
                      id={`gb-fam-${f.master.id}`}
                      className="gb-fam-kids"
                      hidden={!open[f.master.id]}
                    >
                      {f.kids.map((k) => (
                        <div key={`${k.role}:${k.p.id}`} className="gb-fam-kid">
                          <span className="gb-fam-role">{k.role}</span>
                          <div className="gb-list">{row(k.p)}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
          <div className="gb-sec-h">{masters.length > 0 ? "Other walks" : "On the box"}</div>
          {rest.length === 0 ? (
            <p className="gb-hint dim">
              None. Every walk on the box taught a master, so it is under that master above.
            </p>
          ) : (
            <div className="gb-list">{rest.map(row)}</div>
          )}
        </>
      )}

      <div className="gb-sec-h">From the training repo</div>
      <div className="gb-li gb-pol-row">
        <span className="gb-tx">
          <span className="gb-t2">
            Walks we kept in GrowBot_Training. Refresh brings in new and changed ones.
          </span>
        </span>
        <button
          type="button"
          className="gb-btn"
          disabled={!!busy}
          aria-label="Refresh walks from the training repo"
          onClick={() => void pullKept()}
        >
          {busy === "kept" ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <div className="gb-sec-h">From finished training jobs</div>
      {sources === null ? (
        <p className="gb-hint dim">Looking at Jobs…</p>
      ) : sources.length === 0 ? (
        <p className="gb-hint dim">
          No finished GrowBot training yet. Start a GrowBot walk on the Jobs screen; when it
          finishes, its walk shows up here.
        </p>
      ) : (
        <div className="gb-list">
          {sources.map((s) => (
            <div key={s.job_id} className="gb-li gb-pol-row">
              <span className="gb-tx">
                <span className="gb-t1">{s.title}</span>
                <span className="gb-t2">finished {when(s.finished_at) || "recently"}</span>
              </span>
              {s.added ? (
                <span className="gb-tag">On the box</span>
              ) : (
                <button
                  type="button"
                  className="gb-btn primary"
                  disabled={!!busy}
                  aria-label={`Add the walk from ${s.title}`}
                  onClick={() => void add(s)}
                >
                  {busy === s.job_id ? "Adding…" : "Add"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {note && (
        <p className={`gb-hint${note.bad ? " gb-bad-t" : ""}`} aria-live="polite">
          {note.text}
        </p>
      )}
    </Sheet>
  );
}
