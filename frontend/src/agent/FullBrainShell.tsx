// The Full Brain surface: the chat for the active session, with the two lateral
// panels the mock specifies — Sessions slides in from the left, Proposals from
// the right (docs/mocks/assistant-lateral-swipe.html). The visible Sessions /
// Proposals buttons in the chat header are the way in; gestures proved
// unreliable on real devices (same lesson the launcher encodes), so they're not
// the primary path. With no session yet, the Sessions panel opens so a read
// scope is chosen before any chat.

import { type ReactNode, useEffect, useState } from "react";
import { api } from "../api/client";
import { FullBrainScreen } from "./FullBrainScreen";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import type { AgentSession, ChatEvent, ChatRequest, ProposalSummary, SessionCreate } from "./types";

type Panel = "none" | "sessions" | "proposals";

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

  return (
    <div className="fb-shell">
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
