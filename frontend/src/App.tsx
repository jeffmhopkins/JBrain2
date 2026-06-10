import { type TouchEvent, useEffect, useRef, useState } from "react";
import { type Principal, api, setUnauthorizedHandler } from "./api/client";
import { Launcher, type LauncherTarget } from "./components/Launcher";
import { TopBar } from "./components/TopBar";
import { useNotes } from "./notes/useNotes";
import { HomeScreen } from "./screens/HomeScreen";
import { LoginScreen } from "./screens/LoginScreen";
import { OpsScreen } from "./screens/OpsScreen";
import { SettingsScreen } from "./screens/SettingsScreen";

type Session =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "in"; principal: Principal };

type Card = "ops" | "settings";

const SCREEN_TITLES: Record<Card, string> = {
  ops: "Ops",
  settings: "Settings",
};

const CARD_EXIT_MS = 150;

export function App() {
  const [session, setSession] = useState<Session>({ status: "loading" });
  const [card, setCard] = useState<Card | null>(null);
  const [cardClosing, setCardClosing] = useState(false);
  const [launcherOpen, setLauncherOpen] = useState(false);

  // Lives at the app level so the outbox keeps flushing while the user is
  // on Ops or Settings.
  const notes = useNotes(session.status === "in");

  // Any 401 from the API means the cookie expired or was revoked.
  useEffect(() => {
    setUnauthorizedHandler(() => setSession({ status: "anonymous" }));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    api
      .me()
      .then((principal) => setSession({ status: "in", principal }))
      .catch(() => setSession({ status: "anonymous" }));
  }, []);

  async function logout() {
    try {
      await api.logout();
    } catch {
      // Even if the server call fails the local session is done.
    }
    setCard(null);
    setLauncherOpen(false);
    setSession({ status: "anonymous" });
  }

  function navigate(target: LauncherTarget) {
    setCard(target);
  }

  function reducedMotion(): boolean {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // Climb one level: the card sinks away, revealing the launcher beneath.
  function closeCardToLauncher() {
    if (reducedMotion()) {
      setCard(null);
      return;
    }
    setCardClosing(true);
    setTimeout(() => {
      setCardClosing(false);
      setCard(null);
    }, CARD_EXIT_MS);
  }

  // Chevron: jump straight home — drop the launcher instantly (the card
  // still covers it), then let the card sink to reveal home.
  function jumpHome() {
    setLauncherOpen(false);
    closeCardToLauncher();
  }

  // Navigation is a tree: home → (swipe up) → launcher → (tap) → card
  // screen. Swiping DOWN climbs back up a level — card screen reopens the
  // launcher here; the launcher's own down-swipe returns home. Armed only
  // when the screen is scrolled to the top so it never fights scrolling.
  const swipeStart = useRef<{ x: number; y: number } | null>(null);
  const subRef = useRef<HTMLDivElement | null>(null);

  function onSubTouchStart(event: TouchEvent) {
    const scroller = subRef.current?.querySelector("main");
    if ((scroller?.scrollTop ?? 0) > 4) {
      swipeStart.current = null;
      return;
    }
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }

  function onSubTouchMove(event: TouchEvent) {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dy = t.clientY - start.y;
    const dx = Math.abs(t.clientX - start.x);
    if (dy > 56 && dy > dx * 2) {
      swipeStart.current = null;
      closeCardToLauncher();
    }
  }

  if (session.status === "loading") {
    return <main className="centered muted">Loading…</main>;
  }

  if (session.status === "anonymous") {
    return <LoginScreen onLogin={(principal) => setSession({ status: "in", principal })} />;
  }

  return (
    <div className="shell">
      <TopBar syncStatus={notes.syncStatus} onBolt={() => setLauncherOpen(true)} />

      {/* Home stays mounted so stream scroll position survives sub-screens. */}
      <div className={`screen-home${card === null && !launcherOpen ? "" : " screen-hidden"}`}>
        <HomeScreen notes={notes} onOpenLauncher={() => setLauncherOpen(true)} />
      </div>

      <Launcher open={launcherOpen} onClose={() => setLauncherOpen(false)} onNavigate={navigate} />

      {card !== null && (
        <div
          className={`subscreen${cardClosing ? " subscreen-closing" : ""}`}
          ref={subRef}
          onTouchStart={onSubTouchStart}
          onTouchMove={onSubTouchMove}
        >
          <TopBar
            title={SCREEN_TITLES[card]}
            onBack={jumpHome}
            syncStatus={notes.syncStatus}
            onBolt={closeCardToLauncher}
          />
          {card === "ops" && (
            <main className="screen-body">
              <OpsScreen />
            </main>
          )}
          {card === "settings" && (
            <SettingsScreen deviceLabel={session.principal.label} onLogout={() => void logout()} />
          )}
        </div>
      )}
    </div>
  );
}
