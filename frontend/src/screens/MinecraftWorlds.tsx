// The Worlds sub-screen of the Minecraft screen (DESIGN.md "Minecraft server screen" →
// "Worlds and backups"; binding mock docs/mocks/minecraft-worlds/b-worlds-subscreen.html).
// One row on the main screen pushes Worlds; each world pushes its own page with its
// backups; Game rules is a page of its own. Forms open in the shared Sheet, consequences
// confirm in the shared Dialog (MinecraftWorldSheets.tsx).
//
// Jobs show only what /status reports (what, phase, started_at). The one thing this device
// adds is an upload's real byte count, which no other device can know.

import {
  type KeyboardEvent,
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
  type MinecraftLastJob,
  type MinecraftNewWorld,
  type MinecraftResetMode,
  type MinecraftRuleValue,
  type MinecraftRules,
  type MinecraftSlot,
  type MinecraftWorlds,
  api,
  isAccepted,
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
  MapIcon,
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
import type { MapIntent } from "../minecraftMapNav";
import {
  DIFFICULTIES,
  DRAFT_KEEP_MODES,
  GAMEMODES,
  KEEP_HELP,
  KEEP_MODES,
  KEEP_WORD,
  type KeepMode,
  PHASE_TEXT,
  RESTART_KEY,
  RULES,
  type RuleDef,
  TRANSPORT_HELP,
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
  isRefusal,
  jobLine,
  keepFailText,
  keepMeaning,
  keepMode,
  keepSaved,
  keepSavedToast,
  keepSteps,
  nextToGo,
  notGenerated,
  originText,
  pendWhen,
  playedText,
  refusalReason,
  ruleSavedToast,
  ruleWord,
  rulesNote,
  settingsLine,
  slotName,
  slotNumber,
  transportBusyText,
  transportFailText,
  transportMeaning,
  transportOn,
  transportSavedToast,
  whenOf,
  worldBlocked,
  wreckFactText,
  wreckLaterText,
  wreckRowText,
  wreckTone,
} from "../minecraftWorlds";

/** One save per settled stepper: the value moves at once, the PUT goes out when tapping or
 *  typing stops, so four taps are one /gamerule and one toast. */
export const RULE_DEBOUNCE_MS = 700;
const SWIPE_DOWN_PX = 56;

export type Page =
  | { kind: "worlds" }
  | { kind: "world"; slot: string }
  | { kind: "rules"; slot: string }
  /** `at`: opened from a chat's map card, on that card's spot. */
  | { kind: "map"; slot?: string; at?: MapIntent };

export type WorldModal =
  | { kind: "newWorld"; slot: string }
  | { kind: "import"; slot: string | null; fixed: boolean }
  | { kind: "importOver"; slot: string; file: File }
  | { kind: "rename"; slot: string }
  | { kind: "reset"; slot: string }
  | {
      kind: "resetConfirm";
      slot: string;
      mode: MinecraftResetMode;
      seed?: string;
      wreck?: boolean;
    }
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
/** What can wait for a start or load: the settings, and Power Pack teleport. */
type PendKey = SettingKey | "transport";

/** A reason as a clause, so it can sit mid-sentence. */
const clause = (text: string) => text.trim().replace(/[.\s]+$/, "");

function message(err: unknown): string {
  return clause(err instanceof ApiError ? err.message : "the request failed");
}

const isAbort = (err: unknown) => err instanceof DOMException && err.name === "AbortError";

function withPend(
  p: Record<string, PendKey[]>,
  slot: string,
  key: PendKey,
  on: boolean,
): Record<string, PendKey[]> {
  const had = p[slot] ?? [];
  if (had.includes(key) === on) return p;
  return { ...p, [slot]: on ? [...had, key] : had.filter((k) => k !== key) };
}

/** A long operation this device is waiting on: its outcome arrives as a newer
 *  `server.last_job` once `server.job` clears. `since` is the last outcome seen before it was
 *  sent — newer means this one, and comparing the box's own stamps sidesteps clock skew. */
interface Follow {
  since: number;
  /** Not confirmed accepted (the request failed in transit): drop it if no job shows. */
  soft: boolean;
  misses: number;
  ok: () => void;
  fail: (detail: string) => void;
}

/** Status polls with neither a job nor a new outcome before an accepted job is given up on. */
const FOLLOW_MISSES = 5;

/** Everything the Worlds pages and their modals share, owned by the Minecraft screen. */
export function useWorlds({
  serverUp,
  running,
  online,
  spawnWreck,
  job,
  lastJob,
  pendingRestart,
  statusSeq,
  updating,
  state,
  nowMs,
  toast,
  refreshStatus,
}: {
  serverUp: boolean;
  running: boolean;
  online: string[];
  /** The server's crash-site default, which the New-world and Reset sheets start from. */
  spawnWreck: boolean;
  job: MinecraftJob | null;
  lastJob: MinecraftLastJob | null;
  pendingRestart: string[];
  /** Bumped on every status read, so a followed job is checked once per poll. */
  statusSeq: number;
  updating: boolean;
  state: string;
  nowMs: number;
  toast: (msg: string) => void;
  refreshStatus: () => void;
}) {
  const [worlds, setWorlds] = useState<MinecraftWorlds | null>(null);
  const [worldsFailed, setWorldsFailed] = useState<string | null>(null);
  const [backups, setBackups] = useState<Record<string, MinecraftBackup[]>>({});
  const [pages, setPages] = useState<Page[]>([]);
  const [modal, setModal] = useState<WorldModal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [uploadFailed, setUploadFailed] = useState<{ slot: string; detail: string } | null>(null);
  const [backingUp, setBackingUp] = useState<{ slot: string; label: string } | null>(null);
  // A long operation this device sent and hasn't seen settle: the other world actions wait,
  // closing the gap before the box's job shows in a status poll.
  const [inFlight, setInFlight] = useState<string | null>(null);
  // Changes saved to a world that isn't loaded wait for its next load. The box records
  // nothing for those, so this device remembers its own; restart-bound ones come from
  // `pendingRestart`, which every device shares.
  // Power Pack teleport isn't a server property, so `pendingRestart` never names it: a
  // change saved to the stopped, loaded world waits here too, until the server starts.
  const [nextLoad, setNextLoad] = useState<Record<string, PendKey[]>>({});
  const [rev, setRev] = useState(0);
  const follow = useRef<Follow | null>(null);
  const lastJobRef = useRef(lastJob);
  lastJobRef.current = lastJob;
  const uploadAbort = useRef<AbortController | null>(null);
  const openers = useRef<(HTMLElement | null)[]>([]);
  const refocus = useRef<HTMLElement | null>(null);

  const reload = useCallback(async () => {
    try {
      setWorlds(await api.minecraftWorlds());
      setWorldsFailed(null);
    } catch (err) {
      setWorldsFailed(message(err));
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
  const lastJobKey = useRef(jobKey);
  useEffect(() => {
    if (lastJobKey.current !== jobKey) void reload();
    lastJobKey.current = jobKey;
  }, [jobKey, reload]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: checked once per status read.
  useEffect(() => {
    const f = follow.current;
    if (!f) return;
    if (job) {
      f.misses = 0;
      f.soft = false;
      return;
    }
    if (lastJob && lastJob.finished_at > f.since) {
      follow.current = null;
      setInFlight(null);
      if (lastJob.ok) f.ok();
      else f.fail(lastJob.detail || "it failed");
      void reload();
      return;
    }
    f.misses += 1;
    if (f.misses >= (f.soft ? 1 : FOLLOW_MISSES)) {
      follow.current = null;
      setInFlight(null);
      void reload();
    }
  }, [statusSeq]);

  // Loading a world applies its waiting changes; from then on the box reports them.
  const activeId = worlds?.slots.find((s) => s.active)?.id;
  useEffect(() => {
    if (activeId) setNextLoad((p) => (p[activeId]?.length ? { ...p, [activeId]: [] } : p));
  }, [activeId]);
  useEffect(() => {
    if (running && activeId) setNextLoad((p) => withPend(p, activeId, "transport", false));
  }, [running, activeId]);

  // Focus goes back to the row that pushed a page once that page is popped (and the page
  // beneath stops being inert).
  // biome-ignore lint/correctness/useExhaustiveDependencies: runs after each push or pop.
  useEffect(() => {
    const el = refocus.current;
    refocus.current = null;
    if (!el) return;
    // After this commit's other effects, which lift `inert` from the screen beneath.
    const t = setTimeout(() => el.isConnected && el.focus({ preventScroll: true }), 0);
    return () => clearTimeout(t);
  }, [pages.length]);

  const slots = worlds?.slots ?? [];
  const slotOf = (id: string) => slots.find((s) => s.id === id) ?? null;
  const active = slots.find((s) => s.active) ?? null;
  const blocked =
    worldBlocked(job, updating, state) ||
    (upload ? "Wait — a world is uploading from this device." : "") ||
    (inFlight ? `Wait — the server is ${inFlight}.` : "");

  function pendingFor(s: MinecraftSlot): PendKey[] {
    if (!s.active) return nextLoad[s.id] ?? [];
    const restart: PendKey[] = (Object.keys(RESTART_KEY) as SettingKey[]).filter((k) =>
      pendingRestart.includes(RESTART_KEY[k]),
    );
    return nextLoad[s.id]?.includes("transport") ? [...restart, "transport"] : restart;
  }

  async function run(verb: string, fn: () => Promise<void>): Promise<boolean> {
    setError(null);
    try {
      await fn();
      return true;
    } catch (err) {
      setError(`Couldn't ${verb} — ${message(err)}.`);
      return false;
    } finally {
      void reload();
      refreshStatus();
    }
  }

  function startFollow(since: number, soft: boolean, verb: string, ok: () => void) {
    follow.current = {
      since,
      soft,
      misses: 0,
      ok,
      fail: (detail) => setError(`Couldn't ${verb} — ${clause(detail)}.`),
    };
  }

  /** A load, reset or restore: sent at once, answered with its result, a refusal, or
   *  `accepted` — then followed through the status poll to its outcome. */
  async function longOp(o: {
    what: string;
    verb: string;
    send: () => Promise<unknown>;
    done: (res: unknown) => void;
    /** Added to "Nothing changed" on a refusal, e.g. which world is still loaded. */
    still?: string | undefined;
  }) {
    setError(null);
    setInFlight(o.what);
    const since = lastJobRef.current?.finished_at ?? 0;
    const sent = o.send();
    refreshStatus();
    try {
      const res = await sent;
      if (isAccepted(res)) {
        startFollow(since, false, o.verb, () => o.done(null));
        return;
      }
      setInFlight(null);
      o.done(res);
    } catch (err) {
      if (err instanceof ApiError && isRefusal(err.status)) {
        setInFlight(null);
        setError(
          `Couldn't ${o.verb} — ${clause(err.message)}. Nothing changed${o.still ? `; ${o.still}` : ""}.`,
        );
      } else {
        // The answer was lost, not the work: the box may well be carrying on.
        setError(
          `Couldn't confirm that — ${message(err)}. The server may still be ${o.what}; watch for its card here.`,
        );
        startFollow(since, true, o.verb, () => o.done(null));
      }
    } finally {
      void reload();
      refreshStatus();
    }
  }

  const actions = {
    async load(slot: string) {
      const t = slotOf(slot);
      if (!t) return;
      const name = slotName(t);
      await longOp({
        what: "loading a world",
        verb: `load ${name}`,
        still: active ? `${slotName(active)} is still loaded` : undefined,
        send: () => api.minecraftLoadWorld(slot),
        done: (res) => {
          if (res && (res as { loaded?: boolean }).loaded === false) {
            toast(`${name} is already loaded`);
          } else
            toast(
              running
                ? `${name} is loaded — players can join`
                : `${name} is loaded — start the server to play it`,
            );
        },
      });
    },
    async create(slot: string, body: MinecraftNewWorld) {
      if (
        await run("create the world", async () => {
          await api.minecraftCreateWorld(slot, body);
        })
      ) {
        toast(
          `${body.name} is in slot ${slotNumber(slot)} — it's generated${body.wreck ? ", crash site and all," : ""} the first time you load it`,
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
      const was = t[key];
      const put = (v: string | boolean) =>
        setWorlds((w) =>
          w ? { ...w, slots: w.slots.map((s) => (s.id === slot ? { ...s, [key]: v } : s)) } : w,
        );
      // Optimistic: the segment moves at once, and moves back if the box refuses (409 while
      // a job holds the world).
      put(value);
      const ok = await run("save the setting", async () => {
        await api.minecraftUpdateWorld(slot, { [key]: value });
      });
      if (!ok) {
        put(was);
        return;
      }
      if (key === "difficulty" && t.active && running) {
        toast(`Applied live — difficulty is ${value} now`);
        return;
      }
      if (!t.active) {
        setNextLoad((p) => ({ ...p, [slot]: [...new Set([...(p[slot] ?? []), key])] }));
      }
      const when = pendWhen(t, running);
      if (key === "gamemode") {
        toast(
          `Saved — ${value} is the default for new players ${when}; existing players keep theirs`,
        );
      } else if (key === "cheats") toast(`Saved — cheats ${value ? "on" : "off"} ${when}`);
      else toast(`Saved — difficulty changes ${when}`);
    },
    /** Power Pack teleport: the switch's own route. Answers the rose line on a failure, worded
     *  from the slot as re-read (keep inventory's rule), or null once it has landed. */
    async transport(slot: string, on: boolean): Promise<string | null> {
      const t = slotOf(slot);
      if (!t) return null;
      try {
        const { applies, ...rec } = await api.minecraftSetTransport(slot, on);
        const live = applies === "now";
        // The route answers with the slot as written: show it at once, so the switch never
        // snaps back to the old value while the list is re-read (or if that read fails).
        setWorlds((w) =>
          w ? { ...w, slots: w.slots.map((x) => (x.id === slot ? { ...x, ...rec } : x)) } : w,
        );
        setNextLoad((p) => withPend(p, slot, "transport", !live));
        toast(transportSavedToast(on, t, live));
        return null;
      } catch (err) {
        const refused = err instanceof ApiError && isRefusal(err.status);
        const reason = refused ? refusalReason(err.message) : "the server didn't answer";
        const fresh = await api.minecraftWorlds().catch(() => null);
        if (fresh) setWorlds(fresh);
        const now = fresh?.slots.find((x) => x.id === slot);
        return transportFailText({ to: on, now: now ? transportOn(now) : null, refused, reason });
      } finally {
        void reload();
      }
    },
    async reset(slot: string, mode: MinecraftResetMode, seed?: string, wreck?: boolean) {
      const t = slotOf(slot);
      const name = t ? slotName(t) : "It";
      await longOp({
        what: "resetting a world",
        verb: `reset ${name}`,
        send: () => api.minecraftResetWorld(slot, mode, seed, wreck),
        done: () => {
          toast(
            mode === "empty"
              ? `Slot ${slotNumber(slot)} is empty — its backups are kept`
              : `${name} is reset — undo it from its backups`,
          );
          void loadBackups(slot);
        },
      });
    },
    async importWorld(slot: string, file: File) {
      setError(null);
      setUploadFailed(null);
      setUpload({ slot, file: file.name, sent: 0, total: file.size });
      const abort = new AbortController();
      uploadAbort.current = abort;
      const since = lastJobRef.current?.finished_at ?? 0;
      const landed = (name?: string | null) =>
        toast(`${name ?? "The world"} imported into slot ${slotNumber(slot)}`);
      const sent = api.minecraftImportWorld(
        slot,
        file,
        "",
        (done, total) => setUpload({ slot, file: file.name, sent: done, total }),
        abort.signal,
      );
      refreshStatus();
      try {
        const res = await sent;
        if (isAccepted(res)) {
          setInFlight("importing a world");
          startFollow(since, false, `import ${file.name}`, () => landed());
        } else landed(res?.name);
      } catch (err) {
        if (isAbort(err)) toast("Upload cancelled — nothing was imported");
        else if (err instanceof ApiError && err.status === 400) {
          // The box refused the file itself: the card says why, with another try.
          setUploadFailed({ slot, detail: err.message });
        } else if (err instanceof ApiError && isRefusal(err.status)) {
          setError(`Couldn't import ${file.name} — ${clause(err.message)}. Nothing changed.`);
        } else {
          setError(
            `Couldn't confirm the import — ${message(err)}. The server may still be importing; watch for its card here.`,
          );
          setInFlight("importing a world");
          startFollow(since, true, `import ${file.name}`, () => landed());
        }
      } finally {
        uploadAbort.current = null;
        setUpload(null);
        void reload();
        void loadBackups(slot);
        refreshStatus();
      }
    },
    cancelUpload() {
      uploadAbort.current?.abort();
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
      const source = slots.find((s) => s.folder === b.folder) ?? null;
      const when = whenOf(b.created, nowMs);
      await longOp({
        what: "restoring a backup",
        verb: "restore it",
        send: () => api.minecraftRestoreBackup(b.name, to),
        done: () => {
          if (t && b.folder === t.folder) toast(`Restored — ${slotName(t)} is back to ${when}`);
          else {
            toast(`Slot ${slotNumber(to)} now holds ${source?.name ?? b.folder} from ${when}`);
          }
          void loadBackups(to);
        },
      });
    },
    downloaded(b: MinecraftBackup, slot: string) {
      toast(`Downloading ${backupTitle(b)} (${fmtBytes(b.bytes)})`);
      // The box stamps the download once the whole file has gone out, which for a big
      // world takes a while: read it back early, and again later.
      for (const ms of [3000, 10_000]) {
        setTimeout(() => {
          void loadBackups(slot);
          void reload();
        }, ms);
      }
    },
  };

  return {
    worlds,
    worldsFailed,
    slots,
    active,
    slotOf,
    backups,
    loadBackups,
    pages,
    push: (p: Page) => {
      if (p.kind === "worlds") void reload();
      openers.current.push(
        document.activeElement instanceof HTMLElement ? document.activeElement : null,
      );
      setPages((s) => [...s, p]);
    },
    pop: () => {
      refocus.current = openers.current.pop() ?? null;
      setPages((p) => p.slice(0, -1));
    },
    modal,
    setModal,
    error,
    setError,
    upload,
    uploadFailed,
    dismissUploadFailed: () => setUploadFailed(null),
    backingUp,
    inFlight,
    pendingFor,
    pendingRestart,
    blocked,
    rev,
    reload,
    running,
    online,
    spawnWreck,
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
  const back = useRef<HTMLButtonElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  // A pushed page takes focus at its back button, so a keyboard or screen reader lands on it.
  useEffect(() => {
    back.current?.focus({ preventScroll: true });
  }, []);
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
        <button type="button" className="back-btn" onClick={onBack} aria-label="Back" ref={back}>
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
  // The bytes first: the box's own job ("receiving a world upload") says less than this
  // device knows until the last byte has gone.
  if (w.upload && (!job || w.upload.sent < w.upload.total)) {
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
        <button type="button" className="mc-btn" onClick={w.actions.cancelUpload}>
          <XIcon size={18} />
          Cancel upload
        </button>
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

/** The worlds list couldn't be read: say so, with a way to try again. */
function WorldsFailed() {
  const w = useW();
  if (!w.worldsFailed) return null;
  return (
    <div className="mc-errline" role="alert">
      <AlertTriangleIcon size={16} />
      <span>Couldn&apos;t read the worlds — {w.worldsFailed}.</span>
      <button type="button" className="mc-link mc-retry" onClick={() => void w.reload()}>
        Retry
      </button>
    </div>
  );
}

export function WorldsEntry() {
  const w = useW();
  if (!w.worlds) return <WorldsFailed />;
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
  return w.pendingFor(s).length;
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
          {/* Off only: the default is on, so a normal world's glance doesn't grow. */}
          {!transportOn(s) && (
            <>
              {" "}
              · <span className="mc-wmeta-off">teleport off</span>
            </>
          )}
        </span>
        <span className="mc-wmeta">{playedText(s, w.running, w.online.length, w.nowMs)}</span>
        <span className="mc-wmeta">
          {s.last_backup ? `last backup ${whenOf(s.last_backup, w.nowMs)}` : "no backups yet"} ·{" "}
          {s.last_download ? `downloaded ${shortDate(s.last_download)}` : "never downloaded"}
        </span>
        <span className="mc-wmeta">
          Crash site: <span className={wreckTone(s, w.running)}>{wreckRowText(s, w.running)}</span>
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
      {!w.worlds && (w.worldsFailed ? <WorldsFailed /> : <p className="mc-note">Loading…</p>)}
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

/** A segmented choice: a radiogroup with one tab stop (the picked segment); arrows move between
 *  segments and Space or Enter picks, because a pick here is a write to the server. */
export function Seg<T extends string>({
  label,
  labelledBy,
  options,
  value,
  onPick,
  four,
  text,
  weights,
  busy,
}: {
  label: string;
  /** The visible label's id; the group is named by it instead of `label`. */
  labelledBy?: string;
  options: readonly T[];
  value: T | string;
  onPick: (v: T) => void;
  four?: boolean;
  /** Words per option, where they aren't the capitalised value. */
  text?: Partial<Record<T, string>>;
  /** A wider share of the row for a longer word, so it fits a third of 320 px at full Text size. */
  weights?: Partial<Record<T, number>>;
  /** The option a write is in flight for: it spins, and the others take no picks until it lands. */
  busy?: T | null;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const at = Math.max(
    0,
    options.findIndex((o) => o === value),
  );
  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    const i = refs.current.findIndex((b) => b === document.activeElement);
    if (i < 0) return;
    let to: number | null = null;
    if (step) to = (i + step + options.length) % options.length;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = options.length - 1;
    if (to === null) return;
    e.preventDefault();
    refs.current[to]?.focus();
  }
  return (
    <div
      className={`mc-seg${four ? " four" : ""}`}
      role="radiogroup"
      aria-label={labelledBy ? undefined : label}
      aria-labelledby={labelledBy}
      onKeyDown={onKeyDown}
    >
      {options.map((o, i) => {
        const spinning = busy === o;
        const held = !!busy && !spinning;
        const grow = weights?.[o];
        return (
          <button
            type="button"
            // biome-ignore lint/a11y/useSemanticElements: a styled option card or segment; a native radio can't carry this 44px button layout.
            role="radio"
            key={o}
            ref={(el) => {
              refs.current[i] = el;
            }}
            tabIndex={i === at ? 0 : -1}
            aria-checked={o === value}
            aria-busy={spinning || undefined}
            aria-disabled={held || undefined}
            style={grow ? { flexGrow: grow } : undefined}
            onClick={() => !busy && o !== value && onPick(o)}
          >
            {spinning && (
              <span className="mc-spin" aria-hidden="true">
                <RefreshIcon size={14} />
              </span>
            )}
            {text?.[o] ?? cap(o)}
          </button>
        );
      })}
    </div>
  );
}

const Pend = ({ on }: { on: boolean }) => (on ? <span className="mc-pend">pending</span> : null);

function Settings({ s }: { s: MinecraftSlot }) {
  const w = useW();
  const pend = w.pendingFor(s);
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
      <TransportRow s={s} pending={pend.includes("transport")} />
    </div>
  );
}

/** Power Pack teleport, built as Cheats is, with a fixed help box under it: unlike Cheats a
 *  flip can apply live, so the mode is explained before anyone flips it. One write at a time;
 *  the switch shows what the slot reports, never what was attempted. */
function TransportRow({ s, pending }: { s: MinecraftSlot; pending: boolean }) {
  const w = useW();
  const [busy, setBusy] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const on = transportOn(s);
  async function flip() {
    if (busy !== null) return;
    const to = !on;
    setError(null);
    setBusy(to);
    setError(await w.actions.transport(s.id, to));
    setBusy(null);
  }
  return (
    <>
      <div className="mc-sw-row">
        <span className="mc-sw-l">
          <b>Power&nbsp;Pack teleport</b> <Pend on={pending && busy === null} />
          <br />
          {busy !== null ? (
            <output className="mc-keep-now">
              <Spin />
              {transportBusyText(busy)}
            </output>
          ) : (
            transportMeaning(on)
          )}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-busy={busy !== null}
          aria-label={`Power Pack teleport in ${slotName(s)}`}
          className={`mc-switch${on || busy !== null ? " on" : ""}${busy !== null ? " busy" : ""}`}
          onClick={() => void flip()}
        >
          <span className="mc-knob" />
        </button>
      </div>
      {error && (
        <div className="mc-errline mc-keep-err" role="alert">
          <AlertTriangleIcon size={16} />
          <span>{error}</span>
          <button
            type="button"
            className="mc-icon-btn"
            aria-label="Dismiss"
            onClick={() => setError(null)}
          >
            <XIcon size={18} />
          </button>
        </div>
      )}
      <p className="mc-keep-help">
        <b>Power&nbsp;Pack teleport:</b> {TRANSPORT_HELP}
      </p>
    </>
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
  const w = useW();
  if (s.last_download) {
    const b = list.find((x) => x.downloaded_at === s.last_download);
    return (
      <div className="mc-offbox ok">
        <DownloadIcon size={16} />
        <span>
          <b>Last copy off the box: {whenOf(s.last_download, w.nowMs)}</b>
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
  const later = wreckLaterText(s, w.running);

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
          <dt>Crash site</dt>
          <dd className={wreckTone(s, w.running)}>{wreckFactText(s, w.running)}</dd>
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
        {later && <p className="mc-note">{later}</p>}
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

      <div className="mc-card">
        <button type="button" className="mc-navrow" onClick={() => w.push({ kind: "map", slot })}>
          <span className="mc-navico" aria-hidden="true">
            <MapIcon size={20} />
          </span>
          <span className="mc-navtx">
            <span className="ops-card-title">Map</span>
            <span className="mc-navmeta">
              {s.active && w.running
                ? "live · explored ground and the satellite survey"
                : "explored ground and the satellite survey"}
            </span>
          </span>
          <ChevronRightIcon size={18} />
        </button>
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
              {rules && keepMode(rules) === "pack" && " · keep inventory Power\u00a0Pack"}
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
  managed = [],
  keep,
  note,
  onSet,
  onReset,
  debounceMs,
  idPrefix,
}: {
  values: Record<string, MinecraftRuleValue>;
  defaults: Record<string, MinecraftRuleValue>;
  pending: string[];
  /** Rules a box feature holds: never counted as changed, never sent by a reset. */
  managed?: string[];
  /** A saved world's keep-inventory control (Off · On · Power Pack). Absent, as in the New
   *  world form, the row offers the plain Off · On rule only. */
  keep?: KeepControl;
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
  const waiting = useRef<Record<string, () => void>>({});
  // Leaving the page mid-tap still saves the settled value, rather than dropping it.
  useEffect(
    () => () => {
      for (const t of Object.values(timers.current)) clearTimeout(t);
      for (const save of Object.values(waiting.current)) save();
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
    const was = values[r.id];
    const save = () => {
      delete timers.current[r.id];
      delete waiting.current[r.id];
      if (v !== was) onSet(r.id, v);
    };
    waiting.current[r.id] = save;
    timers.current[r.id] = setTimeout(() => {
      setDraft((d) => {
        const { [r.id]: _, ...rest } = d;
        return rest;
      });
      save();
    }, debounceMs);
  }

  const changed = (id: string) =>
    id in defaults && !managed.includes(id) && shownValue(id) !== defaults[id];
  const onPack = keep?.mode === "pack";
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
                  {onPack && g.rules.some((r) => r.id === "keepInventory") && " · Power\u00a0Pack"}
                </span>
                <ChevronRightIcon size={16} />
              </summary>
              <div className="mc-rbody">
                {rows.map((r) =>
                  r.id === "keepInventory" ? (
                    <KeepRow
                      key={r.id}
                      r={r}
                      v={values[r.id] as MinecraftRuleValue}
                      def={defaults[r.id]}
                      pending={pending.includes(r.id)}
                      idPrefix={idPrefix}
                      keep={keep}
                      onSet={(v) => onSet(r.id, v)}
                    />
                  ) : (
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
                  ),
                )}
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
          {onPack && " · keep inventory is on Power Pack, which Reset all leaves alone"}
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

/** What a saved world's keep-inventory row needs from its page. */
export interface KeepControl {
  /** What the server reports. */
  mode: KeepMode;
  /** The plain choice underneath Power Pack. */
  saved: boolean;
  /** A pick in flight and the step it's on. */
  busy: { to: KeepMode; msg: string } | null;
  error: string | null;
  onPick: (to: KeepMode) => void;
  onDismiss: () => void;
}

/** Keep inventory: Off · On · Power Pack in the row (binding mock
 *  docs/mocks/minecraft-keep-inventory/a-segmented-row.html). */
function KeepRow({
  r,
  v,
  def,
  pending,
  idPrefix,
  keep,
  onSet,
}: {
  r: RuleDef;
  v: MinecraftRuleValue;
  def: MinecraftRuleValue | undefined;
  pending: boolean;
  idPrefix: string;
  keep: KeepControl | undefined;
  onSet: (v: MinecraftRuleValue) => void;
}) {
  const mode: KeepMode = keep ? keep.mode : v === true ? "on" : "off";
  const changed = mode !== "pack" && def !== undefined && (mode === "on") !== def;
  const labelId = `${idPrefix}-keep-l`;
  return (
    <div className="mc-rule mc-keep">
      <span className="mc-rl">
        <span className="mc-rtl" id={labelId}>
          {r.label}
        </span>
        <span className="mc-rid mc-mono">{r.id}</span>
        {(pending || changed || mode === "pack") && (
          <span className="mc-rtags">
            {pending && <span className="mc-pend">pending</span>}
            {changed && def !== undefined && (
              <span className="mc-chg">changed · default {ruleWord(r, def)}</span>
            )}
            {mode === "pack" && <span className="mc-chg">held on by the Power Pack</span>}
          </span>
        )}
      </span>
      <Seg
        label={r.label}
        labelledBy={labelId}
        options={keep ? KEEP_MODES : DRAFT_KEEP_MODES}
        value={keep?.busy?.to ?? mode}
        text={KEEP_WORD}
        weights={{ pack: 1.6 }}
        busy={keep?.busy?.to ?? null}
        onPick={(to) => (keep ? keep.onPick(to) : onSet(to === "on"))}
      />
      <output className="mc-keep-now">
        {keep?.busy ? (
          <>
            <Spin />
            {keep.busy.msg}
          </>
        ) : (
          keepMeaning(mode, keep?.saved ?? false)
        )}
      </output>
      {keep?.error && (
        <div className="mc-errline mc-keep-err" role="alert">
          <AlertTriangleIcon size={16} />
          <span>{keep.error}</span>
          <button
            type="button"
            className="mc-icon-btn"
            aria-label="Dismiss"
            onClick={keep.onDismiss}
          >
            <XIcon size={18} />
          </button>
        </div>
      )}
      {keep && <p className="mc-keep-help">{KEEP_HELP}</p>}
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
        <span className="mc-rtl">{r.label}</span>
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
  const [keepBusy, setKeepBusy] = useState<KeepControl["busy"]>(null);
  const [keepErr, setKeepErr] = useState<string | null>(null);
  if (!s) return null;
  const world = s;

  /** Off · On is the plain rule; Power Pack is the slot's switch. Whatever happens, the row
   *  ends on what the server reports when re-read, not on what was attempted. */
  async function pickKeep(to: KeepMode) {
    if (!view || keepBusy) return;
    const from = keepMode(view);
    const steps = keepSteps(from, to, keepSaved(world));
    if (!steps.length) return;
    setKeepErr(null);
    let live = view.live;
    try {
      for (const st of steps) {
        setKeepBusy({ to, msg: st.msg });
        if (st.kind === "pack") {
          live = (await api.minecraftSetPowerPack(slot, st.on)).applies === "now";
        } else {
          live = (await api.minecraftSetRules(slot, { keepInventory: st.on })).live;
        }
      }
      const fresh = await api.minecraftRules(slot).catch(() => null);
      if (fresh) setView(fresh);
      w.toast(keepSavedToast(to, from, world, live));
    } catch (err) {
      const refused = err instanceof ApiError && isRefusal(err.status);
      const reason = refused ? refusalReason(err.message) : "the server didn't answer";
      const fresh = await api.minecraftRules(slot).catch(() => null);
      if (fresh) setView(fresh);
      setKeepErr(keepFailText({ to, from, now: fresh ? keepMode(fresh) : null, refused, reason }));
    } finally {
      setKeepBusy(null);
      // The slot carries the switch and the saved choice underneath it.
      void w.reload();
    }
  }

  async function save(set: Record<string, MinecraftRuleValue>, done: (v: MinecraftRules) => void) {
    if (!view) return;
    setView({ ...view, rules: { ...view.rules, ...set } });
    try {
      const next = await api.minecraftSetRules(slot, set);
      setView(next);
      done(next);
    } catch (err) {
      // Refused (409 while a job holds the world): the switch or stepper goes back.
      setView(view);
      w.setError(`Couldn't save the rule — ${message(err)}.`);
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
          managed={Object.keys(view.managed ?? {})}
          keep={{
            mode: keepMode(view),
            saved: keepSaved(s),
            busy: keepBusy,
            error: keepErr,
            onPick: (to) => void pickKeep(to),
            onDismiss: () => setKeepErr(null),
          }}
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
