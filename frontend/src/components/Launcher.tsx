// Full-screen card launcher (docs/DESIGN.md "Navigation: the card
// launcher"). A navigation surface, not a modal: it owns the whole screen,
// slides up 150ms ease-out, and dismisses on swipe-down or Escape.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useForeground } from "../visibility";
import {
  BookIcon,
  BotIcon,
  CalendarIcon,
  CheckSquareIcon,
  CodeIcon,
  DatabaseIcon,
  FlaskIcon,
  GaugeIcon,
  GlobeIcon,
  GraphIcon,
  ImageIcon,
  ListIcon,
  PinIcon,
  SearchIcon,
  SettingsIcon,
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
  | "review"
  | "entities"
  | "lists"
  | "calendar"
  | "graph"
  | "location"
  | "wiki"
  | "image"
  | "intake"
  | "tasks"
  | "jcode";

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
      { title: "Wiki", icon: <BookIcon size={24} />, target: "wiki" },
      { title: "Calendar", icon: <CalendarIcon size={24} />, target: "calendar" },
      { title: "Lists", icon: <ListIcon size={24} />, target: "lists" },
      { title: "Entities", icon: <UsersIcon size={24} />, target: "entities" },
      { title: "Map", icon: <GraphIcon size={24} />, target: "graph" },
      { title: "Location", icon: <PinIcon size={24} />, target: "location" },
      { title: "Labs", icon: <FlaskIcon size={24} />, phase: "P7" },
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
    ],
  },
  {
    header: "System",
    tiles: [
      { title: "Ops", icon: <GaugeIcon size={24} />, target: "ops" },
      { title: "Workflow", icon: <ZapIcon size={24} />, target: "automations" },
      { title: "Tasks", icon: <CheckSquareIcon size={24} />, target: "tasks" },
      { title: "Data", icon: <DatabaseIcon size={24} />, target: "data" },
      { title: "Settings", icon: <SettingsIcon size={24} />, target: "settings" },
      { title: "LLM", icon: <BotIcon size={24} />, target: "llm-settings" },
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
let imageEnabledPromise: Promise<boolean> | null = null;
function fetchImageEnabled(): Promise<boolean> {
  imageEnabledPromise ??= api
    .getImageSettings()
    .then((s) => s.enabled === true)
    .catch(() => false);
  return imageEnabledPromise;
}
// The Tasks tile badge counts runs since the owner last opened Tasks; that marker
// is stamped by TasksScreen on open and read here. Same key on both sides.
export const TASKS_SEEN_KEY = "jb.tasks.seenAt";
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
  // The Tasks tile badge: runs that finished since the owner last opened Tasks.
  const [taskCount, setTaskCount] = useState<number | null>(null);
  // Config gate for the Image tile (cached once per session). Null until resolved,
  // so the tile only appears when image hosting is confirmed enabled.
  const [imageEnabled, setImageEnabled] = useState<boolean | null>(null);
  // Two gates quiet the poll: a backgrounded PWA, and a launcher buried under a
  // card. Returning to either re-runs this effect — an immediate refetch, then
  // re-arm — so the badge is current the moment the menu is back on screen.
  const foreground = useForeground();

  // Resolve image-hosting enablement when the launcher is the surface on screen.
  // The fetch is cached at module scope, so this fires at most once per session
  // (reopening reads the resolved promise — no refetch, no flash); a buried/
  // backgrounded launcher defers it, like the badge poll.
  useEffect(() => {
    if (!open || !active || !foreground) return;
    let stale = false;
    fetchImageEnabled().then((on) => {
      if (!stale) setImageEnabled(on);
    });
    return () => {
      stale = true;
    };
  }, [open, active, foreground]);

  useEffect(() => {
    if (!open || !active || !foreground) return;
    let stale = false;
    const refresh = () => {
      api
        .reviewQueue()
        .then((queue) => {
          if (!stale) setReviewCount(queue.items.length);
        })
        .catch(() => {});
      // Seed the "seen" marker on first run so the badge has a baseline; then count
      // runs started since the owner last opened Tasks.
      let seen = localStorage.getItem(TASKS_SEEN_KEY);
      if (seen === null) {
        seen = new Date().toISOString();
        try {
          localStorage.setItem(TASKS_SEEN_KEY, seen);
        } catch {
          // best-effort; a missing marker just re-seeds next tick
        }
      }
      api
        .taskRunActivity(seen)
        .then((count) => {
          if (!stale) setTaskCount(count);
        })
        .catch(() => {});
    };
    refresh();
    const interval = setInterval(refresh, REVIEW_POLL_MS);
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

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open && !closing) return null;

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
    // A nav surface, not a modal (docs/DESIGN.md) — hence <nav>, no scrim.
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
      {SECTIONS.map((section) => (
        <section key={section.header} className="launcher-section">
          <h2 className="section-header">{section.header}</h2>
          <div className="tile-grid">
            {section.tiles
              // Omit the Image tile entirely until hosting is confirmed enabled —
              // configuration-gated, not an unbuilt phase (so no disabled badge).
              .filter((tile) => tile.target !== "image" || imageEnabled === true)
              .map((tile) => (
                <button
                  key={tile.title}
                  type="button"
                  className="tile"
                  disabled={tile.phase !== undefined}
                  onClick={() => {
                    if (tile.target) {
                      // Stay open beneath the card: the card slides up over
                      // the launcher, and dismissing it reveals us again.
                      onNavigate(tile.target);
                    }
                  }}
                >
                  <span className="tile-icon">{tile.icon}</span>
                  <span className="tile-title">{tile.title}</span>
                  {tile.phase && <span className="phase-badge">{tile.phase}</span>}
                  {tile.target === "review" && reviewCount !== null && reviewCount > 0 && (
                    <span className="tile-badge">{reviewCount}</span>
                  )}
                  {tile.target === "tasks" && taskCount !== null && taskCount > 0 && (
                    <span className="tile-badge">{taskCount}</span>
                  )}
                </button>
              ))}
          </div>
        </section>
      ))}
    </nav>
  );
}
