// Full-screen card launcher (docs/DESIGN.md "Navigation: the card
// launcher"). A navigation surface, not a modal: it owns the whole screen,
// slides up 150ms ease-out, and dismisses on swipe-down or Escape.

import { type ReactNode, type TouchEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  BookIcon,
  CalendarIcon,
  ChatIcon,
  CheckSquareIcon,
  FlaskIcon,
  GaugeIcon,
  ListIcon,
  SearchIcon,
  SettingsIcon,
  UsersIcon,
  XIcon,
} from "./icons";

export type LauncherTarget = "ops" | "settings";

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
      { title: "Search", icon: <SearchIcon size={24} />, phase: "P2" },
      { title: "Wiki", icon: <BookIcon size={24} />, phase: "P6" },
      { title: "Calendar", icon: <CalendarIcon size={24} />, phase: "P4" },
      { title: "Lists", icon: <ListIcon size={24} />, phase: "P4" },
      { title: "Entities", icon: <UsersIcon size={24} />, phase: "P3" },
      { title: "Labs", icon: <FlaskIcon size={24} />, phase: "P7" },
    ],
  },
  {
    header: "Authoring",
    tiles: [
      { title: "Chat", icon: <ChatIcon size={24} />, phase: "P4" },
      { title: "Review", icon: <CheckSquareIcon size={24} />, phase: "P3" },
    ],
  },
  {
    header: "System",
    tiles: [
      { title: "Ops", icon: <GaugeIcon size={24} />, target: "ops" },
      { title: "Settings", icon: <SettingsIcon size={24} />, target: "settings" },
    ],
  },
];

const SWIPE_DOWN_PX = 48;
const EXIT_MS = 150;

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

  const close = useCallback(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      onClose();
      return;
    }
    setClosing(true);
    setTimeout(() => {
      setClosing(false);
      onClose();
    }, EXIT_MS);
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!open && !closing) return null;

  function onTouchStart(event: TouchEvent) {
    // Only arm the dismiss gesture from the top of the launcher's scroll —
    // otherwise a downward swipe is just the user scrolling back up.
    const scrolled = (panelRef.current?.scrollTop ?? 0) > 4;
    touchStartY.current = scrolled ? null : (event.touches[0]?.clientY ?? null);
  }

  function onTouchMove(event: TouchEvent) {
    const startY = touchStartY.current;
    const y = event.touches[0]?.clientY;
    if (startY !== null && y !== undefined && y - startY > SWIPE_DOWN_PX) {
      touchStartY.current = null;
      close();
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
        <button type="button" className="launcher-grab" onClick={close} aria-label="Close launcher">
          <span className="launcher-handle" aria-hidden="true" />
        </button>
        <button type="button" className="icon-btn" onClick={close} aria-label="Close launcher">
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
                    onNavigate(tile.target);
                    close();
                  }
                }}
              >
                <span className="tile-icon">{tile.icon}</span>
                <span className="tile-title">{tile.title}</span>
                {tile.phase && <span className="phase-badge">{tile.phase}</span>}
              </button>
            ))}
          </div>
        </section>
      ))}
    </nav>
  );
}
