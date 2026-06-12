// The Full Brain surface: the chat for the active session, with the two lateral
// panels the mock specifies — Sessions slides in from the left, Proposals from
// the right (docs/mocks/assistant-lateral-swipe.html). A horizontal swipe is the
// in-context shortcut: swipe right shuttles in Sessions, swipe left shuttles in
// Proposals, and the opposite swipe sends the open panel back out. The header's
// visible Sessions / Proposals buttons do the same thing for anyone who'd rather
// tap. With no session yet, the Sessions panel opens so a read scope is chosen
// before any chat.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { FullBrainScreen } from "./FullBrainScreen";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import type { AgentSession, ChatEvent, ChatRequest, ProposalSummary, SessionCreate } from "./types";

type Panel = "none" | "sessions" | "proposals";
const OPEN_PX = 56; // horizontal travel that commits a panel open or closed

interface Props {
  listSessions?: () => Promise<AgentSession[]>;
  createSession?: (body: SessionCreate) => Promise<AgentSession>;
  chat?: (body: ChatRequest) => AsyncGenerator<ChatEvent>;
  listProposals?: () => Promise<ProposalSummary[]>;
  /** A message carried in from the home Full Brain box; seeds the composer. */
  initialDraft?: string | null;
}

export function FullBrainShell({
  listSessions = api.listSessions,
  createSession = api.createSession,
  chat = api.chat,
  listProposals = api.listProposals,
  initialDraft = null,
}: Props): ReactNode {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [active, setActive] = useState<AgentSession | null>(null);
  const [panel, setPanel] = useState<Panel>("none");
  const [proposals, setProposals] = useState<ProposalSummary[]>([]);
  const [openProposal, setOpenProposal] = useState<string | null>(null);
  const drag = useRef<{ x: number; axis: "?" | "h" | "v" } | null>(null);

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

  // The review inbox; failures just leave it empty.
  useEffect(() => {
    let stale = false;
    listProposals()
      .then((all) => {
        if (!stale) setProposals(all);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [listProposals]);

  async function create(body: SessionCreate): Promise<AgentSession> {
    const created = await createSession(body);
    setSessions((prev) => [created, ...prev]);
    return created;
  }

  function open(session: AgentSession): void {
    setActive(session);
    setPanel("none");
  }

  // A horizontal swipe opens a panel (right→Sessions, left→Proposals) or, when
  // one is open, the opposite swipe sends it back out. Text fields opt out so
  // typing and selection aren't hijacked; taps on buttons fall through (they
  // never travel OPEN_PX).
  function onTouchStart(e: TouchEvent): void {
    const target = e.target as HTMLElement;
    if (target.closest(".fb-composer, textarea, input, select")) {
      drag.current = null;
      return;
    }
    const t = e.touches[0];
    drag.current = t ? { x: t.clientX, axis: "?" } : null;
  }

  function onTouchMove(e: TouchEvent): void {
    const d = drag.current;
    const t = e.touches[0];
    if (!d || !t) return;
    if (d.axis === "?" && Math.abs(t.clientX - d.x) > 10) d.axis = "h";
  }

  function onTouchEnd(e: TouchEvent): void {
    const d = drag.current;
    drag.current = null;
    const t = e.changedTouches[0];
    if (!d || !t || d.axis !== "h") return;
    const dx = t.clientX - d.x;
    if (Math.abs(dx) < OPEN_PX) return;
    if (panel === "none") {
      // Right shuttles in Sessions (from the left); left shuttles in Proposals.
      setPanel(dx > 0 ? "sessions" : "proposals");
    } else if (panel === "sessions" && dx < 0) {
      setPanel("none"); // swipe it back out the way it came
    } else if (panel === "proposals" && dx > 0) {
      setPanel("none");
    }
  }

  return (
    <div
      className="fb-shell"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
    >
      {active ? (
        <FullBrainScreen
          session={active}
          chat={chat}
          initialDraft={initialDraft ?? ""}
          onOpenSessions={() => setPanel("sessions")}
          onOpenProposals={() => setPanel("proposals")}
        />
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
        {openProposal === null ? (
          <ProposalsPanel
            proposals={proposals}
            onOpen={(p) => setOpenProposal(p.id)}
            onClose={() => setPanel("none")}
          />
        ) : (
          <ProposalTree proposalId={openProposal} onClose={() => setOpenProposal(null)} />
        )}
      </aside>
    </div>
  );
}
