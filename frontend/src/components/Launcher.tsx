// Full-screen card launcher (docs/reference/DESIGN.md "Navigation: the card
// launcher"). A navigation surface, not a modal: it owns the whole screen,
// slides up 150ms ease-out, and dismisses on swipe-down or Escape.

import {
  type PointerEvent,
  type ReactNode,
  type TouchEvent,
  useEffect,
  useLayoutEffect,
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
  PlusIcon,
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
  target: LauncherTarget;
}

// One flat grid in the default order; the owner rearranges it per device (see
// ORDER_KEY below). Full Brain is integral to the home screen (the omnibox's Full
// Brain mode), not a launcher tile.
const TILES: Tile[] = [
  { title: "Search", icon: <SearchIcon size={24} />, target: "search" },
  { title: "Research", icon: <FlaskIcon size={24} />, target: "research" },
  { title: "Wiki", icon: <BookIcon size={24} />, target: "wiki" },
  { title: "Calendar", icon: <CalendarIcon size={24} />, target: "calendar" },
  { title: "Lists", icon: <ListIcon size={24} />, target: "lists" },
  { title: "Entities", icon: <UsersIcon size={24} />, target: "entities" },
  { title: "Map", icon: <GraphIcon size={24} />, target: "graph" },
  { title: "Location", icon: <PinIcon size={24} />, target: "location" },
  { title: "Pet", icon: <BotIcon size={24} />, target: "petcontrol" },
  { title: "Review", icon: <CheckSquareIcon size={24} />, target: "review" },
  { title: "Intake", icon: <GlobeIcon size={24} />, target: "intake" },
  { title: "Image", icon: <ImageIcon size={24} />, target: "image" },
  { title: "Code", icon: <CodeIcon size={24} />, target: "jcode" },
  { title: "Math", icon: <SigmaIcon size={24} />, target: "jlaunch" },
  { title: "Radio", icon: <RadioIcon size={24} />, target: "radio" },
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
// The owner arranges the grid like a phone home screen: a long-press enters edit
// mode (tiles wiggle), where a tile drags to a new slot and its × hides it. Hidden
// tiles collect behind a "Hidden" tile that expands them in place, with a + to bring
// one back to the slot it left. Order and hidden set are device-local: a per-phone
// layout choice, not a box setting.
const HIDDEN_KEY = "jb.launcher.hidden";
const ORDER_KEY = "jb.launcher.order";
const LONG_PRESS_MS = 500;
// A finger that drifts this far before the press fires is scrolling, not pressing.
const LONG_PRESS_SLOP_PX = 10;
const KNOWN = new Set<string>(TILES.map((t) => t.target));

function loadTargets(key: string): LauncherTarget[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(key) ?? "[]");
    return Array.isArray(parsed)
      ? parsed.filter((t): t is LauncherTarget => typeof t === "string" && KNOWN.has(t))
      : [];
  } catch {
    return [];
  }
}

function saveTargets(key: string, targets: Iterable<LauncherTarget>): void {
  try {
    localStorage.setItem(key, JSON.stringify([...targets]));
  } catch {
    // best-effort; a dropped write just restores the default layout on the next load
  }
}

/** The saved order, with any tile added since appended at the end. */
function loadOrder(): LauncherTarget[] {
  const saved = loadTargets(ORDER_KEY);
  const seen = new Set(saved);
  return [...saved, ...TILES.map((t) => t.target).filter((t) => !seen.has(t))];
}

const TILE_BY_TARGET = new Map(TILES.map((t) => [t.target, t]));

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
  /** Edit mode, when the shell owns it: it is a back-gesture layer there, so Android back
   * leaves edit mode before it closes the launcher. Uncontrolled when omitted. */
  editing?: boolean;
  onEditingChange?: (editing: boolean) => void;
}

export function Launcher({
  open,
  active = true,
  onClose,
  onNavigate,
  editing: editingProp,
  onEditingChange,
}: LauncherProps) {
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
  const [hidden, setHidden] = useState<Set<LauncherTarget>>(() => new Set(loadTargets(HIDDEN_KEY)));
  const [order, setOrder] = useState<LauncherTarget[]>(loadOrder);
  const [ownEditing, setOwnEditing] = useState(false);
  const editing = editingProp ?? ownEditing;
  const setEditing = onEditingChange ?? setOwnEditing;
  const [showHidden, setShowHidden] = useState(false);
  const [dragging, setDragging] = useState<LauncherTarget | null>(null);
  const tileEls = useRef(new Map<LauncherTarget, HTMLElement>());
  // The pending long-press, and once it fires (or in edit mode at once) the drag:
  // where the finger grabbed the tile, so the tile tracks it without jumping.
  const press = useRef<{
    target: LauncherTarget;
    pointerId: number;
    timer: ReturnType<typeof setTimeout> | null;
    startX: number;
    startY: number;
    x: number;
    y: number;
    grabX: number;
    grabY: number;
  } | null>(null);
  // Set when a press turns into edit/drag, so the click after the release doesn't navigate.
  const suppressClick = useRef(false);

  // Edit mode is a moment, not a setting: leaving the launcher ends it.
  // A pending press dies with it too, or its timer would switch edit mode on behind a
  // launcher that a swipe, ✕ or back just closed.
  useEffect(() => {
    if (!open) {
      const p = press.current;
      if (p?.timer) clearTimeout(p.timer);
      press.current = null;
      setDragging(null);
      setEditing(false);
      setShowHidden(false);
    }
  }, [open]);

  useEffect(
    () => () => {
      const p = press.current;
      if (p?.timer) clearTimeout(p.timer);
    },
    [],
  );

  // React's touch listeners are passive, so only a native one can stop the page
  // scrolling (and the browser cancelling our pointer stream) under a dragged tile.
  useEffect(() => {
    const nav = panelRef.current;
    if (!nav) return;
    const onMove = (event: globalThis.TouchEvent) => {
      if (press.current && press.current.timer === null) event.preventDefault();
    };
    nav.addEventListener("touchmove", onMove, { passive: false });
    return () => nav.removeEventListener("touchmove", onMove);
  });

  // The dragged tile follows the finger as a transform off its CURRENT slot, so it is
  // re-applied after every reorder moves that slot.
  useLayoutEffect(() => {
    if (dragging) placeDragged();
  });

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
      if (event.key !== "Escape") return;
      if (editing) setEditing(false);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, editing]);

  if (!open && !closing) return null;

  const mcTile = tileOf(mc.status, mc.version);

  // Configuration gates (not owner choices): Image and Math only exist where enabled.
  const gated = (target: LauncherTarget) =>
    (target !== "image" || imageEnabled === true) &&
    (target !== "jlaunch" || jlaunchEnabled === true);
  const shown = order.filter((t) => gated(t) && !hidden.has(t));
  const hiddenShown = order.filter((t) => gated(t) && hidden.has(t));
  const hiddenOpen = (showHidden || editing) && hiddenShown.length > 0;

  function setHiddenAndSave(update: (next: Set<LauncherTarget>) => void) {
    setHidden((prev) => {
      const next = new Set(prev);
      update(next);
      saveTargets(HIDDEN_KEY, next);
      return next;
    });
  }

  function placeDragged() {
    const p = press.current;
    const el = p && tileEls.current.get(p.target);
    const slot = el?.parentElement;
    if (!p || !el || !slot) return;
    const box = slot.getBoundingClientRect();
    el.style.transform = `translate(${p.x - p.grabX - box.left}px, ${p.y - p.grabY - box.top}px) scale(1.08)`;
  }

  function startDrag() {
    const p = press.current;
    const el = p && tileEls.current.get(p.target);
    if (!p || !el) return;
    p.timer = null;
    const rect = el.getBoundingClientRect();
    p.grabX = p.x - rect.left;
    p.grabY = p.y - rect.top;
    el.setPointerCapture?.(p.pointerId);
    setDragging(p.target);
  }

  function endPress() {
    const p = press.current;
    if (!p) return;
    if (p.timer) clearTimeout(p.timer);
    const el = tileEls.current.get(p.target);
    if (el) el.style.transform = "";
    press.current = null;
    setDragging(null);
  }

  function onPressStart(event: PointerEvent, target: LauncherTarget) {
    if (event.button !== 0) return;
    endPress();
    suppressClick.current = false;
    const base = { target, pointerId: event.pointerId, grabX: 0, grabY: 0 };
    const at = { startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY };
    if (editing) {
      // Already arranging: any press is a pickup.
      press.current = { ...base, ...at, timer: null };
      startDrag();
      return;
    }
    press.current = {
      ...base,
      ...at,
      timer: setTimeout(() => {
        suppressClick.current = true;
        // The owner's Android WebView holds no VIBRATE permission, where this is a no-op
        // at best and has thrown on some WebView builds — a missing buzz must never
        // swallow the long-press.
        try {
          navigator.vibrate?.(10);
        } catch {
          // the wiggle is the cue on its own
        }
        setEditing(true);
        startDrag();
      }, LONG_PRESS_MS),
    };
  }

  function onPressMove(event: PointerEvent) {
    const p = press.current;
    if (!p || p.pointerId !== event.pointerId) return;
    p.x = event.clientX;
    p.y = event.clientY;
    if (p.timer) {
      if (Math.hypot(p.x - p.startX, p.y - p.startY) > LONG_PRESS_SLOP_PX) endPress();
      return;
    }
    suppressClick.current = true;
    placeDragged();
    // Live reorder: the tile under the finger yields its slot, like a phone home screen.
    for (const [target, el] of tileEls.current) {
      if (target === p.target || hidden.has(target)) continue;
      const r = el.getBoundingClientRect();
      if (p.x < r.left || p.x > r.right || p.y < r.top || p.y > r.bottom) continue;
      setOrder((prev) => {
        const from = prev.indexOf(p.target);
        const over = prev.indexOf(target);
        const next = prev.filter((t) => t !== p.target);
        next.splice(next.indexOf(target) + (from < over ? 1 : 0), 0, p.target);
        saveTargets(ORDER_KEY, next);
        return next;
      });
      break;
    }
  }

  function renderTile(target: LauncherTarget, isHidden: boolean) {
    const tile = TILE_BY_TARGET.get(target);
    if (!tile) return null;
    const cls = ["tile", editing && "tile-editing", dragging === target && "tile-dragging"];
    return (
      <div key={target} className="tile-slot">
        <button
          ref={(el) => {
            if (el && !isHidden) tileEls.current.set(target, el);
            else tileEls.current.delete(target);
          }}
          type="button"
          className={cls.filter(Boolean).join(" ")}
          onPointerDown={isHidden ? undefined : (e) => onPressStart(e, target)}
          onPointerMove={onPressMove}
          onPointerUp={endPress}
          onPointerCancel={endPress}
          onPointerLeave={() => {
            // Only an unfired press dies on leave; a drag holds pointer capture.
            if (press.current?.timer) endPress();
          }}
          // The long-press is ours: no OS context menu / callout on top of it.
          onContextMenu={(e) => e.preventDefault()}
          onClick={() => {
            if (suppressClick.current) {
              suppressClick.current = false;
              return;
            }
            // In edit mode a tap does nothing, as on a phone; Done leaves.
            if (editing) return;
            // Stay open beneath the card: the card slides up over
            // the launcher, and dismissing it reveals us again.
            onNavigate(target);
          }}
        >
          <span className="tile-icon">{tile.icon}</span>
          <span className="tile-title">{tile.title}</span>
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
        {editing && (
          <button
            type="button"
            className="tile-edit-btn"
            aria-label={`${isHidden ? "Show" : "Hide"} ${tile.title}`}
            onClick={() =>
              setHiddenAndSave((next) => (isHidden ? next.delete(target) : next.add(target)))
            }
          >
            {isHidden ? <PlusIcon size={14} /> : <XIcon size={14} />}
          </button>
        )}
      </div>
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
    // Arranging owns the finger: a held tile or edit mode never reads as a dismiss
    // (Done, ✕ and back still close). A press still waiting to become a long-press
    // doesn't block it — a swipe that starts on a tile is a swipe.
    if (editing || (press.current && press.current.timer === null)) return;
    if (startY !== null && y !== undefined && y - startY > SWIPE_DOWN_PX) {
      touchStartY.current = null;
      endPress();
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
        {editing && (
          <button type="button" className="launcher-done" onClick={() => setEditing(false)}>
            Done
          </button>
        )}
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
      <div className="tile-grid launcher-grid">
        {shown.map((t) => renderTile(t, false))}
        {hiddenShown.length > 0 && !editing && (
          <div className="tile-slot">
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
              <span className="tile-sub">{hiddenShown.length}</span>
            </button>
          </div>
        )}
      </div>
      {hiddenOpen && (
        <section className="launcher-hidden" aria-label="Hidden tiles">
          <div className="launcher-hidden-rule" aria-hidden="true" />
          <div className="tile-grid">{hiddenShown.map((t) => renderTile(t, true))}</div>
        </section>
      )}
    </nav>
  );
}
