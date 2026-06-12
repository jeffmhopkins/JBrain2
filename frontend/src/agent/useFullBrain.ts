// The Full Brain controller: one hook that owns the whole surface's state so the
// home screen can render the transcript and panels in the page body while the
// omnibox — the universal composer — drives `send`. Lifting it here is what lets
// the composer live apart from the conversation it feeds.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import {
  type TranscriptMessage,
  applyEvent,
  endStream,
  streamingAssistant,
  userMessage,
} from "./transcript";
import type { AgentSession, ChatEvent, ChatRequest, ProposalSummary, SessionCreate } from "./types";

export type Panel = "none" | "sessions" | "proposals";

export interface FullBrainDeps {
  listSessions: () => Promise<AgentSession[]>;
  createSession: (body: SessionCreate) => Promise<AgentSession>;
  chat: (body: ChatRequest) => AsyncGenerator<ChatEvent>;
  listProposals: () => Promise<ProposalSummary[]>;
}

const LIVE: FullBrainDeps = {
  listSessions: api.listSessions,
  createSession: api.createSession,
  chat: api.chat,
  listProposals: api.listProposals,
};

export interface FullBrain {
  active: AgentSession | null;
  sessions: AgentSession[];
  proposals: ProposalSummary[];
  panel: Panel;
  setPanel: (p: Panel) => void;
  openProposal: string | null;
  setOpenProposal: (id: string | null) => void;
  messages: TranscriptMessage[];
  busy: boolean;
  /** A turn can be sent only once a session (read scope) is chosen and no stream
   * is in flight. */
  canSend: boolean;
  send: (text: string) => void;
  create: (body: SessionCreate) => Promise<AgentSession>;
  open: (session: AgentSession) => void;
}

/** Drive the Full Brain surface. `enabled` gates the network so nothing loads
 * until the mode is actually on screen; `deps` is injected in tests. */
export function useFullBrain(enabled: boolean, deps: FullBrainDeps = LIVE): FullBrain {
  const { listSessions, createSession, chat, listProposals } = deps;
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [active, setActive] = useState<AgentSession | null>(null);
  const [panel, setPanel] = useState<Panel>("none");
  const [proposals, setProposals] = useState<ProposalSummary[]>([]);
  const [openProposal, setOpenProposal] = useState<string | null>(null);
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!enabled) return;
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
  }, [enabled, listSessions]);

  // The review inbox; failures just leave it empty.
  useEffect(() => {
    if (!enabled) return;
    let stale = false;
    listProposals()
      .then((all) => {
        if (!stale) setProposals(all);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [enabled, listProposals]);

  async function send(textRaw: string): Promise<void> {
    const text = textRaw.trim();
    if (!text || busy) return;
    // No scope yet — surface the picker rather than chatting against nothing.
    if (!active) {
      setPanel("sessions");
      return;
    }
    setBusy(true);
    const history = messages.map((m) => ({ role: m.role, content: m.text }));
    setMessages((ms) => [...ms, userMessage(text), streamingAssistant()]);
    try {
      for await (const event of chat({ session_id: active.id, message: text, history })) {
        setMessages((ms) => applyEvent(ms, event));
      }
      setMessages((ms) => (ms[ms.length - 1]?.streaming ? endStream(ms, "end_turn") : ms));
    } catch {
      setMessages((ms) => endStream(ms, "error"));
    } finally {
      setBusy(false);
    }
  }

  async function create(body: SessionCreate): Promise<AgentSession> {
    const created = await createSession(body);
    setSessions((prev) => [created, ...prev]);
    return created;
  }

  function open(session: AgentSession): void {
    setActive(session);
    setPanel("none");
  }

  return {
    active,
    sessions,
    proposals,
    panel,
    setPanel,
    openProposal,
    setOpenProposal,
    messages,
    busy,
    canSend: !busy && active !== null,
    send: (text) => void send(text),
    create,
    open,
  };
}
