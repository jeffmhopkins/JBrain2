// The Worlds sub-screen of the Minecraft screen (DESIGN.md "Minecraft server screen" →
// "Worlds and backups"; binding mock docs/mocks/minecraft-worlds/b-worlds-subscreen.html).
// One row on the main screen pushes Worlds; each world pushes its own page with its
// backups; Game rules is a page of its own. Forms open in the shared Sheet, consequences
// confirm in the shared Dialog (MinecraftWorldSheets.tsx).
//
// Jobs show only what /status reports (what, phase, started_at). The one thing this device
// adds is an upload's real byte count, which no other device can know.

import {
  type ReactNode,
  type TouchEvent,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  type MinecraftBackup,
  type MinecraftJob,
  type MinecraftNewWorld,
  type MinecraftResetMode,
  type MinecraftRuleValue,
  type MinecraftRules,
  type MinecraftSlot,
  type MinecraftWorlds,
  api,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import {
  AlertTriangleIcon,
  ArchiveIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  CubeIcon,
  DownloadIcon,
  InfoIcon,
  LayersIcon,
  MinusIcon,
  PencilIcon,
  PinIcon,
  PlayIcon,
  PlusIcon,
  RefreshIcon,
  SlidersIcon,
  UndoIcon,
  UploadIcon,
  XIcon,
} from "../components/icons";
import { clockOf, plural, shortDate } from "../minecraft";
import {
  DIFFICULTIES,
  GAMEMODES,
  PHASE_TEXT,
  RULES,
  type RuleDef,
  backupShort,
  backupTitle,
  cap,
  changedRules,
  choiceOptions,
  clampRule,
  elapsedOf,
  fmtBytes,
  groupsFor,
  isEmptySlot,
  jobLine,
  nextToGo,
  notGenerated,
  originText,
  pendWhen,
  playedText,
  ruleSavedToast,
  ruleWord,
  rulesNote,
  settingsLine,
  slotName,
  slotNumber,
  whenOf,
  worldBlocked,
} from "../minecraftWorlds";

/** One save per settled stepper: the value moves at once, the PUT goes out when tapping or
 *  typing stops, so four taps are one /gamerule and one toast. */
export const RULE_DEBOUNCE_MS = 700;
const SWIPE_DOWN_PX = 56;

export type Page =
  | { kind: "worlds" }
  | { kind: "world"; slot: string }
  | { kind: "rules"; slot: string };

export type WorldModal =
  | { kind: "newWorld"; slot: string }
  | { kind: "import"; slot: string | null; fixed: boolean }
  | { kind: "importOver"; slot: string; file: File }
  | { kind: "rename"; slot: string }
  | { kind: "reset"; slot: string }
  | { kind: "resetConfirm"; slot: string; mode: MinecraftResetMode; seed?: string }
  | { kind: "load"; slot: string }
  | { kind: "backupNow"; slot: string }
  | { kind: "backup"; name: string; slot: string }
  | { kind: "restore"; name: string; slot: string }
  | { kind: "restoreConfirm"; name: string; slot: string; to: string }
  | { kind: "delete"; name: string; slot: string };

export interface Upload {
  slot: string;
  file: string;
  sent: number;
  total: number;
}

type SettingKey = "gamemode" | "difficulty" | "cheats";

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

/** Everything the Worlds pages and their modals share, owned by the Minecraft screen. */
export function useWorlds({
  serverUp,
  running,
  online,
  job,
  updating,
  state,
  nowMs,
  toast,
  refreshStatus,
}: {
  serverUp: boolean;
  running: boolean;
  online: string[];
  job: MinecraftJob | null;
  updating: boolean;
  state: string;
  nowMs: number;
  toast: (msg: string) => void;
  refreshStatus: () => void;
}) {
  const [worlds, setWorlds] = useState<MinecraftWorlds | null>(null);
  const [backups, setBackups] = useState<Record<string, MinecraftBackup[]>>({});
  const [pages, setPages] = useState<Page[]>([]);
  const [modal, setModal] = useState<WorldModal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [uploadFailed, setUploadFailed] = useState<{ slot: string; detail: string } | null>(null);
  const [backingUp, setBackingUp] = useState<{ slot: string; label: string } | null>(null);
  // Settings saved this session that the server hasn't applied yet. The API reports pending
  // only for rules, so these are this device's knowledge, cleared when a start applies them.
  const [pendingSettings, setPendingSettings] = useState<Record<string, SettingKey[]>>({});
  const [rev, setRev] = useState(0);

  const reload = useCallback(async () => {
    try {
      setWorlds(await api.minecraftWorlds());
    } catch {
      // The row and pages keep the last list; the status block already says why.
    }
    setRev((r) => r + 1);
  }, []);

  const loadBackups = useCallback(async (slot: string) => {
    try {
      const list = await api.minecraftBackups(slot);
      setBackups((b) => ({ ...b, [slot]: list }));
    } catch {
      // keep the last list
    }
  }, []);

  useEffect(() => {
    if (serverUp) void reload();
  }, [serverUp, reload]);

  // A job's every step can change a slot (a backup taken, a world written), so re-read.
  const jobKey = job ? `${job.what}|${job.phase ?? ""}` : "";
  const lastJob = useRef(jobKey);
  useEffect(() => {
    if (lastJob.current !== jobKey) void reload();
    lastJob.current = jobKey;
  }, [jobKey, reload]);

  // A start applies the loaded world's saved settings.
  const wasRunning = useRef(running);
  useEffect(() => {
    const active = worlds?.slots.find((s) => s.active)?.id;
    if (running && !wasRunning.current && active) {
      setPendingSettings((p) => ({ ...p, [active]: [] }));
    }
    wasRunning.current = running;
  }, [running, worlds]);

  const slots = worlds?.slots ?? [];
  const slotOf = (id: string) => slots.find((s) => s.id === id) ?? null;
  const active = slots.find((s) => s.active) ?? null;
  const blocked =
    worldBlocked(job, updating, state) ||
    (upload ? "Wait — a world is uploading from this device." : "");

  async function run(verb: string, fn: () => Promise<void>): Promise<boolean> {
    setError(null);
    try {
      await fn();
      return true;
    } catch (err) {
      setError(`Couldn't ${verb} — ${message(err)}`);
      return false;
    } finally {
      void reload();
      refreshStatus();
    }
  }

  const actions = {
    async load(slot: string) {
      const t = slotOf(slot);
      if (!t) return;
      const was = active ? slotName(active) : null;
      const ok = await run(`load ${slotName(t)}`, async () => {
        const res = await api.minecraftLoadWorld(slot);
        if (!res.loaded) toast(`${slotName(t)} is already loaded`);
        else if (running) toast(`${slotName(t)} is loaded — players can join`);
        else toast(`${slotName(t)} is loaded — start the server to play it`);
      });
      if (!ok && was) setError((e) => `${e}. Nothing changed; ${was} is still loaded.`);
    },
    async create(slot: string, body: MinecraftNewWorld) {
      if (
        await run("create the world", async () => {
          await api.minecraftCreateWorld(slot, body);
        })
      ) {
        toast(
          `${body.name} is in slot ${slotNumber(slot)} — it's generated the first time you load it`,
        );
      }
    },
    async rename(slot: string, name: string) {
      const t = slotOf(slot);
      if (
        await run("rename it", async () => {
          await api.minecraftUpdateWorld(slot, { name });
        })
      ) {
        toast(`Renamed ${t ? slotName(t) : "it"} to ${name}`);
      }
    },
    async setting(slot: string, key: SettingKey, value: string | boolean) {
      const t = slotOf(slot);
      if (!t) return;
      // Optimistic: the segment moves at once; the reload after the PATCH settles it.
      setWorlds((w) =>
        w ? { ...w, slots: w.slots.map((s) => (s.id === slot ? { ...s, [key]: value } : s)) } : w,
      );
      const ok = await run("save the setting", async () => {
        await api.minecraftUpdateWorld(slot, { [key]: value });
      });
      if (!ok) return;
      const live = t.active && running;
      if (key === "difficulty" && live) {
        toast(`Applied live — difficulty is ${value} now`);
        return;
      }
      setPendingSettings((p) => ({
        ...p,
        [slot]: [...new Set([...(p[slot] ?? []), key])],
      }));
      const when = pendWhen(t, running);
      if (key === "gamemode") {
        toast(
          `Saved — ${value} is the default for new players ${when}; existing players keep theirs`,
        );
      } else if (key === "cheats") toast(`Saved — cheats ${value ? "on" : "off"} ${when}`);
      else toast(`Saved — difficulty changes ${when}`);
    },
    async reset(slot: string, mode: MinecraftResetMode, seed?: string) {
      const t = slotOf(slot);
      const name = t ? slotName(t) : "It";
      if (
        await run(`reset ${name}`, async () => {
          await api.minecraftResetWorld(slot, mode, seed);
        })
      ) {
        toast(
          mode === "empty"
            ? `Slot ${slotNumber(slot)} is empty — its backups are kept`
            : `${name} is reset — undo it from its backups`,
        );
        void loadBackups(slot);
      }
    },
    async importWorld(slot: string, file: File) {
      setError(null);
      setUploadFailed(null);
      setUpload({ slot, file: file.name, sent: 0, total: file.size });
      refreshStatus();
      try {
        const landed = await api.minecraftImportWorld(slot, file, "", (sent, total) =>
          setUpload({ slot, file: file.name, sent, total }),
        );
        toast(`${landed?.name ?? "The world"} imported into slot ${slotNumber(slot)}`);
      } catch (err) {
        // A file the box refused (400) is the card's own failure; a 409 is a busy server.
        if (err instanceof ApiError && err.status === 400) {
          setUploadFailed({ slot, detail: err.message });
        } else setError(`Couldn't import ${file.name} — ${message(err)}`);
      } finally {
        setUpload(null);
        void reload();
        void loadBackups(slot);
        refreshStatus();
      }
    },
    async backUp(slot: string, label: string) {
      const t = slotOf(slot);
      const goes = nextToGo(backups[slot] ?? [], worlds?.keep_per_slot ?? 20);
      setBackingUp({ slot, label });
      const ok = await run("back up", async () => {
        await api.minecraftBackUp(slot, label);
      });
      setBackingUp(null);
      if (ok) {
        toast(
          `Backed up ${t ? slotName(t) : "the world"}${label ? ` — “${label}”` : ""}${goes ? ` · removed ${backupShort(goes)} (over ${worlds?.keep_per_slot ?? 20})` : ""}`,
        );
      }
      void loadBackups(slot);
    },
    async pin(b: MinecraftBackup, slot: string) {
      const next = !b.pinned;
      setBackups((all) => ({
        ...all,
        [slot]: (all[slot] ?? []).map((x) => (x.name === b.name ? { ...x, pinned: next } : x)),
      }));
      if (
        await run(next ? "pin it" : "unpin it", async () => {
          await api.minecraftPinBackup(b.name, next);
        })
      ) {
        toast(
          next
            ? "Pinned — kept for good, outside the 20"
            : "Unpinned — it counts toward the 20 again",
        );
      }
      void loadBackups(slot);
    },
    async remove(b: MinecraftBackup, slot: string) {
      if (
        await run("delete it", async () => {
          await api.minecraftDeleteBackup(b.name);
        })
      ) {
        toast(`Deleted ${backupShort(b)}`);
      }
      void loadBackups(slot);
    },
    async restore(b: MinecraftBackup, to: string) {
      const t = slotOf(to);
      if (
        await run("restore it", async () => {
          const landed = await api.minecraftRestoreBackup(b.name, to);
          const into = landed?.name ?? (t ? slotName(t) : "The world");
          toast(`Restored — ${into} is back to ${whenOf(b.created, nowMs)}`);
        })
      ) {
        void loadBackups(to);
      }
    },
    downloaded(b: MinecraftBackup, slot: string) {
      toast(`Downloading ${backupTitle(b)} (${fmtBytes(b.bytes)})`);
      // The box stamps the download as it streams, so read it back once it's under way.
      setTimeout(() => {
        void loadBackups(slot);
        void reload();
      }, 3000);
    },
  };

  const pop = useCallback(() => setPages((p) => p.slice(0, -1)), []);

  return {
    worlds,
    slots,
    active,
    slotOf,
    backups,
    loadBackups,
    pages,
    push: (p: Page) => {
      if (p.kind === "worlds") void reload();
      setPages((s) => [...s, p]);
    },
    pop,
    modal,
    setModal,
    error,
    setError,
    upload,
    uploadFailed,
    dismissUploadFailed: () => setUploadFailed(null),
    backingUp,
    pendingSettings,
    blocked,
    rev,
    reload,
    running,
    online,
    job,
    nowMs,
    toast,
    actions,
  };
}

export type Worlds = ReturnType<typeof useWorlds>;

const Ctx = createContext<Worlds | null>(null);
export const WorldsProvider = Ctx.Provider;

export function useW(): Worlds {
  const w = useContext(Ctx);
  if (!w) throw new Error("useW outside WorldsProvider");
  return w;
}

// ---- bits shared by every page ----

function Why({ text }: { text: string }) {
  return text ? <p className="mc-note mc-why">{text}</p> : null;
}

export function ErrLine() {
  const w = useW();
  if (!w.error) return null;
  return (
    <div className="mc-errline" role="alert">
      <AlertTriangleIcon size={16} />
      <span>{w.error}</span>
      <button
        type="button"
        className="mc-icon-btn"
        aria-label="Dismiss"
        onClick={() => w.setError(null)}
      >
        <XIcon size={18} />
      </button>
    </div>
  );
}

/** A pushed page: its own back bar over the screen, climbed by back, swipe-down or the
 *  platform Back gesture (it registers in the shared back-layer stack, like a Sheet). */
function Layer({
  title,
  onBack,
  children,
}: {
  title: string;
  onBack: () => void;
  children: ReactNode;
}) {
  useBackLayer(onBack);
  const w = useW();
  const root = useRef<HTMLDivElement>(null);
  const covered = useCovered(w, root);
  const body = useRef<HTMLElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  // The card's own swipe-down would close all of Minecraft; a layer climbs one level.
  function onTouchStart(e: TouchEvent) {
    e.stopPropagation();
    const t = e.touches[0];
    const atTop = (body.current?.scrollTop ?? 0) <= 4;
    start.current = atTop && t ? { x: t.clientX, y: t.clientY } : null;
  }
  function onTouchMove(e: TouchEvent) {
    e.stopPropagation();
    const s = start.current;
    const t = e.touches[0];
    if (!s || !t) return;
    const dy = t.clientY - s.y;
    if (dy > SWIPE_DOWN_PX && dy > Math.abs(t.clientX - s.x) * 2) {
      start.current = null;
      onBack();
    }
  }
  return (
    <div
      className="subscreen mc-layer"
      ref={root}
      aria-hidden={covered || undefined}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
    >
      <header className="top-bar">
        <button type="button" className="back-btn" onClick={onBack} aria-label="Back">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">{title}</span>
        </button>
      </header>
      <main className="screen-body mc-screen" ref={body}>
        {children}
      </main>
    </div>
  );
}

/** A page with another pushed over it is out of reach: hidden from assistive tech and
 *  inert, so focus and taps can't land on what's underneath. */
export function useCovered(w: Worlds, el: { current: HTMLElement | null }): boolean {
  const [depth] = useState(() => w.pages.length);
  const covered = w.pages.length > depth;
  useEffect(() => {
    if (el.current) el.current.inert = covered;
  }, [covered, el]);
  return covered;
}

const Spin = () => (
  <span className="mc-spin" aria-hidden="true">
    <RefreshIcon size={16} />
  </span>
);

/** The job in flight: what, its phase, time since it started. This device alone adds the
 *  bytes of its own upload. */
export function JobCard() {
  const w = useW();
  if (w.uploadFailed) {
    const t = w.slotOf(w.uploadFailed.slot);
    return (
      <div className="mc-job" role="alert">
        <div className="mc-notice mc-notice-bad">
          <AlertTriangleIcon size={16} />
          <div>
            <b>Nothing was imported.</b> Nothing was written to any slot, and the upload was thrown
            away.
            <div className="mc-errtext">{w.uploadFailed.detail}</div>
          </div>
        </div>
        <div className="mc-btnrow">
          <button type="button" className="mc-btn" onClick={w.dismissUploadFailed}>
            Dismiss
          </button>
          <button
            type="button"
            className="mc-btn mc-btn-primary mc-grow"
            onClick={() => {
              w.dismissUploadFailed();
              w.setModal({ kind: "import", slot: t?.id ?? null, fixed: !!t });
            }}
          >
            <UploadIcon size={18} />
            Choose another file
          </button>
        </div>
      </div>
    );
  }
  const job = w.job;
  if (!job && w.upload) {
    const u = w.upload;
    const pct = u.total ? Math.round((u.sent / u.total) * 100) : 0;
    return (
      // biome-ignore lint/a11y/useSemanticElements: a card of blocks; <output> only takes phrasing content.
      <div className="mc-job" role="status">
        <div className="mc-job-h">
          <Spin />
          Importing a world
        </div>
        <div className="mc-job-ph">
          <b>Uploading</b> —{" "}
          <span className="mc-num">
            {fmtBytes(u.sent)} of {fmtBytes(u.total)}
          </span>{" "}
          from this device
        </div>
        {/* biome-ignore lint/a11y/useFocusableInteractive: a progressbar announces progress; it isn't operated. */}
        <div
          className="mc-bar"
          role="progressbar"
          aria-label="Upload"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
        >
          <i style={{ width: `${pct}%` }} />
        </div>
        <p className="mc-note">
          Keep this screen open until it finishes — the upload isn&apos;t resumable. The file is
          checked for level.dat before anything is written.
        </p>
      </div>
    );
  }
  if (!job) return null;
  return (
    // biome-ignore lint/a11y/useSemanticElements: a card of blocks; <output> only takes phrasing content.
    <div className="mc-job" role="status">
      <div className="mc-job-h">
        <Spin />
        {cap(job.what)}
        <span className="mc-job-t mc-num">{elapsedOf(job.started_at, w.nowMs)}</span>
      </div>
      {job.phase && (
        <div className="mc-job-ph">
          <b>{cap(job.phase)}</b>
          {PHASE_TEXT[job.phase] ? ` — ${PHASE_TEXT[job.phase]}` : ""}
        </div>
      )}
      <p className="mc-note">
        {job.started_at ? `Started ${clockOf(job.started_at)}. ` : ""}It carries on if you leave —
        any device opening this screen sees it.
      </p>
    </div>
  );
}

// ---- the main screen's row ----

export function WorldsEntry() {
  const w = useW();
  if (!w.worlds) return null;
  const used = w.slots.filter((s) => !isEmptySlot(s)).length;
  const last = Math.max(0, ...w.slots.map((s) => s.last_download ?? 0));
  let meta: ReactNode;
  if (w.job) meta = <span className="mc-navmeta warn">{jobLine(w.job)}</span>;
  else if (w.upload) {
    meta = (
      <span className="mc-navmeta warn">
        Uploading a world — {Math.round((w.upload.sent / (w.upload.total || 1)) * 100)}%
      </span>
    );
  } else {
    meta = (
      <span className="mc-navmeta">
        {w.active ? `${slotName(w.active)} loaded · ` : ""}
        {used} of {w.slots.length} slots used
      </span>
    );
  }
  return (
    <section className="mc-card">
      <button type="button" className="mc-navrow" onClick={() => w.push({ kind: "worlds" })}>
        <span className="mc-navico" aria-hidden="true">
          <LayersIcon size={20} />
        </span>
        <span className="mc-navtx">
          <span className="ops-card-title">Worlds &amp; backups</span>
          {meta}
          {last ? (
            <span className="mc-navmeta">last copy off the box {shortDate(last)}</span>
          ) : (
            <span className="mc-navmeta warn">no world has a copy off the box yet</span>
          )}
        </span>
        <ChevronRightIcon size={18} />
      </button>
    </section>
  );
}

// ---- Worlds ----

function pendingCount(w: Worlds, s: MinecraftSlot): number {
  return (w.pendingSettings[s.id] ?? []).length;
}

function WorldRow({ s }: { s: MinecraftSlot }) {
  const w = useW();
  const pend = pendingCount(w, s);
  return (
    <button type="button" className="mc-wrow" onClick={() => w.push({ kind: "world", slot: s.id })}>
      <span className={`mc-disc${s.active ? " on" : ""}`} aria-hidden="true">
        <CubeIcon size={18} />
      </span>
      <span className="mc-wtx">
        <span className="mc-wnm">
          <span className="mc-wt">{slotName(s)}</span>
          {s.active && <span className="badge ok mc-loaded">loaded</span>}
          {pend > 0 && <span className="mc-pend">{pend} pending</span>}
        </span>
        <span className="mc-wmeta">
          {cap(s.gamemode)} · {cap(s.difficulty)} ·{" "}
          <span className="mc-num">{s.exists ? fmtBytes(s.bytes) : "not generated"}</span>
        </span>
        <span className="mc-wmeta">{playedText(s, w.running, w.online.length, w.nowMs)}</span>
        <span className="mc-wmeta">
          {s.last_backup ? `last backup ${whenOf(s.last_backup, w.nowMs)}` : "no backups yet"} ·{" "}
          {s.last_download ? `downloaded ${shortDate(s.last_download)}` : "never downloaded"}
        </span>
      </span>
      <ChevronRightIcon size={18} />
    </button>
  );
}

function EmptyActions({ s }: { s: MinecraftSlot }) {
  const w = useW();
  return (
    <>
      <div className="mc-wactions">
        <button
          type="button"
          className="mc-btn mc-btn-primary"
          disabled={!!w.blocked}
          onClick={() => w.setModal({ kind: "newWorld", slot: s.id })}
        >
          <PlusIcon size={18} />
          New world
        </button>
        <button
          type="button"
          className="mc-btn"
          disabled={!!w.blocked}
          onClick={() => w.setModal({ kind: "import", slot: s.id, fixed: true })}
        >
          <UploadIcon size={18} />
          Import
        </button>
      </div>
      <Why text={w.blocked} />
    </>
  );
}

export function WorldsLayer() {
  const w = useW();
  const others = w.slots.filter((s) => !s.active && !isEmptySlot(s));
  const empties = w.slots.filter(isEmptySlot);
  const free = empties.length > 0;
  return (
    <Layer title="Worlds" onBack={w.pop}>
      <ErrLine />
      <JobCard />
      {!w.worlds && <p className="mc-note">Loading…</p>}
      {w.active && (
        <>
          <h3 className="mc-sect">Loaded</h3>
          <div className="mc-card">
            <WorldRow s={w.active} />
          </div>
        </>
      )}
      {others.length > 0 && (
        <>
          <h3 className="mc-sect">
            Other worlds<span className="mc-sect-r">tap one to load, back up or change it</span>
          </h3>
          <div className="mc-card">
            {others.map((s) => (
              <WorldRow key={s.id} s={s} />
            ))}
          </div>
        </>
      )}
      {empties.length > 0 && (
        <>
          <h3 className="mc-sect">Empty slots</h3>
          <div className="mc-card">
            {empties.map((s) => (
              <div className="mc-wempty" key={s.id}>
                <div className="mc-wrow mc-wrow-static">
                  <span className="mc-disc empty" aria-hidden="true">
                    <PlusIcon size={18} />
                  </span>
                  <span className="mc-wtx">
                    <span className="mc-wnm">
                      <span className="mc-wt dim">Slot {slotNumber(s.id)} — empty</span>
                    </span>
                    <span className="mc-wmeta">
                      {s.backups ? (
                        <>
                          {plural(s.backups, "backup")} kept from its last world —{" "}
                          <button
                            type="button"
                            className="mc-link mc-inline"
                            onClick={() => w.push({ kind: "world", slot: s.id })}
                          >
                            see them
                          </button>
                        </>
                      ) : (
                        "Make a new world here, or import one"
                      )}
                    </span>
                  </span>
                </div>
                <div className="mc-wbody">
                  <EmptyActions s={s} />
                </div>
              </div>
            ))}
          </div>
        </>
      )}
      {w.worlds && !free && (
        <div className="mc-btnrow mc-tight">
          <button
            type="button"
            className="mc-btn mc-grow"
            disabled={!!w.blocked}
            onClick={() => w.setModal({ kind: "import", slot: null, fixed: false })}
          >
            <UploadIcon size={18} />
            Import .mcworld over a world
          </button>
        </div>
      )}
      {w.worlds && (
        <p className="mc-note mc-foot-note">
          {free
            ? `${w.slots.length} slots. `
            : `All ${w.slots.length} slots in use — reset or empty one first. `}
          One world is loaded at a time; each keeps its own settings, rules and backups.
        </p>
      )}
    </Layer>
  );
}

// ---- one world ----

export function Seg<T extends string>({
  label,
  options,
  value,
  onPick,
  four,
}: {
  label: string;
  options: readonly T[];
  value: T | string;
  onPick: (v: T) => void;
  four?: boolean;
}) {
  return (
    <div className={`mc-seg${four ? " four" : ""}`} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          type="button"
          // biome-ignore lint/a11y/useSemanticElements: a styled option card or segment; a native radio can't carry this 44px button layout.
          role="radio"
          key={o}
          aria-checked={o === value}
          onClick={() => o !== value && onPick(o)}
        >
          {cap(o)}
        </button>
      ))}
    </div>
  );
}

const Pend = ({ on }: { on: boolean }) => (on ? <span className="mc-pend">pending</span> : null);

function Settings({ s }: { s: MinecraftSlot }) {
  const w = useW();
  const pend = w.pendingSettings[s.id] ?? [];
  const live = s.active && w.running;
  return (
    <div className="mc-pad">
      <p className={`mc-note${live ? " mc-live" : ""}`}>{settingsLine(s, w.running)}</p>
      <div className="mc-set">
        <div className="mc-set-l">
          Game mode <Pend on={pend.includes("gamemode")} />
        </div>
        <Seg
          label="Game mode"
          options={GAMEMODES}
          value={s.gamemode}
          onPick={(v) => void w.actions.setting(s.id, "gamemode", v)}
        />
        <p className="mc-note">
          The default for new players and new characters — anyone who has played keeps their own
          mode.
        </p>
      </div>
      <div className="mc-set">
        <div className="mc-set-l">
          Difficulty <Pend on={pend.includes("difficulty")} />
        </div>
        <Seg
          label="Difficulty"
          options={DIFFICULTIES}
          value={s.difficulty}
          four
          onPick={(v) => void w.actions.setting(s.id, "difficulty", v)}
        />
      </div>
      <div className="mc-sw-row">
        <span className="mc-sw-l" id={`mc-cheats-${s.id}`}>
          <b>Cheats</b> <Pend on={pend.includes("cheats")} />
          <br />
          {s.cheats ? "on — operators can run commands" : "off — no commands in game"}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={s.cheats}
          aria-label={`Cheats in ${slotName(s)}`}
          className={`mc-switch${s.cheats ? " on" : ""}`}
          onClick={() => void w.actions.setting(s.id, "cheats", !s.cheats)}
        >
          <span className="mc-knob" />
        </button>
      </div>
    </div>
  );
}

function useRules(slot: string | null, rev: number) {
  const [view, setView] = useState<MinecraftRules | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-read whenever a world changes (rev).
  useEffect(() => {
    if (!slot) return;
    let live = true;
    api
      .minecraftRules(slot)
      .then((v) => live && setView(v))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [slot, rev]);
  return [view, setView] as const;
}

function OffBox({ s, list }: { s: MinecraftSlot; list: MinecraftBackup[] }) {
  if (s.last_download) {
    const b = list.find((x) => x.downloaded_at === s.last_download);
    return (
      <div className="mc-offbox ok">
        <DownloadIcon size={16} />
        <span>
          <b>Last copy off the box: {whenOf(s.last_download, Date.now())}</b>
          {b ? ` — ${backupShort(b)}` : ""}. Minecraft backups aren&apos;t in the box backup, so a
          download is the only copy kept elsewhere.
        </span>
      </div>
    );
  }
  return (
    <div className="mc-offbox warn">
      <AlertTriangleIcon size={16} />
      <span>
        <b>No copy has left the box yet.</b> Minecraft backups aren&apos;t in the box backup —
        download one to keep a copy somewhere else.
      </span>
    </div>
  );
}

function Retention({ list, keep }: { list: MinecraftBackup[]; keep: number }) {
  const n = list.filter((b) => !b.pinned).length;
  const pins = list.length - n;
  const goes = nextToGo(list, keep);
  return (
    <div className="mc-retain">
      <b className="mc-num">
        {n} of {keep}
      </b>{" "}
      kept{pins ? ` · ${pins} pinned` : ""}
      <div className="mc-bar" aria-hidden="true">
        <i style={{ width: `${Math.min(100, (n / keep) * 100)}%` }} />
      </div>
      Each world keeps its newest {keep}. Automatic ones go first; pinned ones are kept for good,
      outside the {keep}.{goes && <b> The next backup removes {backupShort(goes)}.</b>}
    </div>
  );
}

function BackupRows({ slot, list }: { slot: string; list: MinecraftBackup[] }) {
  const w = useW();
  const pending = w.backingUp?.slot === slot ? w.backingUp : null;
  if (!list.length && !pending) {
    const s = w.slotOf(slot);
    return (
      <div className="mc-empty">
        {s && notGenerated(s)
          ? "Not generated yet — nothing to back up."
          : "No backups yet. Back up now before anything risky."}
      </div>
    );
  }
  return (
    <>
      {pending && (
        <div className="mc-krow">
          <span className="mc-kico">
            <Spin />
          </span>
          <span className="mc-ktx">
            <span className="mc-kl dim">{pending.label || "Backing up…"}</span>
            <span className="mc-ks">
              {w.running && w.slotOf(slot)?.active
                ? "copying live — players stay on"
                : "copying the stopped world"}
            </span>
          </span>
        </div>
      )}
      {list.map((b) => (
        <button
          type="button"
          key={b.name}
          className="mc-krow"
          onClick={() => w.setModal({ kind: "backup", name: b.name, slot })}
          aria-label={`${backupTitle(b)}, ${whenOf(b.created, w.nowMs)}`}
        >
          <span className={`mc-kico${b.auto ? "" : " mine"}`} aria-hidden="true">
            {b.auto ? <ClockIcon size={14} /> : <PencilIcon size={14} />}
          </span>
          <span className="mc-ktx">
            <span className={`mc-kl${b.auto ? " auto" : ""}`}>{backupTitle(b)}</span>
            <span className="mc-ks">
              {whenOf(b.created, w.nowMs)} · {fmtBytes(b.bytes)} · {b.auto ? "automatic" : "yours"}
              {b.downloaded_at ? ` · downloaded ${shortDate(b.downloaded_at)}` : ""}
            </span>
          </span>
          {b.pinned && (
            <span className="mc-kpin">
              <PinIcon size={13} />
              pinned
            </span>
          )}
          <ChevronRightIcon size={18} />
        </button>
      ))}
    </>
  );
}

export function WorldLayer({ slot }: { slot: string }) {
  const w = useW();
  const s = w.slotOf(slot);
  const list = w.backups[slot] ?? [];
  const { loadBackups, rev } = w;
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-read whenever a world changes (rev).
  useEffect(() => {
    void loadBackups(slot);
  }, [slot, rev, loadBackups]);
  const [rules] = useRules(s && !isEmptySlot(s) ? slot : null, rev);

  if (!s) {
    return (
      <Layer title={`Slot ${slotNumber(slot)}`} onBack={w.pop}>
        <p className="mc-note">Loading…</p>
      </Layer>
    );
  }
  if (isEmptySlot(s)) {
    return (
      <Layer title={`Slot ${slotNumber(slot)}`} onBack={w.pop}>
        <ErrLine />
        <JobCard />
        <div className="mc-card">
          <div className="mc-pad">
            <p className="mc-note">
              Empty. Make a new world here, import one, or restore one of the backups kept from its
              last world.
            </p>
            <EmptyActions s={s} />
          </div>
        </div>
        <h3 className="mc-sect">
          Backups<span className="mc-sect-r">kept from its last world</span>
        </h3>
        <div className="mc-card">
          <BackupRows slot={slot} list={list} />
        </div>
      </Layer>
    );
  }

  const changed = rules ? changedRules(rules).length : 0;
  const rulePend = rules?.pending.length ?? 0;
  const ruleCount = rules ? Object.keys(rules.rules).length : Object.keys(RULES).length;
  const canBackUp = s.exists && !w.backingUp;
  return (
    <Layer title={slotName(s)} onBack={w.pop}>
      <ErrLine />
      <JobCard />
      <div className="mc-hero">
        <div className="mc-state">
          <span className={`mc-dot mc-dot-${s.active ? "ok" : "off"}`} aria-hidden="true" />
          <span className="mc-state-word">{s.active ? "loaded" : "not loaded"}</span>
          <span className="mc-state-sub">
            {s.active
              ? w.running
                ? `${plural(w.online.length, "player")} on`
                : "server stopped"
              : `${w.active ? slotName(w.active) : "another world"} is loaded`}
          </span>
        </div>
        <dl className="mc-facts-dl">
          <dt>Origin</dt>
          <dd>{originText(s)}</dd>
          <dt>Seed</dt>
          <dd>
            {s.seed ? (
              <>
                <span className="mc-mono">{s.seed}</span>{" "}
                <button
                  type="button"
                  className="mc-link mc-inline"
                  aria-label={`Copy seed of ${slotName(s)}`}
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(s.seed ?? "");
                      w.toast(`Copied seed ${s.seed}`);
                    } catch {
                      w.setError("Couldn't copy — clipboard unavailable.");
                    }
                  }}
                >
                  Copy
                </button>
              </>
            ) : (
              <>
                unknown{" "}
                <span className="mc-faint">
                  — not recorded (its level.dat couldn&apos;t be read)
                </span>
              </>
            )}
          </dd>
          <dt>Size</dt>
          <dd className="mc-num">{s.exists ? fmtBytes(s.bytes) : "— (not generated)"}</dd>
          <dt>Played</dt>
          <dd>{playedText(s, w.running, w.online.length, w.nowMs)}</dd>
          <dt>Backups</dt>
          <dd>
            {s.backups
              ? `${s.backups}${s.last_backup ? ` · last ${whenOf(s.last_backup, w.nowMs)}` : ""}`
              : "none yet"}
          </dd>
        </dl>
        {!s.active && (
          <>
            <button
              type="button"
              className="mc-btn mc-btn-primary"
              disabled={!!w.blocked}
              onClick={() => w.setModal({ kind: "load", slot })}
            >
              <PlayIcon size={18} />
              Load {slotName(s)}
            </button>
            <Why text={w.blocked} />
          </>
        )}
      </div>

      <h3 className="mc-sect">
        Settings<span className="mc-sect-r">this world only</span>
      </h3>
      <div className="mc-card">
        <Settings s={s} />
        <button
          type="button"
          className="mc-row mc-rules-entry"
          onClick={() => w.push({ kind: "rules", slot })}
        >
          <span className="mc-navico" aria-hidden="true">
            <SlidersIcon size={20} />
          </span>
          <span className="mc-rn">
            <span className="mc-nm mc-nm-sm">Game rules</span>
            <span className="mc-sm">
              {plural(ruleCount, "rule")} ·{" "}
              {changed ? `${changed} changed from default` : "all at defaults"}
              {rulePend > 0 && <span className="mc-pend-t"> · {rulePend} pending</span>}
            </span>
          </span>
          <ChevronRightIcon size={18} />
        </button>
      </div>

      <h3 className="mc-sect">
        Backups<span className="mc-sect-r">{plural(list.length, "backup")}</span>
      </h3>
      <div className="mc-card">
        <div className="mc-pad">
          <button
            type="button"
            className="mc-btn mc-btn-primary"
            disabled={!!w.blocked || !canBackUp}
            onClick={() => w.setModal({ kind: "backupNow", slot })}
          >
            <ArchiveIcon size={18} />
            Back up {slotName(s)} now
          </button>
          <Why text={w.blocked} />
          {s.exists || list.length ? (
            <>
              <OffBox s={s} list={list} />
              <Retention list={list} keep={w.worlds?.keep_per_slot ?? 20} />
            </>
          ) : (
            <p className="mc-note">
              Not generated yet — there&apos;s nothing to back up until it first loads.
            </p>
          )}
        </div>
        <div className="mc-klist">
          <BackupRows slot={slot} list={list} />
        </div>
      </div>

      <h3 className="mc-sect">Manage</h3>
      <div className="mc-card">
        <div className="mc-pad">
          <div className="mc-wactions">
            <button
              type="button"
              className="mc-btn"
              onClick={() => w.setModal({ kind: "rename", slot })}
            >
              <PencilIcon size={18} />
              Rename
            </button>
            <button
              type="button"
              className="mc-btn"
              disabled={!!w.blocked}
              onClick={() => w.setModal({ kind: "import", slot, fixed: true })}
            >
              <UploadIcon size={18} />
              Import over
            </button>
            <button
              type="button"
              className="mc-btn mc-btn-danger mc-full"
              disabled={!!w.blocked}
              onClick={() => w.setModal({ kind: "reset", slot })}
            >
              <UndoIcon size={18} />
              Reset…
            </button>
          </div>
          <Why text={w.blocked} />
        </div>
      </div>
    </Layer>
  );
}

// ---- game rules ----

/** The editor, for a world's rules (saved through the API) or a new world's draft. */
export function RulesEditor({
  values,
  defaults,
  pending,
  note,
  onSet,
  onReset,
  debounceMs,
  idPrefix,
}: {
  values: Record<string, MinecraftRuleValue>;
  defaults: Record<string, MinecraftRuleValue>;
  pending: string[];
  note: ReactNode;
  onSet: (id: string, v: MinecraftRuleValue) => void;
  onReset: (ids: string[], group: string | null) => void;
  debounceMs: number;
  idPrefix: string;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // A stepper's shown value while its save waits for the taps to settle.
  const [draft, setDraft] = useState<Record<string, number>>({});
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  useEffect(
    () => () => {
      for (const t of Object.values(timers.current)) clearTimeout(t);
    },
    [],
  );

  const shownValue = (id: string) => (id in draft ? (draft[id] as number) : values[id]);
  const groups = useMemo(() => groupsFor(values), [values]);
  const needle = q.trim().toLowerCase();
  const hit = (r: RuleDef) =>
    !needle || `${r.label} ${r.id} ${r.sub ?? ""}`.toLowerCase().includes(needle);

  function settle(r: RuleDef, n: number) {
    const v = clampRule(r, n);
    setDraft((d) => ({ ...d, [r.id]: v }));
    clearTimeout(timers.current[r.id]);
    timers.current[r.id] = setTimeout(() => {
      delete timers.current[r.id];
      setDraft((d) => {
        const { [r.id]: _, ...rest } = d;
        return rest;
      });
      if (v !== values[r.id]) onSet(r.id, v);
    }, debounceMs);
  }

  const changed = (id: string) => id in defaults && shownValue(id) !== defaults[id];
  const total = Object.keys(values).filter(changed).length;
  let shown = 0;

  return (
    <div className="mc-rules">
      {note}
      <input
        className="mc-inp"
        type="search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Find a rule — e.g. phantoms"
        aria-label="Find a game rule"
        autoComplete="off"
        spellCheck={false}
      />
      <div className="mc-rgroups">
        {groups.map((g) => {
          const rows = g.rules.filter(hit);
          shown += rows.length;
          if (!rows.length) return null;
          const ch = g.rules.filter((r) => changed(r.id));
          const pe = g.rules.filter((r) => pending.includes(r.id)).length;
          const isOpen = needle ? true : !!open[g.id];
          return (
            <details
              key={g.id}
              className="mc-rgrp"
              open={isOpen}
              onToggle={(e) => {
                const now = (e.currentTarget as HTMLDetailsElement).open;
                if (!needle && now !== !!open[g.id]) setOpen((o) => ({ ...o, [g.id]: now }));
              }}
            >
              <summary>
                <span className="mc-gt">{g.title}</span>
                <span className="mc-gm">
                  {plural(g.rules.length, "rule")}
                  {ch.length > 0 && <span className="mc-chg-t"> · {ch.length} changed</span>}
                  {pe > 0 && <span className="mc-pend-t"> · {pe} pending</span>}
                </span>
                <ChevronRightIcon size={16} />
              </summary>
              <div className="mc-rbody">
                {rows.map((r) => (
                  <RuleRow
                    key={r.id}
                    r={r}
                    v={shownValue(r.id) as MinecraftRuleValue}
                    def={defaults[r.id]}
                    pending={pending.includes(r.id)}
                    idPrefix={idPrefix}
                    onSet={(v) => onSet(r.id, v)}
                    onStep={(n) => settle(r, n)}
                  />
                ))}
                {ch.length > 0 && (
                  <button
                    type="button"
                    className="mc-link mc-rreset"
                    onClick={() =>
                      onReset(
                        ch.map((r) => r.id),
                        g.title,
                      )
                    }
                  >
                    Reset {g.title.toLowerCase()} to defaults
                  </button>
                )}
              </div>
            </details>
          );
        })}
      </div>
      {shown === 0 && <p className="mc-note">No rule matches.</p>}
      <div className="mc-rfoot">
        <span className="mc-note">
          {total
            ? `${plural(total, "rule")} ${total === 1 ? "differs" : "differ"} from the defaults`
            : "All rules at their defaults"}
        </span>
        {total > 0 && (
          <button
            type="button"
            className="mc-btn"
            onClick={() => onReset(Object.keys(values).filter(changed), null)}
          >
            <UndoIcon size={18} />
            Reset all
          </button>
        )}
      </div>
    </div>
  );
}

function RuleRow({
  r,
  v,
  def,
  pending,
  idPrefix,
  onSet,
  onStep,
}: {
  r: RuleDef;
  v: MinecraftRuleValue;
  def: MinecraftRuleValue | undefined;
  pending: boolean;
  idPrefix: string;
  onSet: (v: MinecraftRuleValue) => void;
  onStep: (n: number) => void;
}) {
  const changed = def !== undefined && v !== def;
  const [typed, setTyped] = useState<string | null>(null);
  let control: ReactNode;
  if (r.kind === "bool") {
    control = (
      <button
        type="button"
        role="switch"
        aria-checked={v === true}
        aria-label={r.label}
        className={`mc-switch${v ? " on" : ""}`}
        onClick={() => onSet(!v)}
      >
        <span className="mc-knob" />
      </button>
    );
  } else if (r.kind === "choice") {
    const opts = choiceOptions(v);
    control =
      opts.length === 1 ? (
        <span className="mc-rstatic">
          {String(v)}
          <span className="mc-sm">the only value this server reports</span>
        </span>
      ) : (
        <select
          className="mc-inp mc-sel"
          value={String(v)}
          aria-label={r.label}
          onChange={(e) => onSet(e.target.value)}
        >
          {opts.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      );
  } else {
    const n = Number(v);
    control = (
      <div className="mc-stepper">
        <button
          type="button"
          className="mc-btn"
          aria-label={`Lower — ${r.label}`}
          disabled={n <= (r.min ?? 0)}
          onClick={() => onStep(n - (r.step ?? 1))}
        >
          <MinusIcon size={18} />
        </button>
        <input
          id={`${idPrefix}-${r.id}`}
          className="mc-sv"
          inputMode="numeric"
          value={typed ?? String(n)}
          aria-label={`${r.label}${r.unit ? ` (${r.unit})` : ""}`}
          onChange={(e) => {
            setTyped(e.target.value);
            const parsed = Number.parseInt(e.target.value.replace(/[^\d]/g, ""), 10);
            if (!Number.isNaN(parsed)) onStep(parsed);
          }}
          onBlur={() => setTyped(null)}
        />
        <button
          type="button"
          className="mc-btn"
          aria-label={`Raise — ${r.label}`}
          disabled={n >= (r.max ?? Number.MAX_SAFE_INTEGER)}
          onClick={() => onStep(n + (r.step ?? 1))}
        >
          <PlusIcon size={18} />
        </button>
      </div>
    );
  }
  return (
    <div className={`mc-rule ${r.kind}`}>
      <span className="mc-rl">
        <span className="mc-rt">{r.label}</span>
        {r.sub && <span className="mc-rs">{r.sub}</span>}
        <span className="mc-rid mc-mono">{r.id}</span>
        {(pending || changed) && (
          <span className="mc-rtags">
            {pending && <span className="mc-pend">pending</span>}
            {changed && def !== undefined && (
              <span className="mc-chg">changed · default {ruleWord(r, def)}</span>
            )}
          </span>
        )}
      </span>
      {control}
    </div>
  );
}

export function RulesLayer({ slot }: { slot: string }) {
  const w = useW();
  const s = w.slotOf(slot);
  const [view, setView] = useRules(slot, w.rev);
  if (!s) return null;

  async function save(set: Record<string, MinecraftRuleValue>, done: (v: MinecraftRules) => void) {
    if (!view) return;
    setView({ ...view, rules: { ...view.rules, ...set } });
    try {
      const next = await api.minecraftSetRules(slot, set);
      setView(next);
      done(next);
    } catch (err) {
      w.setError(`Couldn't save the rule — ${message(err)}`);
      setView(await api.minecraftRules(slot).catch(() => view));
    }
  }

  const note = view
    ? (() => {
        const n = rulesNote(view, s);
        return (
          <div className={`mc-offbox mc-rules-note${n.tone === "live" ? " ok" : ""}`}>
            {n.tone === "live" ? <CheckIcon size={16} /> : <InfoIcon size={16} />}
            <span>
              <b>{n.head}</b> {n.rest}
            </span>
          </div>
        );
      })()
    : null;

  return (
    <Layer title="Game rules" onBack={w.pop}>
      <ErrLine />
      <h3 className="mc-sect">
        <span className="mc-sect-name">{slotName(s)}</span>
        <span className="mc-sect-r">{s.active ? "loaded" : "not loaded"}</span>
      </h3>
      {!view ? (
        <p className="mc-note">Reading the rules…</p>
      ) : (
        <RulesEditor
          values={view.rules}
          defaults={view.defaults}
          pending={view.pending}
          note={note}
          debounceMs={RULE_DEBOUNCE_MS}
          idPrefix={`r-${slot}`}
          onSet={(id, v) =>
            void save({ [id]: v }, (next) => {
              const r = RULES[id] ?? {
                id,
                label: id,
                group: "other",
                def: v,
                kind: "num" as const,
              };
              w.toast(ruleSavedToast(r, v, next.live, pendWhen(s, w.running)));
            })
          }
          onReset={(ids) =>
            void save(
              Object.fromEntries(ids.map((id) => [id, view.defaults[id] as MinecraftRuleValue])),
              (next) =>
                w.toast(
                  `${plural(ids.length, "rule")} back to default${next.live ? " — applied live" : ` — applied ${pendWhen(s, w.running)}`}`,
                ),
            )
          }
        />
      )}
    </Layer>
  );
}
