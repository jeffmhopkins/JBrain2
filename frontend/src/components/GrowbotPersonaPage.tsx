/** A persona's page (DESIGN.md "GrowBot — the desk robot's screen", "A persona's page"): its
 *  name with where it is, its own tabs, and Open <name>… in the foot. Every write the box
 *  checkpoints first answers with that checkpoint, and the toast's Undo restores it.
 *
 *  The Memories tab reads its visual memories when it opens rather than with the persona: they
 *  are pictures, and most visits never look at them. */
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  type GrowbotCheckpoint,
  type GrowbotFace,
  type GrowbotMemory,
  type GrowbotPersona,
  type GrowbotPersonaDetail,
  type GrowbotSoul,
  type GrowbotWritten,
  api,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import {
  FACES,
  MEMORY,
  type MemoryId,
  type Places,
  REGIONS,
  type RegionInfo,
  SAFETY_NOTE,
  ago,
  clip,
  day,
  memoryForgot,
  onPhone,
  phoneName,
  regionText,
  robotName,
  selfName,
  when,
  where,
  words,
} from "../growbot";
import { Dialog } from "./Dialog";
import { GrowbotCheckpoints } from "./GrowbotCheckpoints";
import { GrowbotMemoryGrid } from "./GrowbotMemories";
import { GrowbotMemoryEditor } from "./GrowbotMemoryEditor";
import { GrowbotTabs, type Say } from "./GrowbotPersonaSheets";
import { Sheet } from "./Sheet";
import { useArm } from "./growbotArm";
import { ChevronLeftIcon } from "./icons";

type PTab = "soul" | "vm" | "cps" | "manage";

interface Props {
  persona: GrowbotPersona;
  pl: Places;
  say: Say;
  onBack: () => void;
  onOpen: () => void;
  /** Something about it changed on the box: the screen re-reads the overview. */
  onChanged: () => void;
  onGone: () => void;
  onDuplicated: (id: string) => void;
  onSeed: (checkpoint: string) => void;
  onImport: () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function asText(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function Floor() {
  return (
    <div className="gb-floor">
      <span className="gb-lk">locked</span>
      <span>{SAFETY_NOTE}</span>
    </div>
  );
}

export function GrowbotPersonaPage({
  persona,
  pl,
  say,
  onBack,
  onOpen,
  onChanged,
  onGone,
  onDuplicated,
  onSeed,
  onImport,
}: Props) {
  const [detail, setDetail] = useState<GrowbotPersonaDetail | null>(null);
  const [error, setError] = useState("");
  const [ptab, setPtab] = useState<PTab>("soul");
  const [editing, setEditing] = useState<RegionInfo | null>(null);
  const [remembering, setRemembering] = useState<MemoryId | null>(null);
  const [renaming, setRenaming] = useState(false);
  // The face being saved, shown picked while it is.
  const [facing, setFacing] = useState<GrowbotFace | null>(null);
  const [restoring, setRestoring] = useState<GrowbotCheckpoint | null>(null);
  const [mems, setMems] = useState<GrowbotMemory[] | null>(null);
  const [memError, setMemError] = useState("");
  // Bumped by every write that can change them (a restore brings a checkpoint's back; a reset
  // or an import clears them), so an open Memories tab reads them again.
  const [memAsk, setMemAsk] = useState(0);
  const { isArmed, arm } = useArm();
  useBackLayer(onBack);
  // The page replaces the list, so focus moves to its name rather than staying on a row
  // that is gone.
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
  }, []);

  const load = useCallback(async () => {
    try {
      setDetail(await api.growbotPersona(persona.id));
      setError("");
    } catch (e) {
      setError(message(e));
    }
  }, [persona.id]);

  // A newer version means the creature (or another screen) wrote it: read it again.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the version is the trigger.
  useEffect(() => {
    void load();
  }, [load, persona.version]);

  // Numbered as asked, and only the newest answer is taken: a read that set out before a
  // delete would otherwise put the forgotten memory back on screen.
  const memAsked = useRef(0);
  const loadMems = useCallback(async () => {
    const at = ++memAsked.current;
    try {
      const got = await api.growbotMemories(persona.id);
      if (at !== memAsked.current) return;
      setMems(got);
      setMemError("");
      setDetail((d) => (d ? { ...d, memories: got.length } : d));
    } catch (e) {
      if (at === memAsked.current) setMemError(message(e));
    }
  }, [persona.id]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: the version and memAsk are triggers.
  useEffect(() => {
    if (ptab === "vm") void loadMems();
  }, [ptab, loadMems, persona.version, memAsk]);

  const p = persona;
  const live = p.phone
    ? ` ${p.name} picks it up the next time it's idle ${onPhone(p.phone, pl)}.`
    : "";

  function took(out: GrowbotWritten) {
    setDetail(out.persona);
    setMemAsk((n) => n + 1);
    onChanged();
  }

  /** Undo from a toast: the same restore, with its own checkpoint, without the confirm. */
  function undoTo(cp: GrowbotCheckpoint) {
    return () => {
      void api
        .restoreGrowbotCheckpoint(p.id, cp.id)
        .then((out) => {
          took(out);
          say(`Undone — ${p.name} is as it was before.`);
        })
        .catch((e) => say(`It wasn't undone — ${message(e)}`));
    };
  }

  async function saveCheckpoint(name: string): Promise<boolean> {
    try {
      await api.saveGrowbotCheckpoint(p.id, name);
      await load();
      onChanged();
      say(`Saved “${name}”.`);
      return true;
    } catch (e) {
      say(`The checkpoint wasn't saved — ${message(e)}`);
      return false;
    }
  }

  /** Gone for good: deleting makes no checkpoint of its own, so there is no Undo. */
  async function deleteCheckpoint(cp: GrowbotCheckpoint) {
    try {
      await api.deleteGrowbotCheckpoint(p.id, cp.id);
      setDetail((d) =>
        d
          ? {
              ...d,
              checkpoints: Math.max(0, d.checkpoints - 1),
              checkpoint_list: d.checkpoint_list.filter((c) => c.id !== cp.id),
            }
          : d,
      );
      onChanged();
      say(`Deleted “${cp.name}”.`);
    } catch (e) {
      say(`“${cp.name}” wasn't deleted — ${message(e)}`);
    }
  }

  async function deleteAutomatic() {
    try {
      const n = await api.deleteGrowbotAutomaticCheckpoints(p.id);
      await load();
      onChanged();
      say(`Deleted ${n} automatic checkpoint${n === 1 ? "" : "s"}. The ones you named are kept.`);
    } catch (e) {
      say(`The automatic checkpoints weren't deleted — ${message(e)}`);
    }
  }

  async function restore(cp: GrowbotCheckpoint) {
    setRestoring(null);
    try {
      const out = await api.restoreGrowbotCheckpoint(p.id, cp.id);
      took(out);
      const saved = out.checkpoint;
      say(
        `${p.name} is back to “${cp.name}”.${saved ? ` “${saved.name}” keeps what it was.` : ""}`,
        saved ? { undo: undoTo(saved) } : undefined,
      );
    } catch (e) {
      say(`${p.name} wasn't restored — ${message(e)}`);
    }
  }

  async function duplicate() {
    try {
      const made = await api.duplicateGrowbotPersona(p.id);
      say(
        `Made ${made.name} — its memories and visual memories, no checkpoints. Rename it from its Manage section.`,
      );
      onDuplicated(made.id);
    } catch (e) {
      say(`It wasn't duplicated — ${message(e)}`);
    }
  }

  async function exportSoul() {
    try {
      const { blob, filename } = await api.exportGrowbotPersona(p.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1_000);
      say(
        `Saved ${filename} to Downloads. It has its memories but not its visual memories — those stay on the box.`,
      );
    } catch (e) {
      say(`The soul file didn't download — ${message(e)}`);
    }
  }

  async function reset() {
    if (!arm("reset")) return;
    try {
      const out = await api.resetGrowbotPersona(p.id);
      took(out);
      const cp = out.checkpoint;
      say(
        `${p.name} is a fresh creature again, and its visual memories are cleared.${cp ? ` “${cp.name}” keeps everything it was, visual memories too.` : ""}${live}`,
        cp ? { undo: undoTo(cp) } : undefined,
      );
    } catch (e) {
      say(`${p.name} wasn't reset — ${message(e)}`);
    }
  }

  async function remove() {
    if (!arm("delete")) return;
    try {
      await api.deleteGrowbotPersona(p.id);
      const body = p.wearing
        ? ` ${robotName(p.wearing, pl)} stands still until a persona connects.`
        : "";
      const phone = p.phone ? ` ${phoneName(p.phone, pl, true)} shows that it's gone.` : "";
      say(`${p.name} is deleted, with its memories.${body}${phone}`);
      onGone();
    } catch (e) {
      say(`${p.name} wasn't deleted — ${message(e)}`);
    }
  }

  async function forget(m: GrowbotMemory) {
    if (!arm(`vm:${m.id}`)) return;
    try {
      await api.growbotForgetMemory(p.id, m.id);
    } catch (e) {
      say(`${me} didn't forget ${memoryForgot(m)} — ${message(e)}`);
      return;
    }
    setMems((list) => list?.filter((x) => x.id !== m.id) ?? list);
    setDetail((d) => (d ? { ...d, memories: Math.max(0, d.memories - 1) } : d));
    say(
      `${me} forgot ${memoryForgot(m)}.${p.phone ? ` It forgets ${onPhone(p.phone, pl)} the next time it's idle.` : ""}`,
    );
    void loadMems();
  }

  const soul: GrowbotSoul = detail?.soul ?? {};
  const cps = detail?.checkpoint_list ?? [];
  const n = detail?.checkpoints ?? p.checkpoints;
  const me = selfName(soul, p.name);
  const vmCount = detail?.memories ?? mems?.length ?? p.memories;
  const robot = p.wearing ?? pl.robots.find((r) => !r.revoked_at)?.code ?? null;

  function soulTab() {
    const log = [...(soul.log ?? [])]
      .filter((l) => typeof l?.txt === "string")
      .sort((a, b) => (b.t ?? 0) - (a.t ?? 0))
      .slice(0, 4);
    const mood = typeof soul.scratch?.mood === "string" ? soul.scratch.mood : "";
    const rules = (soul.scratch?.rules ?? []).map(words).filter(Boolean);
    const wants = (soul.goals?.wants ?? []).map(words).filter(Boolean);
    const memory: Record<(typeof MEMORY)[number]["id"], ReactNode> = {
      identity: soul.identity ? (
        <p className="gb-pv">{soul.identity}</p>
      ) : (
        <p className="gb-pv dim">Not written yet — it writes this as it wakes up.</p>
      ),
      bond: soul.identityCore ? (
        <p className="gb-pv">{soul.identityCore}</p>
      ) : (
        <p className="gb-pv dim">
          Not written yet. The creature writes this once, in a dream, and never changes it.
        </p>
      ),
      scratch: (
        <>
          <p className="gb-pv">
            {soul.scratch?.state || "nothing yet"}
            {mood ? ` · feeling ${mood}` : ""}
          </p>
          {rules.length > 0 && (
            <ul>
              {rules.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          )}
        </>
      ),
      goals: (
        <>
          <p className="gb-pv">
            Calls itself <strong>{me}</strong>.
          </p>
          {wants.length > 0 && (
            <ul>
              {wants.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          )}
        </>
      ),
      log: log.length ? (
        <ul>
          {log.map((l) => (
            <li key={`${l.t}-${l.txt}`}>
              {l.txt}
              {l.t ? <span className="gb-dim-t"> · {ago(new Date(l.t).toISOString())}</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="gb-pv dim">No diary lines yet.</p>
      ),
    };
    return (
      <>
        <div className="gb-sec-h">
          Personality and settings<span className="r">tap to edit</span>
        </div>
        <div className="gb-list">
          {REGIONS.map((r) => (
            <button key={r.id} type="button" className="gb-li" onClick={() => setEditing(r)}>
              <span className="gb-tx">
                <span className="gb-t1">{r.label}</span>
                <span className="gb-t2">
                  {clip(regionText(soul, r), 80) || "upstream's default"}
                </span>
              </span>
              <span className="gb-chev" aria-hidden="true">
                ›
              </span>
            </button>
          ))}
        </div>
        <Floor />
        <div className="gb-sec-h">
          What it remembers<span className="r">tap Edit to correct</span>
        </div>
        <section className="gb-card">
          {MEMORY.map((m) => (
            <div key={m.id} className="gb-region">
              <div className="gb-rh">
                <h3>{m.label}</h3>
                <span className="gb-up">{m.up}</span>
                <button
                  type="button"
                  className="gb-linkbtn"
                  aria-label={`Edit ${m.label.toLowerCase()}`}
                  onClick={() => setRemembering(m.id)}
                >
                  Edit
                </button>
              </div>
              {memory[m.id]}
            </div>
          ))}
        </section>
        <p className="gb-hint dim">
          {me} writes these itself, as it goes and in its dreams, and goes on from whatever you
          correct. Every edit saves a checkpoint first.
        </p>
      </>
    );
  }

  function vmTab() {
    const failed = memError ? (
      <p className="gb-msg bad" role="alert">
        {memError}
      </p>
    ) : null;
    if (!mems) return failed ?? <p className="gb-hint dim">Loading {me}'s visual memories…</p>;
    if (!mems.length) {
      return (
        <section className="gb-card">
          {failed}
          <p className="gb-hint dim">
            No saved views yet. It saves one when it meets a person or a thing it wants to remember.
          </p>
        </section>
      );
    }
    return (
      <>
        {failed}
        <section className="gb-card">
          <GrowbotMemoryGrid
            pid={p.id}
            memories={mems}
            isArmed={(m) => isArmed(`vm:${m.id}`)}
            onDelete={(m) => void forget(m)}
          />
          <p className="gb-hint dim gb-gap">
            Saved views of people and things it has met — they name people, so they stay on the box.
            Deleting one makes it forget that face or thing; a checkpoint made before still has it.
          </p>
        </section>
      </>
    );
  }

  function cpsTab() {
    return (
      <GrowbotCheckpoints
        name={p.name}
        cps={cps}
        loaded={detail !== null}
        isArmed={isArmed}
        arm={arm}
        onSave={saveCheckpoint}
        onRestore={setRestoring}
        onSeed={(c) => onSeed(c.id)}
        onDelete={(c) => void deleteCheckpoint(c)}
        onDeleteAutomatic={() => void deleteAutomatic()}
      />
    );
  }

  /** Saved as it is tapped: a face is not a soul write, so nothing reloads and there is no
   *  checkpoint to undo to; tapping the old one back is the undo. */
  async function pickFace(face: GrowbotFace) {
    const was = detail?.face ?? p.face;
    if (face === was || facing !== null) return;
    setFacing(face);
    try {
      took(await api.setGrowbotPersonaFace(p.id, face));
      const name = FACES.find((f) => f.id === face)?.name ?? face;
      say(
        p.phone
          ? `${p.name} wears the ${name.toLowerCase()} face; it changes ${onPhone(p.phone, pl)} in a few seconds.`
          : `${p.name} wears the ${name.toLowerCase()} face next time it opens.`,
      );
    } catch (e) {
      say(`The face didn't change — ${message(e)}`);
    } finally {
      setFacing(null);
    }
  }

  function manageTab() {
    const current = facing ?? detail?.face ?? p.face;
    return (
      <section className="gb-card">
        <fieldset className="gb-choices">
          <legend>Face</legend>
          {FACES.map((f) => (
            <label key={f.id} className={`gb-choice${current === f.id ? " sel" : ""}`}>
              <input
                type="radio"
                name={`gb-face-${p.id}`}
                checked={current === f.id}
                disabled={facing !== null}
                onChange={() => void pickFace(f.id)}
              />
              <span className="gb-tx">
                {f.name}
                <em>{f.what}</em>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="gb-acts two first">
          <button type="button" className="gb-btn" onClick={() => setRenaming(true)}>
            Rename
          </button>
          <button type="button" className="gb-btn" onClick={() => void duplicate()}>
            Duplicate
          </button>
          <button type="button" className="gb-btn" onClick={() => void exportSoul()}>
            Export soul file
          </button>
          <button type="button" className="gb-btn" onClick={onImport}>
            Import over it
          </button>
        </div>
        <div className="gb-acts col">
          <button
            type="button"
            className={`gb-btn block${isArmed("reset") ? " armed" : ""}`}
            onClick={() => void reset()}
          >
            {isArmed("reset")
              ? "Tap again — its visual memories go too; a checkpoint is saved first"
              : "Reset to a fresh creature"}
          </button>
          <button
            type="button"
            className={`gb-btn block danger${isArmed("delete") ? " armed" : ""}`}
            onClick={() => void remove()}
          >
            {isArmed("delete")
              ? `Tap again — erases its memories and ${n} checkpoint${n === 1 ? "" : "s"}`
              : `Delete ${p.name}`}
          </button>
        </div>
        <p className="gb-hint dim gb-gap">
          Made {day(p.created_at)} from {p.made_from}.
        </p>
      </section>
    );
  }

  return (
    <div className="gb-page">
      <div className="gb-page-head">
        <button
          type="button"
          className="gb-page-back"
          aria-label="Back to personas"
          onClick={onBack}
        >
          <ChevronLeftIcon size={22} />
        </button>
        <span className="gb-page-ttl">
          <h2 className="gb-page-name" tabIndex={-1} ref={heading}>
            {p.name}
          </h2>
          <span className="gb-page-sub">{where(p, pl).text}</span>
        </span>
      </div>
      <GrowbotTabs
        base="gb-p"
        label={p.name}
        inner
        value={ptab}
        onPick={setPtab}
        tabs={[
          { id: "soul", label: "Soul" },
          { id: "vm", label: "Memories", count: vmCount },
          { id: "cps", label: "Checkpoints", count: n },
          { id: "manage", label: "Manage" },
        ]}
      />
      <main
        className="screen-body gb-screen"
        id="gb-p-panel"
        role="tabpanel"
        aria-labelledby={`gb-p-tab-${ptab}`}
      >
        {error && (
          <p className="gb-msg bad" role="alert">
            {error}
          </p>
        )}
        {!detail && !error && <p className="gb-hint dim">Loading {p.name}…</p>}
        {detail && ptab === "soul" && soulTab()}
        {ptab === "vm" && vmTab()}
        {ptab === "cps" && cpsTab()}
        {ptab === "manage" && manageTab()}
      </main>
      <div className="gb-foot">
        <button type="button" className="gb-btn primary" onClick={onOpen}>
          Open {p.name}…
        </button>
      </div>

      {editing && detail && (
        <GrowbotRegionEditor
          p={p}
          soul={soul}
          region={editing}
          robot={robot ? robotName(robot, pl) : null}
          openOn={p.phone ? onPhone(p.phone, pl) : null}
          onClose={() => setEditing(null)}
          onSaved={(out) => {
            setEditing(null);
            took(out);
            const cp = out.checkpoint;
            say(
              `Saved.${cp ? ` “${cp.name}” is in checkpoints if you want it back.` : ""}${live}`,
              cp ? { undo: undoTo(cp) } : undefined,
            );
          }}
        />
      )}

      {remembering && detail && (
        <GrowbotMemoryEditor
          p={p}
          soul={soul}
          which={remembering}
          openOn={p.phone ? onPhone(p.phone, pl) : null}
          onClose={() => setRemembering(null)}
          onSaved={(out) => {
            setRemembering(null);
            took(out);
            const cp = out.checkpoint;
            say(
              `Saved.${cp ? ` “${cp.name}” is in checkpoints if you want it back.` : ""}${live}`,
              cp ? { undo: undoTo(cp) } : undefined,
            );
          }}
        />
      )}

      {renaming && (
        <GrowbotRenameSheet
          p={p}
          me={me}
          onClose={() => setRenaming(false)}
          onRenamed={(out, old) => {
            setRenaming(false);
            took(out);
            say(`${old} is now ${out.persona.name} on this list.`);
          }}
        />
      )}

      {restoring && (
        <Dialog
          title={`Restore “${restoring.name}”?`}
          confirmLabel="Restore"
          tone="primary"
          onCancel={() => setRestoring(null)}
          onConfirm={() => void restore(restoring)}
        >
          {`${p.name} goes back to how it was on ${when(restoring.created_at)}, and gets back the visual memories it had then. Its soul and visual memories as they are now are saved as a checkpoint first, so this can be undone.${
            p.phone ? ` It reloads ${onPhone(p.phone, pl)}.` : ""
          }`}
        </Dialog>
      )}
    </div>
  );
}

interface EditorProps {
  p: GrowbotPersona;
  soul: GrowbotSoul;
  region: RegionInfo;
  robot: string | null;
  openOn: string | null;
  onClose: () => void;
  onSaved: (out: GrowbotWritten) => void;
}

/** One region's editor. Refusals land in place and say Nothing was saved; the box's own
 *  refusal (422) and a change made on its phone meanwhile (409) are already worded so. */
function GrowbotRegionEditor({ p, soul, region, robot, openOn, onClose, onSaved }: EditorProps) {
  const [text, setText] = useState(() => (region.kind === "meta" ? "" : regionText(soul, region)));
  // A soul file's meta is whatever its author wrote; only a string is a name.
  const [name, setName] = useState(asText(soul.meta?.name) || p.name);
  const [desc, setDesc] = useState(asText(soul.meta?.description));
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    let value: unknown;
    if (region.kind === "json") {
      try {
        value = JSON.parse(text);
      } catch {
        setErr("That isn't valid — check the brackets and quotes. Nothing was saved.");
        return;
      }
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        setErr("It has to be one { … } block. Nothing was saved.");
        return;
      }
    } else if (region.kind === "text") {
      if (!text.trim()) {
        setErr("It can't be empty — the creature would have no voice. Nothing was saved.");
        return;
      }
      value = text;
    } else {
      if (!name.trim()) {
        setErr("Give it a name. Nothing was saved.");
        return;
      }
      value = { name: name.trim(), description: desc.trim() };
    }
    setBusy(true);
    setErr("");
    try {
      onSaved(await api.editGrowbotRegion(p.id, region.id, value));
    } catch (e) {
      setErr(message(e));
      setBusy(false);
    }
  }

  return (
    <Sheet title={`${region.label} — ${p.name}`} onClose={onClose}>
      {region.kind === "meta" ? (
        <>
          <label className="gb-field">
            Name in the soul file
            <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="gb-field">
            Description
            <textarea rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} />
          </label>
          {err && (
            <p className="gb-msg bad" role="alert">
              {err}
            </p>
          )}
        </>
      ) : (
        <label className="gb-field">
          {region.what}
          <textarea
            className={region.kind === "json" ? "code" : ""}
            rows={region.kind === "json" ? 9 : 8}
            spellCheck={region.kind === "text"}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          {err && (
            <span className="gb-err" role="alert">
              {err}
            </span>
          )}
        </label>
      )}
      {region.robot && robot && <p className="gb-msg info">{region.robot(robot)}</p>}
      {region.floor && <Floor />}
      <p className="gb-hint dim gb-gap">
        Saving makes a checkpoint of {p.name} first, so you can undo it.
        {openOn ? ` It's open ${openOn}; it reloads with this the next time it's idle.` : ""}
      </p>
      <div className="gb-sheet-foot">
        <button
          type="button"
          className="gb-btn primary block"
          disabled={busy}
          onClick={() => void save()}
        >
          Save
        </button>
      </div>
    </Sheet>
  );
}

function GrowbotRenameSheet({
  p,
  me,
  onClose,
  onRenamed,
}: {
  p: GrowbotPersona;
  me: string;
  onClose: () => void;
  onRenamed: (out: GrowbotWritten, old: string) => void;
}) {
  const [name, setName] = useState(p.name);
  const [err, setErr] = useState("");
  const n = name.trim();
  async function go() {
    if (!n) return;
    try {
      onRenamed(await api.renameGrowbotPersona(p.id, n), p.name);
    } catch (e) {
      setErr(message(e));
    }
  }
  return (
    <Sheet title={`Rename ${p.name}`} onClose={onClose}>
      <label className="gb-field">
        Name on this list
        <input
          value={name}
          autoComplete="off"
          maxLength={80}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <p className="gb-hint dim">
        It still calls itself {me} until you tell it otherwise — that name is its own, and it learns
        a new one when you say so to it.
      </p>
      {err && (
        <p className="gb-msg bad" role="alert">
          {err}
        </p>
      )}
      <div className="gb-sheet-foot">
        <button
          type="button"
          className="gb-btn primary block"
          disabled={!n}
          onClick={() => void go()}
        >
          Rename
        </button>
      </div>
    </Sheet>
  );
}
