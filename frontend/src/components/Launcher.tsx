// Full-screen card launcher (docs/DESIGN.md "Navigation: the card
// launcher"). A navigation surface, not a modal: it owns the whole screen,
// slides up 150ms ease-out, and dismisses on swipe-down or Escape.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import {
  BookIcon,
  BotIcon,
  CalendarIcon,
  CheckSquareIcon,
  FlaskIcon,
  GaugeIcon,
  GraphIcon,
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
  | "settings"
  | "llm-settings"
  | "search"
  | "review"
  | "entities"
  | "lists"
  | "calendar"
  | "graph"
  | "location"
  | "wiki";

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
    tiles: [{ title: "Review", icon: <CheckSquareIcon size={24} />, target: "review" }],
  },
  {
    header: "System",
    tiles: [
      { title: "Ops", icon: <GaugeIcon size={24} />, target: "ops" },
      { title: "Workflow", icon: <ZapIcon size={24} />, target: "automations" },
      { title: "Settings", icon: <SettingsIcon size={24} />, target: "settings" },
      { title: "LLM", icon: <BotIcon size={24} />, target: "llm-settings" },
    ],
  },
];

const SWIPE_DOWN_PX = 48;
const EXIT_MS = 150;
// The Review badge polls while the launcher is open so it reads live — new
// holds tick up, resolved ones clear — without reopening the menu. Human/
// analysis pace, so a light interval; the launcher is only mounted while open.
const REVIEW_POLL_MS = 10_000;

interface LauncherProps {
  open: boolean;
  onClose: () => void;
  onNavigate: (target: LauncherTarget) => void;
}

export function Launcher({ open, onClose, onNavigate }: LauncherProps) {
  // Stay mounted through the exit animation, then unmount.
  const [closing, setClosing] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const touchStartY = useRef<number | null>(null);
  const wasOpen = useRef(open);
  // A live count drives the Review tile badge: an immediate fetch on open, then
  // a poll and a refresh whenever the tab regains focus, so it stays current
  // while the launcher sits open (including beneath the Review card). Failures
  // just leave the badge at its last value.
  const [reviewCount, setReviewCount] = useState<number | null>(null);

  useEffect(() => {
    if (!open) return;
    let stale = false;
    const refresh = () =>
      api
        .reviewQueue()
        .then((queue) => {
          if (!stale) setReviewCount(queue.items.length);
        })
        .catch(() => {});
    refresh();
    const interval = setInterval(refresh, REVIEW_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stale = true;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [open]);

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
            {section.tiles.map((tile) => (
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
              </button>
            ))}
          </div>
        </section>
      ))}
    </nav>
  );
}
