// The Body tab's trained walks (owner, 2026-11-14: "load these policies ... pull it directly
// from the tasks that train them"). One sheet off the Move pane's "Walk with" row: the box's
// policy library (rename, delete), then the finished Jobs training runs it can add from.

import { useEffect, useState } from "react";
import { type GrowbotPolicy, type GrowbotPolicySource, api } from "../api/client";
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
  onRenamed,
  onDeleted,
  say,
}: {
  p: GrowbotPolicy;
  onRenamed: (p: GrowbotPolicy) => void;
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
              say={say}
              onRenamed={(n) => onChange((all) => all.map((x) => (x.id === n.id ? n : x)))}
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
