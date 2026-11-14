// The Body tab's trained walks (owner, 2026-11-14: "load these policies ... pull it directly
// from the tasks that train them"). One sheet off the Move pane's "Walk with" row: the box's
// policy library, then the finished Jobs training runs it can add from. Each policy can be
// renamed, deleted, switched off, given a motion, and picked as its family's one (owner,
// 2026-11-14: "in manage we should be able to change policy availability ... or set motion
// types ... for each one in a family"); `offered` is what the pad then shows.

import { useEffect, useState } from "react";
import {
  type GrowbotMotion,
  type GrowbotPolicy,
  type GrowbotPolicyPatch,
  type GrowbotPolicySource,
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

/** What kind of walk the pad drives, in words: which end leads and what it takes. */
export function walkKind(p: GrowbotPolicy): string {
  const lead = p.contract.facing === "camera" ? "camera end first" : "nose first";
  return p.contract.speed ? `${lead}, with speed` : `${lead}, heading only`;
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

  return (
    <Sheet title="Trained walks" onClose={onClose}>
      <div className="gb-sec-h">On the box</div>
      {policies.length === 0 ? (
        <p className="gb-hint dim">None yet. Add one from a finished training job below.</p>
      ) : (
        <div className="gb-list">
          {policies.map((p) => (
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
                    (all ?? []).map((s) =>
                      s.job_id === p.source_job ? { ...s, added: false } : s,
                    ),
                  );
                }
              }}
            />
          ))}
        </div>
      )}

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
