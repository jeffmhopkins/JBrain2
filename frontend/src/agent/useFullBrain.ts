// The Full Brain controller: one hook that owns the whole surface's state so the
// home screen can render the transcript and panels in the page body while the
// omnibox — the universal composer — drives `send`. Lifting it here is what lets
// the composer live apart from the conversation it feeds.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import {
  type TranscriptMessage,
  applyEvent,
  endStream,
  streamingAssistant,
  userMessage,
} from "./transcript";
import type {
  AgentSession,
  ChatEvent,
  ChatRequest,
  ProposalSummary,
  SessionCreate,
  TranscriptTurn,
} from "./types";

export type Panel = "none" | "sessions" | "proposals";

export interface FullBrainDeps {
  listSessions: () => Promise<AgentSession[]>;
  createSession: (body: SessionCreate) => Promise<AgentSession>;
  chat: (body: ChatRequest) => AsyncGenerator<ChatEvent>;
  listProposals: () => Promise<ProposalSummary[]>;
  getTranscript: (sessionId: string) => Promise<TranscriptTurn[]>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
}

const LIVE: FullBrainDeps = {
  listSessions: api.listSessions,
  createSession: api.createSession,
  chat: api.chat,
  listProposals: api.listProposals,
  getTranscript: api.getTranscript,
  renameSession: api.renameSession,
  deleteSession: api.deleteSession,
};

/** Map a persisted turn back into a transcript message — assistant turns rebuild
 * their tool steps, note sources, and any staged-proposal / resolved-entity chips
 * so the bubble (and its inline entity links) replay in full. */
function fromTurn(t: TranscriptTurn): TranscriptMessage {
  return {
    role: t.role,
    text: t.content,
    tools: t.tools.map((tool) => ({
      id: tool.id,
      name: tool.name,
      ...(tool.ok === null ? {} : { ok: tool.ok }),
      sources: tool.sources.map((s) => ({ noteId: s.note_id, domain: s.domain, text: s.snippet })),
      ...(tool.proposal ? { proposal: tool.proposal } : {}),
      ...(tool.entities?.length ? { entities: tool.entities } : {}),
    })),
    // Rebuild the rich tool-result views (e.g. a list_card) so they replay too.
    views: t.tools.flatMap((tool) => (tool.view ? [tool.view] : [])),
    streaming: false,
  };
}

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
  rename: (id: string, title: string) => void;
  remove: (id: string) => void;
}

/** Drive the Full Brain surface. `enabled` gates the network so nothing loads
 * until the mode is actually on screen; `deps` is injected in tests. */
export function useFullBrain(enabled: boolean, deps: FullBrainDeps = LIVE): FullBrain {
  const { listSessions, createSession, chat, listProposals, getTranscript } = deps;
  const { renameSession, deleteSession } = deps;
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

  // The review inbox. Reload it whenever the panel is opened and after each turn
  // — the agent can stage a Proposal mid-conversation, so a once-on-mount fetch
  // would leave the list stale (it'd read "Nothing staged"). Failures leave it
  // empty.
  const reloadProposals = useCallback(() => {
    listProposals()
      .then((all) => setProposals(all))
      .catch(() => {});
  }, [listProposals]);

  useEffect(() => {
    if (enabled) reloadProposals();
  }, [enabled, reloadProposals]);

  useEffect(() => {
    if (enabled && panel === "proposals") reloadProposals();
  }, [enabled, panel, reloadProposals]);

  // Replay the active session's stored transcript on open/switch (keyed on id, so
  // a live turn's own setMessages never triggers a reload). A failure just leaves
  // the conversation empty.
  const activeId = active?.id ?? null;
  useEffect(() => {
    if (!enabled || activeId === null) return;
    let stale = false;
    getTranscript(activeId)
      .then((turns) => {
        if (!stale) setMessages(turns.map(fromTurn));
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [enabled, activeId, getTranscript]);

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
      // The turn may have staged a Proposal — refresh the review inbox.
      reloadProposals();
    }
  }

  async function create(body: SessionCreate): Promise<AgentSession> {
    const created = await createSession(body);
    setSessions((prev) => [created, ...prev]);
    return created;
  }

  function open(session: AgentSession): void {
    // Clear only when actually switching sessions, so the prior chat doesn't
    // linger while the new one's transcript loads (the id-keyed effect reloads
    // it). Re-opening the current session must NOT clear — its id is unchanged,
    // so the effect wouldn't re-fire to repopulate it.
    if (session.id !== active?.id) setMessages([]);
    setActive(session);
    setPanel("none");
  }

  async function rename(id: string, title: string): Promise<void> {
    await renameSession(id, title);
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)));
    if (active?.id === id) setActive({ ...active, title });
  }

  async function remove(id: string): Promise<void> {
    await deleteSession(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
    // If the open conversation was deleted, drop it (and its transcript).
    if (active?.id === id) {
      setActive(null);
      setMessages([]);
    }
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
    rename: (id, title) => void rename(id, title).catch(() => {}),
    remove: (id) => void remove(id).catch(() => {}),
  };
}
