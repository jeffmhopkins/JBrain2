// Full-screen card launcher (docs/DESIGN.md "Navigation: the card
// launcher"). A navigation surface, not a modal: it owns the whole screen,
// slides up 150ms ease-out, and dismisses on swipe-down or Escape.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
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

const SWIPE_DOWN_PX = 64;
const EXIT_MS = 150;

interface LauncherProps {
  open: boolean;
  onClose: () => void;
  onNavigate: (target: LauncherTarget) => void;
}

export function Launcher({ open, onClose, onNavigate }: LauncherProps) {
  // Stay mounted through the exit animation, then unmount.
  const [closing, setClosing] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const touchStartY = useRef<number | null>(null);

  function close() {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      onClose();
      return;
    }
    setClosing(true);
    setTimeout(() => {
      setClosing(false);
      onClose();
    }, EXIT_MS);
  }

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // biome-ignore lint/correctness/useExhaustiveDependencies: close is stable per render; re-binding on open is the point.
  }, [open]);

  if (!open && !closing) return null;

  function onTouchStart(event: TouchEvent) {
    touchStartY.current = event.touches[0]?.clientY ?? null;
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
    <div
      className={`launcher${closing ? " launcher-closing" : ""}`}
      ref={panelRef}
      tabIndex={-1}
      role="dialog"
      aria-label="Launcher"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
    >
      <div className="launcher-head">
        <span className="launcher-handle" aria-hidden="true" />
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
    </div>
  );
}
