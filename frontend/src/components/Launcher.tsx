// Full-screen card launcher (docs/reference/DESIGN.md "Navigation: the card
// launcher"). A navigation surface, not a modal: it owns the whole screen,
// slides up 150ms ease-out, and dismisses on swipe-down or Escape.

import {
  type PointerEvent,
  type ReactNode,
  type TouchEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { type MinecraftStatus, type MinecraftVersion, api } from "../api/client";
import { tileOf } from "../minecraft";
import { countUnviewed, loadViewed } from "../tasks/viewed";
import { useForeground } from "../visibility";
import {
  BookIcon,
  BotIcon,
  CalendarIcon,
  ChatIcon,
  CheckSquareIcon,
  CodeIcon,
  CubeIcon,
  DatabaseIcon,
  EyeOffIcon,
  FlaskIcon,
  GaugeIcon,
  GlobeIcon,
  GraphIcon,
  ImageIcon,
  ListIcon,
  MessageIcon,
  PinIcon,
  RadioIcon,
  SearchIcon,
  SettingsIcon,
  SigmaIcon,
  UsersIcon,
  XIcon,
  ZapIcon,
} from "./icons";

export type LauncherTarget =
  | "ops"
  | "automations"
  | "data"
  | "settings"
  | "llm-settings"
  | "search"
  | "research"
  | "review"
  | "entities"
  | "lists"
  | "calendar"
  | "graph"
  | "location"
  | "wiki"
  | "image"
  | "radio"
  | "intake"
  | "tasks"
  | "petcontrol"
  | "jpanel"
  | "endpoints"
  | "jcode"
  | "jlaunch"
  | "jmolt"
  | "minecraft";

interface Tile {
  title: string;
  icon: ReactNode;
  /** Present = not built yet; tile renders disabled with the phase badge. */
  phase?: string;
  target?: LauncherTarget;
}

interface Section {
  header: string;
  tiles: Tile[];
}

const SECTIONS: Section[] = [
  {
    header: "Knowledge",
    tiles: [
      { title: "Search", icon: <SearchIcon size={24} />, target: "search" },
      { title: "Research", icon: <FlaskIcon size={24} />, target: "research" },
      { title: "Wiki", icon: <BookIcon size={24} />, target: "wiki" },
      { title: "Calendar", icon: <CalendarIcon size={24} />, target: "calendar" },
      { title: "Lists", icon: <ListIcon size={24} />, target: "lists" },
      { title: "Entities", icon: <UsersIcon size={24} />, target: "entities" },
      { title: "Map", icon: <GraphIcon size={24} />, target: "graph" },
      { title: "Location", icon: <PinIcon size={24} />, target: "location" },
      { title: "Pet", icon: <BotIcon size={24} />, target: "petcontrol" },
    ],
  },
  {
    header: "Authoring",
    // Full Brain is integral to the home screen (the omnibox's Full Brain
    // mode), not a launcher tile.
    tiles: [
      { title: "Review", icon: <CheckSquareIcon size={24} />, target: "review" },
      { title: "Intake", icon: <GlobeIcon size={24} />, target: "intake" },
      { title: "Image", icon: <ImageIcon size={24} />, target: "image" },
      { title: "Code", icon: <CodeIcon size={24} />, target: "jcode" },
      { title: "Math", icon: <SigmaIcon size={24} />, target: "jlaunch" },
      { title: "Radio", icon: <RadioIcon size={24} />, target: "radio" },
    ],
  },
  {
    header: "System",
    tiles: [
      { title: "Ops", icon: <GaugeIcon size={24} />, target: "ops" },
      // The panels in the house, as one door: the messages they carry and the flasher that
      // puts firmware on them. It takes the slot the pet-face preview held — that preview
      // was a hardware validation surface, and what the owner reaches for at work is a
      // message from a four-year-old. Distinct from "Pet" (the phone remote for the wall).
      { title: "jpanel", icon: <ChatIcon size={24} />, target: "jpanel" },
      // Flashing a physical panel over the box's USB port — the same surface, opened on
      // its own tab. It keeps a tile because it is a rare-but-urgent errand done with a
      // board in your hand and a cable in the box, and hunting for a tab behind a
      // messaging screen is the wrong thing to be doing while standing there.
      { title: "Endpoints", icon: <PinIcon size={24} />, target: "endpoints" },
      { title: "Workflow", icon: <ZapIcon size={24} />, target: "automations" },
      { title: "Tasks", icon: <CheckSquareIcon size={24} />, target: "tasks" },
      { title: "Data", icon: <DatabaseIcon size={24} />, target: "data" },
      // The family's Bedrock server, lifted off Ops into its own screen the way Data was:
      // the waves after M1 (world slots, backups, remote play) all land there.
      { title: "Minecraft", icon: <CubeIcon size={24} />, target: "minecraft" },
      { title: "Settings", icon: <SettingsIcon size={24} />, target: "settings" },
      { title: "LLM", icon: <BotIcon size={24} />, target: "llm-settings" },
      { title: "jmolt", icon: <MessageIcon size={24} />, target: "jmolt" },
    ],
  },
];

const SWIPE_DOWN_PX = 96;
const EXIT_MS = 150;

// The Image tile is configuration-gated, mirroring the provider-hidden-when-unkeyed
// pattern: generate/edit 404 on a box without image hosting, so the tile is omitted
// unless `getImageSettings().enabled` is true. Fetched ONCE per session and cached in
// a module-level promise so reopening the launcher never refetches or flashes; a fetch
// failure resolves to false (tile hidden), never throwing.
//
// The result is also mirrored to localStorage (device-local, like the tasks/viewed
// markers). Enablement is stable per box, so the last-known value hydrates the tile
// state SYNCHRONOUSLY on mount — without it, every fresh page load painted the grid
// without Image, then popped it in a tick later when this async fetch resolved, so the
// icon count visibly jumped N→N+1 on the first open. The fetch still runs to pick up a
// genuine change; it just no longer decides the count from scratch each load.
const IMAGE_ENABLED_KEY = "jb.image.enabled";

/** Last-known image enablement, or null when this device has never resolved it. */
function loadImageEnabledCached(): boolean | null {
  try {
    const raw = localStorage.getItem(IMAGE_ENABLED_KEY);
    return raw === null ? null : raw === "true";
  } catch {
    return null;
  }
}

function saveImageEnabledCached(enabled: boolean): void {
  try {
    localStorage.setItem(IMAGE_ENABLED_KEY, enabled ? "true" : "false");
  } catch {
    // best-effort; a dropped marker just re-flashes the tile once on the next load
  }
}

let imageEnabledPromise: Promise<boolean> | null = null;
function fetchImageEnabled(): Promise<boolean> {
  imageEnabledPromise ??= api
    .getImageSettings()
    .then((s) => s.enabled === true)
    .catch(() => false);
  return imageEnabledPromise;
}

// The Math (jlaunch) tile is configuration-gated exactly like Image: the /jlaunch routes
// 404 on a box without the launcher enabled, so probe /jlaunch/specs once per session and
// hide the tile unless it resolves. Same session-cached promise + device-local hydration so
// reopening never refetches or flashes; a fetch failure (incl. the 404) resolves to false.
const JLAUNCH_ENABLED_KEY = "jb.jlaunch.enabled";

function loadJlaunchEnabledCached(): boolean | null {
  try {
    const raw = localStorage.getItem(JLAUNCH_ENABLED_KEY);
    return raw === null ? null : raw === "true";
  } catch {
    return null;
  }
}

function saveJlaunchEnabledCached(enabled: boolean): void {
  try {
    localStorage.setItem(JLAUNCH_ENABLED_KEY, enabled ? "true" : "false");
  } catch {
    // best-effort; a dropped marker just re-flashes the tile once on the next load
  }
}

let jlaunchEnabledPromise: Promise<boolean> | null = null;
function fetchJlaunchEnabled(): Promise<boolean> {
  jlaunchEnabledPromise ??= api
    .jlaunchSpecs()
    .then(() => true)
    .catch(() => false);
  return jlaunchEnabledPromise;
}
// Owner-hidden tiles. The grid outgrew one screen, so a long-press greys a tile and
// it drops out 5s later; a second long-press inside that window keeps it. Hidden
// tiles collect behind a "Hidden" tile that expands them in place, where a long-press
// restores one. Device-local like the gates above: it's a per-phone layout choice.
const HIDDEN_KEY = "jb.launcher.hidden";
const LONG_PRESS_MS = 500;
const HIDE_GRACE_MS = 5_000;
// A finger that drifts this far is scrolling or swiping, not pressing.
const LONG_PRESS_SLOP_PX = 10;

function loadHidden(): Set<LauncherTarget> {
  try {
    const raw = localStorage.getItem(HIDDEN_KEY);
    const parsed: unknown = raw === null ? [] : JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? (parsed as LauncherTarget[]) : []);
  } catch {
    return new Set();
  }
}

function saveHidden(hidden: Set<LauncherTarget>): void {
  try {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hidden]));
  } catch {
    // best-effort; a dropped write just brings the tile back on the next load
  }
}

// The Review badge polls while the launcher is open so it reads live — new
// holds tick up, resolved ones clear — without reopening the menu. Human/
// analysis pace, so a light interval; the launcher is only mounted while open.
const REVIEW_POLL_MS = 10_000;

interface LauncherProps {
  open: boolean;
  /** False while a card is stacked over the launcher: it stays mounted (for the
   * reveal beneath the card) but is off-screen, so the badge poll pauses. */
  active?: boolean;
  onClose: () => void;
  onNavigate: (target: LauncherTarget) => void;
}

export function Launcher({ open, active = true, onClose, onNavigate }: LauncherProps) {
  // Stay mounted through the exit animation, then unmount.
  const [closing, setClosing] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const touchStartY = useRef<number | null>(null);
  const wasOpen = useRef(open);
  // A live count drives the Review tile badge: an immediate fetch on open, then
  // a poll while the launcher is the surface on screen. Failures just leave the
  // badge at its last value.
  const [reviewCount, setReviewCount] = useState<number | null>(null);
  // The Tasks tile badge: tasks whose latest run hasn't been opened on this device
  // (the same device-local "unviewed" state that drives each card's NEW band), not
  // merely runs since Tasks was last opened — opening the screen no longer clears it.
  const [taskCount, setTaskCount] = useState<number | null>(null);
  // The Minecraft tile's dot and word. Unknown (no dot) until read, and on any failure.
  const [mc, setMc] = useState<{
    status: MinecraftStatus | null;
    version: MinecraftVersion | null;
  }>({ status: null, version: null });
  // Config gate for the Image tile. Hydrated synchronously from the last-known
  // device-local value so the tile set is stable from the first paint (no N→N+1
  // count jump on open); null only on a device that has never resolved it, where
  // the tile stays hidden until the fetch below confirms enablement.
  const [imageEnabled, setImageEnabled] = useState<boolean | null>(loadImageEnabledCached);
  // Config gate for the Math (jlaunch) tile — same pattern as Image.
  const [jlaunchEnabled, setJlaunchEnabled] = useState<boolean | null>(loadJlaunchEnabledCached);
  // Two gates quiet the poll: a backgrounded PWA, and a launcher buried under a
  // card. Returning to either re-runs this effect — an immediate refetch, then
  // re-arm — so the badge is current the moment the menu is back on screen.
  const foreground = useForeground();
  const [hidden, setHidden] = useState<Set<LauncherTarget>>(loadHidden);
  // Tiles greyed by a long-press, each with the timer that will hide it.
  const [pending, setPending] = useState<Set<LauncherTarget>>(() => new Set());
  const pendingTimers = useRef(new Map<LauncherTarget, ReturnType<typeof setTimeout>>());
  const [showHidden, setShowHidden] = useState(false);
  const press = useRef<{
    timer: ReturnType<typeof setTimeout>;
    x: number;
    y: number;
  } | null>(null);
  // Set when a press fires, so the click that follows the release doesn't navigate.
  const longPressed = useRef(false);

  // Closing the launcher inside the grace window still honours the hide: the owner
  // asked for it and didn't take it back.
  useEffect(() => {
    const timers = pendingTimers.current;
    return () => {
      if (timers.size === 0) return;
      const next = loadHidden();
      for (const [target, timer] of timers) {
        clearTimeout(timer);
        next.add(target);
      }
      timers.clear();
      saveHidden(next);
    };
  }, []);

  // Resolve image-hosting enablement when the launcher is the surface on screen.
  // The fetch is cached at module scope, so this fires at most once per session
  // (reopening reads the resolved promise — no refetch, no flash); a buried/
  // backgrounded launcher defers it, like the badge poll.
  useEffect(() => {
    if (!open || !active || !foreground) return;
    let stale = false;
    fetchImageEnabled().then((on) => {
      saveImageEnabledCached(on);
      if (!stale) setImageEnabled(on);
    });
    fetchJlaunchEnabled().then((on) => {
      saveJlaunchEnabledCached(on);
      if (!stale) setJlaunchEnabled(on);
    });
    return () => {
      stale = true;
    };
  }, [open, active, foreground]);

  useEffect(() => {
    if (!open || !active || !foreground) return;
    let stale = false;
    const refresh = () => {
      // The Review tile's badge is the whole signal for both tabs (D4/D5 — no push, no
      // nagging count anywhere else), so it sums the wiki findings and the notes tab's
      // waiting rows. A first pass still reading is not waiting on anyone and is
      // excluded, the same rule the tab's own count pill uses.
      Promise.all([api.reviewQueue(), api.notesInbox().catch(() => ({ items: [] }))])
        .then(([queue, notes]) => {
          if (stale) return;
          setReviewCount(queue.items.length + notes.items.filter((r) => !r.live).length);
        })
        .catch(() => {});
      // Count tasks with an unviewed latest run — recomputed each poll against the
      // device-local viewed markers, so returning from a task's session (which stamps
      // its marker) drops the badge on the next tick.
      api
        .tasks()
        .then((tasks) => {
          if (!stale) setTaskCount(countUnviewed(tasks, loadViewed()));
        })
        .catch(() => {});
    };
    const refreshMc = () =>
      Promise.all([api.minecraftStatus(), api.minecraftVersion(false).catch(() => null)])
        .then(([status, version]) => {
          if (!stale) setMc({ status, version });
        })
        .catch(() => {
          if (!stale) setMc({ status: null, version: null });
        });
    refresh();
    void refreshMc();
    const interval = setInterval(() => {
      refresh();
      void refreshMc();
    }, REVIEW_POLL_MS);
    return () => {
      stale = true;
      clearInterval(interval);
    };
  }, [open, active, foreground]);

  // The retreat is driven by `open` going false — from the X/grab, swipe-down,
  // Escape, OR the platform back gesture (App clears launcherOpen). Closing this
  // controlled way drops the navigation depth immediately, so it stays in
  // lockstep with history and back never falls through to exit the app
  // mid-animation.
  useEffect(() => {
    const justClosed = wasOpen.current && !open;
    wasOpen.current = open;
    if (!justClosed) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    setClosing(true);
    const t = setTimeout(() => setClosing(false), EXIT_MS);
    return () => clearTimeout(t);
  }, [open]);

  // Focus moves in when the launcher OPENS, once. App passes an inline onClose, so keying
  // this on it re-ran the focus on every App render and pulled focus out of whatever sat on
  // top — a card's confirm Dialog included.
  useEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open && !closing) return null;

  const mcTile = tileOf(mc.status, mc.version);

  // Configuration gates (not owner choices): Image and Math only exist where enabled.
  const gated = (tile: Tile) =>
    (tile.target !== "image" || imageEnabled === true) &&
    (tile.target !== "jlaunch" || jlaunchEnabled === true);
  const hiddenTiles = SECTIONS.flatMap((s) => s.tiles).filter(
    (tile) => gated(tile) && tile.target !== undefined && hidden.has(tile.target),
  );
  const hiddenOpen = showHidden && hiddenTiles.length > 0;

  function setHiddenAndSave(update: (next: Set<LauncherTarget>) => void) {
    setHidden((prev) => {
      const next = new Set(prev);
      update(next);
      saveHidden(next);
      return next;
    });
  }

  function dropPending(target: LauncherTarget) {
    clearTimeout(pendingTimers.current.get(target));
    pendingTimers.current.delete(target);
    setPending((prev) => {
      const next = new Set(prev);
      next.delete(target);
      return next;
    });
  }

  function onLongPress(target: LauncherTarget) {
    if (hidden.has(target)) {
      setHiddenAndSave((next) => next.delete(target));
    } else if (pendingTimers.current.has(target)) {
      dropPending(target);
    } else {
      setPending((prev) => new Set(prev).add(target));
      pendingTimers.current.set(
        target,
        setTimeout(() => {
          dropPending(target);
          setHiddenAndSave((next) => next.add(target));
        }, HIDE_GRACE_MS),
      );
    }
  }

  function cancelPress() {
    if (press.current) clearTimeout(press.current.timer);
    press.current = null;
  }

  function onPressStart(event: PointerEvent, target: LauncherTarget) {
    cancelPress();
    longPressed.current = false;
    press.current = {
      x: event.clientX,
      y: event.clientY,
      timer: setTimeout(() => {
        press.current = null;
        longPressed.current = true;
        navigator.vibrate?.(10);
        onLongPress(target);
      }, LONG_PRESS_MS),
    };
  }

  function onPressMove(event: PointerEvent) {
    const p = press.current;
    if (p && Math.hypot(event.clientX - p.x, event.clientY - p.y) > LONG_PRESS_SLOP_PX) {
      cancelPress();
    }
  }

  function renderTile(tile: Tile) {
    const target = tile.target;
    return (
      <button
        key={tile.title}
        type="button"
        className={`tile${target && pending.has(target) ? " tile-pending" : ""}`}
        disabled={tile.phase !== undefined}
        onPointerDown={target ? (e) => onPressStart(e, target) : undefined}
        onPointerMove={onPressMove}
        onPointerUp={cancelPress}
        onPointerCancel={cancelPress}
        onPointerLeave={cancelPress}
        // The long-press is ours: no OS context menu / callout on top of it.
        onContextMenu={(e) => e.preventDefault()}
        onClick={() => {
          if (longPressed.current) {
            longPressed.current = false;
            return;
          }
          if (target) {
            // Stay open beneath the card: the card slides up over
            // the launcher, and dismissing it reveals us again.
            onNavigate(target);
          }
        }}
      >
        <span className="tile-icon">{tile.icon}</span>
        <span className="tile-title">{tile.title}</span>
        {tile.phase && <span className="phase-badge">{tile.phase}</span>}
        {target === "review" && reviewCount !== null && reviewCount > 0 && (
          <span className="tile-badge">{reviewCount}</span>
        )}
        {target === "tasks" && taskCount !== null && taskCount > 0 && (
          <span className="tile-badge">{taskCount}</span>
        )}
        {target === "minecraft" && mcTile && (
          <>
            <span className={`mc-tile-dot ${mcTile.level}`} aria-hidden="true" />
            <span className="mc-tile-sub">{mcTile.word}</span>
          </>
        )}
      </button>
    );
  }

  function onTouchStart(event: TouchEvent) {
    // Owner-settled: a down-swipe anywhere on the launcher dismisses it,
    // regardless of scroll position (pull-to-refresh is suppressed in CSS
    // so the browser can't hijack the gesture).
    touchStartY.current = event.touches[0]?.clientY ?? null;
  }

  function onTouchMove(event: TouchEvent) {
    const startY = touchStartY.current;
    const y = event.touches[0]?.clientY;
    if (startY !== null && y !== undefined && y - startY > SWIPE_DOWN_PX) {
      touchStartY.current = null;
      onClose();
    }
  }

  return (
    // A nav surface, not a modal (docs/reference/DESIGN.md) — hence <nav>, no scrim.
    <nav
      className={`launcher${closing ? " launcher-closing" : ""}`}
      ref={panelRef}
      tabIndex={-1}
      aria-label="Launcher"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
    >
      {/* Gestures proved unreliable on real devices — the visible close
          affordances are the primary path; swipes are an enhancement. */}
      <div className="launcher-head">
        <button
          type="button"
          className="launcher-grab"
          onClick={onClose}
          aria-label="Close launcher"
        >
          <span className="launcher-handle" aria-hidden="true" />
        </button>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close launcher">
          <XIcon size={22} />
        </button>
      </div>
      {SECTIONS.map((section, i) => (
        <section key={section.header} className="launcher-section">
          <h2 className="section-header">{section.header}</h2>
          <div className="tile-grid">
            {section.tiles
              .filter((tile) => gated(tile) && !(tile.target && hidden.has(tile.target)))
              .map(renderTile)}
            {i === SECTIONS.length - 1 && hiddenTiles.length > 0 && (
              <button
                type="button"
                className="tile"
                aria-expanded={hiddenOpen}
                onClick={() => setShowHidden((v) => !v)}
              >
                <span className="tile-icon">
                  <EyeOffIcon size={24} />
                </span>
                <span className="tile-title">Hidden</span>
                <span className="tile-sub">{hiddenTiles.length}</span>
              </button>
            )}
          </div>
        </section>
      ))}
      {hiddenOpen && (
        <section className="launcher-section">
          <h2 className="section-header">Hidden · long-press to restore</h2>
          <div className="tile-grid">{hiddenTiles.map(renderTile)}</div>
        </section>
      )}
    </nav>
  );
}
