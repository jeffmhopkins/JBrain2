// The Full Brain surface: the chat for the active session, with the lateral
// shortcuts the mock specifies — swipe right shuttles in Sessions (from the left
// edge), swipe left shuttles in Proposals (from the right). No edge chrome; the
// gesture is the in-context shortcut and the launcher tile is the tappable way
// in (docs/mocks/assistant-lateral-swipe.html). With no session yet, the
// Sessions panel opens so a read scope is chosen before any chat.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { FullBrainScreen } from "./FullBrainScreen";
import { type ProposalSummary, ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import type { AgentSession, ChatEvent, ChatRequest, SessionCreate } from "./types";

type Panel = "none" | "sessions" | "proposals";
const OPEN_PX = 64; // horizontal travel that commits a panel open

interface Props {
  listSessions?: () => Promise<AgentSession[]>;
  createSession?: (body: SessionCreate) => Promise<AgentSession>;
  chat?: (body: ChatRequest) => AsyncGenerator<ChatEvent>;
  /** Proposals data arrives in P4.8; empty for now. */
  proposals?: ProposalSummary[];
}

export function FullBrainShell({
  listSessions = api.listSessions,
  createSession = api.createSession,
  chat = api.chat,
  proposals = [],
}: Props): ReactNode {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [active, setActive] = useState<AgentSession | null>(null);
  const [panel, setPanel] = useState<Panel>("none");
  const drag = useRef<{ x: number; axis: "?" | "h" | "v"; dx: number } | null>(null);

  useEffect(() => {
    let stale = false;
    listSessions()
      .then((all) => {
        if (stale) return;
        setSessions(all);
        const live = all.find((s) => s.status === "active") ?? null;
        setActive(live);
        // No session means no read scope chosen — pick one before chatting.
        if (!live) setPanel("sessions");
      })
      .catch(() => {
        if (!stale) setPanel("sessions");
      });
    return () => {
      stale = true;
    };
  }, [listSessions]);

  async function create(body: SessionCreate): Promise<AgentSession> {
    const created = await createSession(body);
    setSessions((prev) => [created, ...prev]);
    return created;
  }

  function open(session: AgentSession): void {
    setActive(session);
    setPanel("none");
  }

  function onTouchStart(e: TouchEvent): void {
    if (panel !== "none") return;
    const t = e.touches[0];
    if (t) drag.current = { x: t.clientX, axis: "?", dx: 0 };
  }

  function onTouchMove(e: TouchEvent): void {
    const d = drag.current;
    const t = e.touches[0];
    if (!d || !t) return;
    d.dx = t.clientX - d.x;
    if (d.axis === "?" && Math.abs(d.dx) > 10) d.axis = "h";
  }

  function onTouchEnd(): void {
    const d = drag.current;
    drag.current = null;
    if (!d || d.axis !== "h" || Math.abs(d.dx) < OPEN_PX) return;
    // Swipe right (dx>0) → Sessions; swipe left (dx<0) → Proposals.
    setPanel(d.dx > 0 ? "sessions" : "proposals");
  }

  return (
    <div
      className="fb-shell"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
    >
      {active ? (
        <FullBrainScreen session={active} chat={chat} />
      ) : (
        <div className="fb-empty">Choose a session to start asking about your brain.</div>
      )}

      <aside
        className={`panel left${panel === "sessions" ? " open" : ""}`}
        aria-hidden={panel !== "sessions"}
      >
        <SessionsPanel
          sessions={sessions}
          onOpen={open}
          onCreate={create}
          onClose={() => setPanel("none")}
        />
      </aside>

      <aside
        className={`panel right${panel === "proposals" ? " open" : ""}`}
        aria-hidden={panel !== "proposals"}
      >
        <ProposalsPanel
          proposals={proposals}
          onOpen={() => undefined}
          onClose={() => setPanel("none")}
        />
      </aside>
    </div>
  );
}
