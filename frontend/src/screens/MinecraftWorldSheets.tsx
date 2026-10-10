// The Worlds sub-screen's modals: forms in the shared bottom Sheet, consequences in the
// shared center Dialog (DESIGN.md "Modal system"). One at a time: a Sheet that leads to a
// confirm hands over to the Dialog rather than stacking under it.

import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  type MinecraftResetMode,
  type MinecraftRuleValue,
  api,
  minecraftBackupFileUrl,
} from "../api/client";
import { Dialog } from "../components/Dialog";
import { Sheet } from "../components/Sheet";
import {
  AlertTriangleIcon,
  ArchiveIcon,
  DownloadIcon,
  FileIcon,
  InfoIcon,
  PinIcon,
  PlusIcon,
  RefreshIcon,
  TrashIcon,
  UndoIcon,
  UploadIcon,
} from "../components/icons";
import { listOf } from "../minecraft";
import {
  DEFAULT_RULES,
  DIFFICULTIES,
  GAMEMODES,
  LABEL_MAX,
  NAME_MAX,
  PINNED_DELETE_WHY,
  SEED_MAX,
  type SeedMode,
  autoReason,
  backupTitle,
  canDeleteBackup,
  defaultResetMode,
  deleteConfirm,
  fmtBytes,
  importCheck,
  importOverConfirm,
  isEmptySlot,
  loadConfirm,
  nextToGo,
  resetConfirm,
  resetOptions,
  restoreConfirm,
  restoreTargetText,
  seedFor,
  slotName,
  slotNumber,
  whenOf,
} from "../minecraftWorlds";
import { RulesEditor, Seg, type WorldModal, useW } from "./MinecraftWorlds";

function Field({
  id,
  label,
  children,
  hint,
}: {
  id: string;
  label: ReactNode;
  children: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <div className="mc-fld">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <span className="mc-note">{hint}</span>}
    </div>
  );
}

function Foot({ children }: { children: ReactNode }) {
  return <div className="mc-shfoot">{children}</div>;
}

/** Random (rolled by the box, shown, re-rollable) or typed, like Bedrock's own seed box. */
function SeedFields({
  mode,
  onMode,
  rolled,
  rollFailed,
  onReroll,
  typed,
  onTyped,
  hint,
}: {
  mode: SeedMode;
  onMode: (m: SeedMode) => void;
  rolled: string | null;
  rollFailed: boolean;
  onReroll: () => void;
  typed: string;
  onTyped: (t: string) => void;
  hint: string;
}) {
  return (
    <div className="mc-set">
      <div className="mc-set-l">Seed</div>
      <div className="mc-seg" role="radiogroup" aria-label="Seed">
        {(["random", "enter"] as const).map((m) => (
          <button
            type="button"
            // biome-ignore lint/a11y/useSemanticElements: a styled option card or segment; a native radio can't carry this 44px button layout.
            role="radio"
            key={m}
            aria-checked={mode === m}
            onClick={() => onMode(m)}
          >
            {m === "random" ? "Random" : "Enter a seed"}
          </button>
        ))}
      </div>
      {mode === "enter" ? (
        <>
          <input
            className="mc-inp mc-mono"
            aria-label="Seed"
            value={typed}
            maxLength={SEED_MAX}
            placeholder="any text or number"
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => onTyped(e.target.value.replace(/[\r\n]/g, ""))}
          />
          <span className="mc-note">
            <span className="mc-num">{typed.length}</span> of {SEED_MAX} — the same as
            Minecraft&apos;s own seed box. Text is turned into a number the way the game does it.
          </span>
        </>
      ) : (
        <>
          <div className="mc-inp-row">
            <span className="mc-inp mc-mono mc-seedbox" aria-live="polite">
              {rolled ?? (rollFailed ? "The box will pick one" : "rolling…")}
            </span>
            <button
              type="button"
              className="mc-btn"
              aria-label="Roll a new random seed"
              onClick={onReroll}
            >
              <RefreshIcon size={18} />
              Re-roll
            </button>
          </div>
          <span className="mc-note">{hint}</span>
        </>
      )}
    </div>
  );
}

/** The box's roll. If it can't be had, the form sends no seed and the box picks one when
 *  it creates the world, so the seed is still known. */
function useRolledSeed(): [string | null, () => void, boolean] {
  const [seed, setSeed] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const roll = () => {
    setSeed(null);
    setFailed(false);
    api
      .minecraftNewSeed()
      .then(setSeed)
      .catch(() => setFailed(true));
  };
  useEffect(roll, []);
  return [seed, roll, failed];
}

// ---- new world ----

function NewWorldSheet({ slot, onClose }: { slot: string; onClose: () => void }) {
  const w = useW();
  const [name, setName] = useState("");
  const [mode, setMode] = useState<SeedMode>("random");
  const [rolled, reroll, rollFailed] = useRolledSeed();
  const [typed, setTyped] = useState("");
  const [gamemode, setGamemode] = useState<string>("survival");
  const [difficulty, setDifficulty] = useState<string>("normal");
  const [cheats, setCheats] = useState(false);
  const [rules, setRules] = useState<Record<string, MinecraftRuleValue>>({});
  const values = { ...DEFAULT_RULES, ...rules };
  const changed = Object.keys(rules).length;
  const seed = seedFor(mode, rolled, typed);
  const ok = name.trim().length > 0 && (mode === "random" || seed !== null);

  function setRule(id: string, v: MinecraftRuleValue) {
    setRules((r) => {
      const { [id]: _, ...rest } = r;
      return v === DEFAULT_RULES[id] ? rest : { ...rest, [id]: v };
    });
  }

  return (
    <Sheet title="New world" onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        Goes into slot {slotNumber(slot)} · generated the first time it loads
      </p>
      <div className="mc-shbody">
        <Field id="mc-nw-name" label="Name">
          <input
            id="mc-nw-name"
            className="mc-inp"
            value={name}
            maxLength={NAME_MAX}
            placeholder="e.g. Skyblock run"
            autoComplete="off"
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <SeedFields
          mode={mode}
          onMode={setMode}
          rolled={rolled}
          rollFailed={rollFailed}
          onReroll={reroll}
          typed={typed}
          onTyped={setTyped}
          hint="Recorded with the world, so Reset can rebuild this exact terrain later."
        />
        <div className="mc-set">
          <div className="mc-set-l">Game mode</div>
          <Seg label="Game mode" options={GAMEMODES} value={gamemode} onPick={setGamemode} />
        </div>
        <div className="mc-set">
          <div className="mc-set-l">Difficulty</div>
          <Seg
            label="Difficulty"
            options={DIFFICULTIES}
            value={difficulty}
            four
            onPick={setDifficulty}
          />
        </div>
        <div className="mc-sw-row">
          <span className="mc-sw-l">
            <b>Cheats</b>
            <br />
            {cheats ? "on — operators can run commands" : "off — no commands in game"}
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={cheats}
            aria-label="Cheats"
            className={`mc-switch${cheats ? " on" : ""}`}
            onClick={() => setCheats(!cheats)}
          >
            <span className="mc-knob" />
          </button>
        </div>
        <details className="mc-rules-new">
          <summary>
            <span>World rules</span>
            <span className="mc-note">
              {changed ? `${changed} rule${changed === 1 ? "" : "s"} changed` : "defaults"}
            </span>
          </summary>
          <div className="mc-rules-in">
            <RulesEditor
              values={values}
              defaults={DEFAULT_RULES}
              pending={[]}
              note={
                <p className="mc-note">
                  Set before the world is generated. Leave them and it starts with the defaults
                  below.
                </p>
              }
              debounceMs={0}
              idPrefix="nw"
              onSet={setRule}
              onReset={(ids) =>
                setRules((r) =>
                  Object.fromEntries(Object.entries(r).filter(([k]) => !ids.includes(k))),
                )
              }
            />
          </div>
        </details>
        <Foot>
          <button
            type="button"
            className="mc-btn mc-btn-primary"
            disabled={!ok}
            onClick={() => {
              onClose();
              void w.actions.create(slot, {
                name: name.trim(),
                ...(seed ? { seed } : {}),
                gamemode,
                difficulty,
                cheats,
                ...(changed ? { rules } : {}),
              });
            }}
          >
            <PlusIcon size={18} />
            Create world
          </button>
        </Foot>
      </div>
    </Sheet>
  );
}

// ---- import ----

function ImportSheet({
  slot,
  fixed,
  onClose,
}: {
  slot: string | null;
  fixed: boolean;
  onClose: () => void;
}) {
  const w = useW();
  const [target, setTarget] = useState<string | null>(slot);
  const [file, setFile] = useState<File | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const t = target ? w.slotOf(target) : null;
  const check = file ? importCheck(file.size) : null;
  const occupied = !!t?.exists;

  function start() {
    if (!file || !t || check === "too_big") return;
    if (occupied) w.setModal({ kind: "importOver", slot: t.id, file });
    else {
      onClose();
      void w.actions.importWorld(t.id, file);
    }
  }

  return (
    <Sheet title="Import a world" onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        {fixed && t
          ? `Into ${isEmptySlot(t) ? `slot ${slotNumber(t.id)} (empty)` : slotName(t)}`
          : "A .mcworld exported from Minecraft"}
      </p>
      <div className="mc-shbody">
        <details className="mc-help">
          <summary>How to export from Windows</summary>
          <ol>
            <li>
              In Minecraft: <b>Play</b>, then the <b>pencil</b> (Edit) next to the world.
            </li>
            <li>
              Scroll to the bottom: <b>Export World</b>. It saves a <b>.mcworld</b> file.
            </li>
            <li>On the same PC, open JBrain and choose that file here.</li>
          </ol>
        </details>
        {file && (
          <div className="mc-file">
            <FileIcon size={18} />
            <span className="mc-fn">{file.name}</span>
            <span className="mc-fs mc-num">{fmtBytes(file.size)}</span>
          </div>
        )}
        {file && check === "too_big" && (
          <div className="mc-notice mc-notice-bad">
            <AlertTriangleIcon size={16} />
            <div>
              <b>Too big to import.</b> {file.name} is {fmtBytes(file.size)}; the limit is 1 GB.
              Nothing was uploaded.
            </div>
          </div>
        )}
        {file && check === "tunnel" && (
          <div className="mc-notice mc-notice-warn">
            <AlertTriangleIcon size={16} />
            <div>
              <b>Over 100 MB only uploads at home on the Wi-Fi.</b> Away from home it goes over the
              Cloudflare tunnel, which turns down files this big, so the upload fails there.
            </div>
          </div>
        )}
        <input
          ref={picker}
          type="file"
          accept=".mcworld,.zip,application/octet-stream"
          className="mc-sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <button type="button" className="mc-btn" onClick={() => picker.current?.click()}>
          <FileIcon size={18} />
          {file ? "Choose a different file" : "Choose .mcworld file"}
        </button>
        {!fixed && (
          <div className="mc-set">
            <div className="mc-set-l">Into</div>
            <div className="mc-opts" role="radiogroup" aria-label="Slot">
              {w.slots.map((s) => (
                <button
                  type="button"
                  // biome-ignore lint/a11y/useSemanticElements: a styled option card or segment; a native radio can't carry this 44px button layout.
                  role="radio"
                  key={s.id}
                  className="mc-opt"
                  aria-checked={target === s.id}
                  onClick={() => setTarget(s.id)}
                >
                  <span className="mc-rad" />
                  <span className="mc-omin">
                    <span className="mc-ot">
                      {slotName(s)}
                      {s.active ? " · loaded" : ""}
                    </span>
                    <span className="mc-os">
                      {isEmptySlot(s)
                        ? "empty slot"
                        : `replaces ${slotName(s)} — ${s.exists ? "backed up first" : "not generated, nothing to lose"}${s.active && w.running ? "; the server restarts around it" : ""}`}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
        {occupied && t && (
          <div className="mc-notice mc-notice-info">
            <InfoIcon size={16} />
            <div>
              <b>Overwriting takes a backup first.</b> {slotName(t)} is backed up before the import,
              so you can bring it back from its backups.
            </div>
          </div>
        )}
        <p className="mc-note">
          <b>Settings come from the world file</b> — its game mode, difficulty, cheats, seed and
          rules.{t && !isEmptySlot(t) ? ` ${slotName(t)}'s current settings aren't kept.` : ""}
        </p>
        <p className="mc-note">
          Checked before anything is written: it must hold a level.dat and be under 1 GB. A bad file
          never touches a slot.
        </p>
        <Foot>
          <button
            type="button"
            className="mc-btn mc-btn-primary"
            disabled={!file || !t || check === "too_big" || !!w.blocked}
            onClick={start}
          >
            <UploadIcon size={18} />
            Import{t ? ` into ${isEmptySlot(t) ? `slot ${slotNumber(t.id)}` : slotName(t)}` : ""}
          </button>
        </Foot>
      </div>
    </Sheet>
  );
}

// ---- rename ----

function RenameSheet({ slot, onClose }: { slot: string; onClose: () => void }) {
  const w = useW();
  const s = w.slotOf(slot);
  const [name, setName] = useState(s?.name ?? "");
  if (!s) return null;
  const n = name.trim();
  return (
    <Sheet title="Rename world" onClose={onClose}>
      <p className="mc-note mc-sheet-sub">Slot {slotNumber(slot)} · only the name changes</p>
      <div className="mc-shbody">
        <Field
          id="mc-rn-name"
          label="Name"
          hint={
            <>
              <span className="mc-num">{name.length}</span> of {NAME_MAX}
            </>
          }
        >
          <input
            id="mc-rn-name"
            className="mc-inp"
            value={name}
            maxLength={NAME_MAX}
            autoComplete="off"
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Foot>
          <button type="button" className="mc-btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="mc-btn mc-btn-primary"
            disabled={!n || n === s.name}
            onClick={() => {
              onClose();
              void w.actions.rename(slot, n);
            }}
          >
            Save name
          </button>
        </Foot>
      </div>
    </Sheet>
  );
}

// ---- reset ----

function ResetSheet({ slot, onClose }: { slot: string; onClose: () => void }) {
  const w = useW();
  const s = w.slotOf(slot);
  const [how, setHow] = useState<MinecraftResetMode>(s ? defaultResetMode(s) : "new_seed");
  const [mode, setMode] = useState<SeedMode>("random");
  const [rolled, reroll, rollFailed] = useRolledSeed();
  const [typed, setTyped] = useState("");
  if (!s) return null;
  const seed = how === "new_seed" ? seedFor(mode, rolled, typed) : null;
  const ready = how !== "new_seed" || mode === "random" || seed !== null;
  return (
    <Sheet title={`Reset ${slotName(s)}`} onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        {s.exists
          ? "Backed up first — undo it from the backups"
          : "Not generated yet — nothing to back up"}
      </p>
      <div className="mc-shbody">
        <div className="mc-opts" role="radiogroup" aria-label="Reset to">
          {resetOptions(s).map((o) => (
            <button
              type="button"
              // biome-ignore lint/a11y/useSemanticElements: a styled option card or segment; a native radio can't carry this 44px button layout.
              role="radio"
              key={o.mode}
              className={`mc-opt${o.mode === "empty" ? " danger" : ""}`}
              aria-checked={how === o.mode}
              disabled={o.disabled}
              onClick={() => setHow(o.mode)}
            >
              <span className="mc-rad" />
              <span className="mc-omin">
                <span className="mc-ot">{o.title}</span>
                <span className="mc-os">{o.sub}</span>
              </span>
            </button>
          ))}
        </div>
        {how === "new_seed" && (
          <SeedFields
            mode={mode}
            onMode={setMode}
            rolled={rolled}
            rollFailed={rollFailed}
            onReroll={reroll}
            typed={typed}
            onTyped={setTyped}
            hint="Or enter one — leave it to chance and this number is used."
          />
        )}
        {s.active && w.running && (
          <p className="mc-note">
            It&apos;s loaded, so the server stops, resets it and starts again
            {w.online.length
              ? ` — ${listOf(w.online)} get a 10-second warning in chat and are disconnected until it's back up`
              : ""}
            .
          </p>
        )}
        <Foot>
          <button type="button" className="mc-btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="mc-btn mc-btn-danger"
            disabled={!ready}
            onClick={() =>
              w.setModal({
                kind: "resetConfirm",
                slot,
                mode: how,
                ...(seed ? { seed } : {}),
              })
            }
          >
            Continue
          </button>
        </Foot>
      </div>
    </Sheet>
  );
}

function ResetDialog({
  slot,
  mode,
  seed,
  onClose,
}: {
  slot: string;
  mode: MinecraftResetMode;
  seed: string | undefined;
  onClose: () => void;
}) {
  const w = useW();
  const s = w.slotOf(slot);
  const [typed, setTyped] = useState("");
  if (!s) return null;
  const spec = resetConfirm(s, mode, { running: w.running, online: w.online });
  const name = slotName(s);
  return (
    <Dialog
      title={spec.title}
      confirmLabel={spec.confirmLabel}
      tone={spec.tone}
      confirmDisabled={typed.trim() !== name}
      onCancel={onClose}
      onConfirm={() => {
        onClose();
        void w.actions.reset(slot, mode, seed);
      }}
      extra={
        <div className="mc-fld mc-dlg-fld">
          <label htmlFor="mc-rs-typed">
            Type <b>{name}</b> to confirm
          </label>
          <input
            id="mc-rs-typed"
            className="mc-inp"
            value={typed}
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            onChange={(e) => setTyped(e.target.value)}
          />
        </div>
      }
    >
      {spec.body}
    </Dialog>
  );
}

// ---- backups ----

function BackupNowSheet({ slot, onClose }: { slot: string; onClose: () => void }) {
  const w = useW();
  const s = w.slotOf(slot);
  const [label, setLabel] = useState("");
  if (!s) return null;
  const keep = w.worlds?.keep_per_slot ?? 20;
  const goes = nextToGo(w.backups[slot] ?? [], keep);
  return (
    <Sheet title={`Back up ${slotName(s)}`} onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        {w.running && s.active
          ? "Copied live — players stay on"
          : "A straight copy — nothing else changes"}
      </p>
      <div className="mc-shbody">
        <Field
          id="mc-bk-label"
          label={
            <>
              Label <span className="mc-faint">(optional)</span>
            </>
          }
        >
          <input
            id="mc-bk-label"
            className="mc-inp"
            value={label}
            maxLength={LABEL_MAX}
            placeholder="e.g. before the castle"
            autoComplete="off"
            onChange={(e) => setLabel(e.target.value)}
          />
        </Field>
        {goes && (
          <div className="mc-notice mc-notice-warn">
            <AlertTriangleIcon size={16} />
            <div>
              {slotName(s)} already keeps {keep}, so this removes <b>{backupTitle(goes)}</b> from{" "}
              {whenOf(goes.created, w.nowMs)}. Pin it first to keep it.
            </div>
          </div>
        )}
        <Foot>
          <button
            type="button"
            className="mc-btn mc-btn-primary"
            onClick={() => {
              onClose();
              void w.actions.backUp(slot, label.trim());
            }}
          >
            <ArchiveIcon size={18} />
            Back up now
          </button>
        </Foot>
      </div>
    </Sheet>
  );
}

function BackupSheet({
  name,
  slot,
  onClose,
}: {
  name: string;
  slot: string;
  onClose: () => void;
}) {
  const w = useW();
  const b = (w.backups[slot] ?? []).find((x) => x.name === name);
  const s = w.slotOf(slot);
  if (!b) return null;
  const keep = w.worlds?.keep_per_slot ?? 20;
  return (
    <Sheet title={backupTitle(b)} onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        {s ? slotName(s) : b.folder} · {whenOf(b.created, w.nowMs)}
      </p>
      <div className="mc-shbody">
        <dl className="mc-facts-dl">
          <dt>Kind</dt>
          <dd>{b.auto ? `automatic — ${autoReason(b.label)}` : "yours"}</dd>
          <dt>Size</dt>
          <dd className="mc-num">{fmtBytes(b.bytes)}</dd>
          <dt>Kept</dt>
          <dd>
            {b.pinned
              ? "pinned — kept for good"
              : `one of the newest ${keep}${b.auto ? " (automatic ones go first)" : ""}`}
          </dd>
          <dt>Off the box</dt>
          <dd>
            {b.downloaded_at ? `downloaded ${whenOf(b.downloaded_at, w.nowMs)}` : "not downloaded"}
          </dd>
        </dl>
        <button
          type="button"
          className="mc-btn mc-btn-primary"
          disabled={!!w.blocked}
          onClick={() => w.setModal({ kind: "restore", name, slot })}
        >
          <UndoIcon size={18} />
          Restore…
        </button>
        {w.blocked && <p className="mc-note">{w.blocked}</p>}
        <div className="mc-wactions">
          <a
            className="mc-btn"
            href={minecraftBackupFileUrl(b.name)}
            download={b.name}
            onClick={() => w.actions.downloaded(b, slot)}
          >
            <DownloadIcon size={18} />
            Download
          </a>
          <button
            type="button"
            className="mc-btn"
            aria-pressed={b.pinned}
            onClick={() => void w.actions.pin(b, slot)}
          >
            <PinIcon size={18} />
            {b.pinned ? "Unpin" : "Pin"}
          </button>
          <button
            type="button"
            className="mc-btn mc-btn-danger mc-full"
            disabled={!canDeleteBackup(b)}
            onClick={() => w.setModal({ kind: "delete", name, slot })}
          >
            <TrashIcon size={18} />
            Delete
          </button>
        </div>
        {!canDeleteBackup(b) && <p className="mc-note">{PINNED_DELETE_WHY}</p>}
        <p className="mc-note">
          A download is a .mcworld — double-click it on Windows to open it in Minecraft.
        </p>
      </div>
    </Sheet>
  );
}

function RestoreSheet({
  name,
  slot,
  onClose,
}: {
  name: string;
  slot: string;
  onClose: () => void;
}) {
  const w = useW();
  const b = (w.backups[slot] ?? []).find((x) => x.name === name);
  const [to, setTo] = useState(slot);
  if (!b) return null;
  const source = w.slots.find((s) => s.folder === b.folder) ?? null;
  const ctx = { running: w.running, online: w.online };
  return (
    <Sheet title="Restore a backup" onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        {backupTitle(b)} · {whenOf(b.created, w.nowMs)}
      </p>
      <div className="mc-shbody">
        <div className="mc-set">
          <div className="mc-set-l">Restore into</div>
          <div className="mc-opts" role="radiogroup" aria-label="Restore into">
            {w.slots.map((s) => (
              <button
                type="button"
                // biome-ignore lint/a11y/useSemanticElements: a styled option card or segment; a native radio can't carry this 44px button layout.
                role="radio"
                key={s.id}
                className="mc-opt"
                aria-checked={to === s.id}
                onClick={() => setTo(s.id)}
              >
                <span className="mc-rad" />
                <span className="mc-omin">
                  <span className="mc-ot">
                    {slotName(s)}
                    {s.folder === b.folder ? " · same world" : ""}
                    {s.active ? " · loaded" : ""}
                  </span>
                  <span className="mc-os">{restoreTargetText(b, s, source, ctx)}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
        <Foot>
          <button type="button" className="mc-btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="mc-btn mc-btn-warn"
            onClick={() => w.setModal({ kind: "restoreConfirm", name, slot, to })}
          >
            Continue
          </button>
        </Foot>
      </div>
    </Sheet>
  );
}

// ---- the one place a world modal is chosen ----

export function WorldModals() {
  const w = useW();
  const m: WorldModal | null = w.modal;
  if (!m) return null;
  const close = () => w.setModal(null);
  const ctx = { running: w.running, online: w.online };
  switch (m.kind) {
    case "newWorld":
      return <NewWorldSheet slot={m.slot} onClose={close} />;
    case "import":
      return <ImportSheet slot={m.slot} fixed={m.fixed} onClose={close} />;
    case "rename":
      return <RenameSheet slot={m.slot} onClose={close} />;
    case "reset":
      return <ResetSheet slot={m.slot} onClose={close} />;
    case "resetConfirm":
      return <ResetDialog slot={m.slot} mode={m.mode} seed={m.seed} onClose={close} />;
    case "backupNow":
      return <BackupNowSheet slot={m.slot} onClose={close} />;
    case "backup":
      return <BackupSheet name={m.name} slot={m.slot} onClose={close} />;
    case "restore":
      return <RestoreSheet name={m.name} slot={m.slot} onClose={close} />;
    default:
      break;
  }
  let spec: ReturnType<typeof loadConfirm> | null = null;
  let act: () => void = () => {};
  if (m.kind === "load") {
    const t = w.slotOf(m.slot);
    if (t) {
      spec = loadConfirm(t, w.active, ctx);
      act = () => void w.actions.load(m.slot);
    }
  } else if (m.kind === "importOver") {
    const t = w.slotOf(m.slot);
    if (t) {
      spec = importOverConfirm(t, m.file.name, ctx);
      act = () => void w.actions.importWorld(m.slot, m.file);
    }
  } else if (m.kind === "restoreConfirm") {
    const b = (w.backups[m.slot] ?? []).find((x) => x.name === m.name);
    const t = w.slotOf(m.to);
    if (b && t) {
      spec = restoreConfirm(b, t, ctx, w.nowMs);
      act = () => void w.actions.restore(b, m.to);
    }
  } else if (m.kind === "delete") {
    const b = (w.backups[m.slot] ?? []).find((x) => x.name === m.name);
    if (b) {
      spec = deleteConfirm(b);
      act = () => void w.actions.remove(b, m.slot);
    }
  }
  if (!spec) return null;
  return (
    <Dialog
      key={m.kind}
      title={spec.title}
      confirmLabel={spec.confirmLabel}
      tone={spec.tone}
      onCancel={close}
      onConfirm={() => {
        close();
        act();
      }}
    >
      {spec.body}
    </Dialog>
  );
}
