import { useEffect, useState } from "react";
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

type Screen = "home" | "ops" | "settings";

const SCREEN_TITLES: Record<Exclude<Screen, "home">, string> = {
  ops: "Ops",
  settings: "Settings",
};

export function App() {
  const [session, setSession] = useState<Session>({ status: "loading" });
  const [screen, setScreen] = useState<Screen>("home");
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
    setScreen("home");
    setSession({ status: "anonymous" });
  }

  function navigate(target: LauncherTarget) {
    setScreen(target);
  }

  if (session.status === "loading") {
    return <main className="centered muted">Loading…</main>;
  }

  if (session.status === "anonymous") {
    return <LoginScreen onLogin={(principal) => setSession({ status: "in", principal })} />;
  }

  return (
    <div className="shell">
      <TopBar
        {...(screen !== "home"
          ? { title: SCREEN_TITLES[screen], onBack: () => setScreen("home") }
          : {})}
        syncStatus={notes.syncStatus}
        onBolt={() => setLauncherOpen(true)}
      />

      {/* Home stays mounted so stream scroll position survives sub-screens. */}
      <div className={`screen-home${screen === "home" ? "" : " screen-hidden"}`}>
        <HomeScreen notes={notes} onOpenLauncher={() => setLauncherOpen(true)} />
      </div>
      {screen === "ops" && (
        <main className="screen-body">
          <OpsScreen />
        </main>
      )}
      {screen === "settings" && (
        <SettingsScreen deviceLabel={session.principal.label} onLogout={() => void logout()} />
      )}

      <Launcher open={launcherOpen} onClose={() => setLauncherOpen(false)} onNavigate={navigate} />
    </div>
  );
}
