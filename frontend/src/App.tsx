import { useEffect, useState } from "react";
import { type Principal, api, setUnauthorizedHandler } from "./api/client";
import { BottomNav, type Tab } from "./components/BottomNav";
import { LoginScreen } from "./screens/LoginScreen";
import { OpsScreen } from "./screens/OpsScreen";
import { PlaceholderScreen } from "./screens/PlaceholderScreen";

type Session =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "in"; principal: Principal };

export function App() {
  const [session, setSession] = useState<Session>({ status: "loading" });
  const [tab, setTab] = useState<Tab>("ops");

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
    setSession({ status: "anonymous" });
  }

  if (session.status === "loading") {
    return <main className="centered muted">Loading…</main>;
  }

  if (session.status === "anonymous") {
    return <LoginScreen onLogin={(principal) => setSession({ status: "in", principal })} />;
  }

  return (
    <div className="shell">
      <header className="top-bar">
        <span className="brand">JBrain</span>
        <span className="muted device-label">{session.principal.label}</span>
        <button type="button" className="small" onClick={logout}>
          Log out
        </button>
      </header>
      <main className="content">
        {tab === "capture" && (
          <PlaceholderScreen
            title="Capture"
            phase="Coming in Phase 1"
            blurb="Quick note capture into the inbox lands here."
          />
        )}
        {tab === "chat" && (
          <PlaceholderScreen
            title="Chat"
            phase="Coming in Phase 4"
            blurb="Conversational access to your notes and wiki lands here."
          />
        )}
        {tab === "search" && (
          <PlaceholderScreen
            title="Search"
            phase="Coming in Phase 2"
            blurb="RAG-backed search over your notes lands here."
          />
        )}
        {tab === "review" && (
          <PlaceholderScreen
            title="Review"
            phase="Coming in Phase 3"
            blurb="Wiki review and correction-note workflow lands here."
          />
        )}
        {tab === "ops" && <OpsScreen />}
      </main>
      <BottomNav active={tab} onSelect={setTab} />
    </div>
  );
}
