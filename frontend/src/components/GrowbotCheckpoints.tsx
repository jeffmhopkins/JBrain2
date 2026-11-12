/** A persona's Checkpoints tab (DESIGN.md "A persona's page", binding mock
 *  docs/mocks/growbot-checkpoints/b-by-day.html): the Chats panel's look, grouped by day.
 *  A dashed *Save a checkpoint* row opens the naming sheet; each day is a card of compact rows
 *  (a filled dot for a named checkpoint, a hollow one for an automatic one), and a row swipes
 *  left for restore · new persona · delete, or opens its own sheet when tapped, which holds
 *  the same three so nothing lives only behind the gesture. */
import { useState } from "react";
import type { GrowbotCheckpoint } from "../api/client";
import { byDay, clock, memoriesCount, when } from "../growbot";
import { Sheet } from "./Sheet";
import { SwipeRow } from "./SwipeRow";
import { ChevronRightIcon, PlusIcon, TrashIcon, UndoIcon } from "./icons";

interface Props {
  name: string;
  cps: GrowbotCheckpoint[];
  loaded: boolean;
  isArmed: (key: string) => boolean;
  arm: (key: string) => boolean;
  onSave: (name: string) => Promise<boolean>;
  onRestore: (cp: GrowbotCheckpoint) => void;
  onSeed: (cp: GrowbotCheckpoint) => void;
  onDelete: (cp: GrowbotCheckpoint) => void;
  onDeleteAutomatic: () => void;
}

export function GrowbotCheckpoints({
  name,
  cps,
  loaded,
  isArmed,
  arm,
  onSave,
  onRestore,
  onSeed,
  onDelete,
  onDeleteAutomatic,
}: Props) {
  const [railId, setRailId] = useState<string | null>(null);
  const [opened, setOpened] = useState<GrowbotCheckpoint | null>(null);
  const [saving, setSaving] = useState(false);
  const autos = cps.filter((c) => c.auto).length;

  // The rail and the sheet arm their own keys, so a delete armed on the rail and then backed
  // out of never leaves the sheet's button one tap from deleting.
  function del(cp: GrowbotCheckpoint, where: "rail" | "sheet") {
    if (!arm(`cp-${where}:${cp.id}`)) return;
    setRailId(null);
    setOpened(null);
    onDelete(cp);
  }
  function act(fn: (cp: GrowbotCheckpoint) => void, cp: GrowbotCheckpoint) {
    setRailId(null);
    setOpened(null);
    fn(cp);
  }

  return (
    <>
      <button type="button" className="gb-new-row" onClick={() => setSaving(true)}>
        <PlusIcon size={16} />
        Save a checkpoint
      </button>
      {cps.length === 0 && loaded && (
        <p className="gb-hint dim">
          No checkpoints yet. One is made on its own before every edit, restore and reset.
        </p>
      )}
      {byDay(cps).map((g) => (
        <section key={g.heading} className="gb-cp-day" aria-label={g.heading}>
          <div className="gb-sec-h">
            {g.heading}
            <span className="r">{g.rows.length}</span>
          </div>
          <div className="gb-cp-group">
            {g.rows.map((c) => (
              <SwipeRow
                key={c.id}
                className={c.auto ? "auto" : ""}
                open={railId === c.id}
                onOpenChange={(o) => setRailId(o ? c.id : null)}
                onTap={() => setOpened(c)}
                actions={[
                  {
                    key: "restore",
                    label: "restore",
                    tone: "restore",
                    icon: <UndoIcon size={18} />,
                    onClick: () => act(onRestore, c),
                  },
                  {
                    key: "seed",
                    label: "new persona",
                    tone: "seed",
                    icon: <PlusIcon size={18} />,
                    onClick: () => act(onSeed, c),
                  },
                  {
                    key: "delete",
                    label: "delete",
                    tone: "delete",
                    icon: <TrashIcon size={18} />,
                    armed: isArmed(`cp-rail:${c.id}`),
                    armedLabel: "tap again to delete",
                    onClick: () => del(c, "rail"),
                  },
                ]}
              >
                <span
                  className={`gb-cp-dot${c.auto ? " auto" : ""}`}
                  title={c.auto ? "automatic" : "named"}
                />
                <span className="gb-cp-name">
                  {c.name}
                  <span className="gb-sr-only">{c.auto ? ", automatic" : ""}</span>
                </span>
                <span className="gb-cp-meta">
                  {c.memories > 0 && <span className="vm">{c.memories} vm</span>}
                  <span>{clock(c.created_at)}</span>
                </span>
                <ChevronRightIcon size={15} />
              </SwipeRow>
            ))}
          </div>
        </section>
      ))}
      {autos > 0 && (
        <button
          type="button"
          className={`gb-btn block danger${isArmed("cp:auto") ? " armed" : ""}`}
          onClick={() => {
            if (arm("cp:auto")) onDeleteAutomatic();
          }}
        >
          {isArmed("cp:auto")
            ? `Tap again — deletes ${autos}, keeps the named ones`
            : `Delete the ${autos} automatic one${autos === 1 ? "" : "s"}`}
        </button>
      )}
      {cps.length > 0 && (
        <p className="gb-hint dim">
          Swipe a row left to restore, start a new persona or delete, or tap it. Filled dot: one you
          named. Hollow: made on its own before a change; the 20 newest are kept.
        </p>
      )}

      {saving && (
        <SaveSheet
          name={name}
          onClose={() => setSaving(false)}
          onSave={async (n) => {
            if (await onSave(n)) setSaving(false);
          }}
        />
      )}

      {opened && (
        <Sheet title={opened.name} onClose={() => setOpened(null)}>
          <p className="gb-pv">
            {when(opened.created_at)}
            {opened.auto ? ` · automatic, ${opened.why || "made before a change"}` : " · named"}
            <br />
            Keeps its whole soul
            {opened.memories > 0
              ? ` and ${memoriesCount(opened.memories)}.`
              : " and no visual memories."}
          </p>
          <div className="gb-acts col first">
            <button
              type="button"
              className="gb-btn primary block"
              onClick={() => act(onRestore, opened)}
            >
              Restore {name} to this
            </button>
            <button type="button" className="gb-btn block" onClick={() => act(onSeed, opened)}>
              New persona from it
            </button>
            <button
              type="button"
              className={`gb-btn block danger${isArmed(`cp-sheet:${opened.id}`) ? " armed" : ""}`}
              onClick={() => del(opened, "sheet")}
            >
              {isArmed(`cp-sheet:${opened.id}`)
                ? "Tap again — this can't be undone"
                : "Delete checkpoint"}
            </button>
          </div>
        </Sheet>
      )}
    </>
  );
}

function SaveSheet({
  name,
  onClose,
  onSave,
}: {
  name: string;
  onClose: () => void;
  onSave: (name: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const n = draft.trim();
  async function go() {
    if (!n || busy) return;
    setBusy(true);
    await onSave(n);
    setBusy(false);
  }
  return (
    <Sheet title="Save a checkpoint" onClose={onClose}>
      <label className="gb-field">
        Name it
        <input
          value={draft}
          placeholder="e.g. Before the trip"
          autoComplete="off"
          maxLength={80}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void go();
          }}
        />
      </label>
      <p className="gb-hint dim">
        A copy of {name}'s whole soul and visual memories as they are now, kept on the box until you
        delete it.
      </p>
      <div className="gb-sheet-foot">
        <button
          type="button"
          className="gb-btn primary block"
          disabled={!n || busy}
          onClick={() => void go()}
        >
          Save
        </button>
      </div>
    </Sheet>
  );
}
