// The Full Brain controller: one hook that owns the whole surface's state so the
// home screen can render the transcript and panels in the page body while the
// omnibox — the universal composer — drives `send`. Lifting it here is what lets
// the composer live apart from the conversation it feeds.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { freshCoords } from "../location";
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

/** The two conversation tabs and the agents each owns. Full Brain is the Curator
 * (your knowledge base, full domain access); Research is Jerv (web) or Teacher
 * (study tutor) — neither reads your notes. A null mode means the surface is off
 * screen (Entry / capture modes), so the controller does no network work. */
export type ConvMode = "research" | "fullbrain";
const MODE_AGENTS: Record<ConvMode, readonly string[]> = {
  research: ["jerv", "teacher"],
  fullbrain: ["curator"],
};
/** The agent a re-click / empty-state start spins up for each tab. */
const NEW_AGENT: Record<ConvMode, string> = { research: "jerv", fullbrain: "curator" };
/** The owner holds every scope, so a fresh Curator reads the whole brain; Jerv
 * (and Teacher) read no owner data, so they start with an empty firewall scope. */
const ALL_DOMAINS = ["general", "health", "finance", "location"];
const newSessionBody = (mode: ConvMode): SessionCreate =>
  mode === "fullbrain"
    ? { domain_scopes: ALL_DOMAINS, agent: "curator" }
    : { domain_scopes: [], agent: "jerv" };

/** The latest non-archived session whose agent belongs to the mode — what a tab
 * reopens when you switch into it (active or ended, newest by last activity). */
function latestForMode(sessions: AgentSession[], mode: ConvMode): AgentSession | null {
  const agents = MODE_AGENTS[mode];
  return (
    sessions
      .filter((s) => agents.includes(s.agent) && s.status !== "archived")
      .sort((a, b) => Date.parse(b.last_active_at) - Date.parse(a.last_active_at))[0] ?? null
  );
}

export interface FullBrainDeps {
  listSessions: () => Promise<AgentSession[]>;
  createSession: (body: SessionCreate) => Promise<AgentSession>;
  chat: (body: ChatRequest) => AsyncGenerator<ChatEvent>;
  listProposals: (sessionId?: string) => Promise<ProposalSummary[]>;
  getTranscript: (sessionId: string) => Promise<TranscriptTurn[]>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  archiveSession: (id: string) => Promise<void>;
  unarchiveSession: (id: string) => Promise<void>;
  rescopeSession: (id: string, domainScopes: string[]) => Promise<void>;
}

const LIVE: FullBrainDeps = {
  listSessions: api.listSessions,
  createSession: api.createSession,
  chat: api.chat,
  listProposals: api.listProposals,
  getTranscript: api.getTranscript,
  renameSession: api.renameSession,
  deleteSession: api.deleteSession,
  archiveSession: api.archiveSession,
  unarchiveSession: api.unarchiveSession,
  rescopeSession: api.rescopeSession,
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
      ...(tool.args ? { args: tool.args } : {}),
      ...(tool.summary ? { summary: tool.summary } : {}),
      sources: tool.sources.map((s) => ({ noteId: s.note_id, domain: s.domain, text: s.snippet })),
      ...(tool.proposal ? { proposal: tool.proposal } : {}),
      ...(tool.entities?.length ? { entities: tool.entities } : {}),
    })),
    // Rebuild the rich tool-result views (e.g. a list_card) so they replay too.
    views: t.tools.flatMap((tool) => (tool.view ? [tool.view] : [])),
    streaming: false,
    reasoning: t.reasoning ?? "",
    thinking: false,
  };
}

export interface FullBrain {
  active: AgentSession | null;
  /** The sessions shown for the current tab — filtered to that mode's agents
   * (Research lists Jerv + Teacher; Full Brain lists Curator). */
  sessions: AgentSession[];
  /** The agents a new chat may use in this mode — drives the panel's picker
   * (Research offers Jerv + Teacher; Full Brain only Curator). */
  agentOptions: readonly string[];
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
  /** `appointmentId` rides a calendar handoff so the agent resolves that exact
   * appointment; the user bubble still shows only `text`. */
  send: (text: string, opts?: { appointmentId?: string }) => void;
  create: (body: SessionCreate) => Promise<AgentSession>;
  /** Re-clicking the active tab: start a new chat with that mode's default agent.
   * Reuses the open chat if it's already an empty one of that same agent, so a
   * repeated tap doesn't pile up blank sessions. */
  startFresh: () => void;
  open: (session: AgentSession) => void;
  rename: (id: string, title: string) => void;
  remove: (id: string) => void;
  archive: (id: string) => void;
  unarchive: (id: string) => void;
  rescope: (id: string, domainScopes: string[]) => void;
}

/** Drive a conversation surface. `mode` is which tab is on screen (or null when
 * the surface is off screen, which gates the network so nothing loads); it also
 * selects the agent group the tab reads and creates under. `autoStart` (the home
 * screen, not the tests) opens that group's most recent non-archived chat on
 * entry — or starts a fresh one if there is none. `deps` is injected in tests. */
export function useFullBrain(
  mode: ConvMode | null,
  deps: FullBrainDeps = LIVE,
  autoStart = false,
): FullBrain {
  const { listSessions, createSession, chat, listProposals, getTranscript } = deps;
  const { renameSession, deleteSession, archiveSession, unarchiveSession } = deps;
  const { rescopeSession } = deps;
  const enabled = mode !== null;
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [active, setActive] = useState<AgentSession | null>(null);
  const [panel, setPanel] = useState<Panel>("none");
  const [proposals, setProposals] = useState<ProposalSummary[]>([]);
  const [openProposal, setOpenProposal] = useState<string | null>(null);
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [busy, setBusy] = useState(false);
  // The open chat's id — the key the transcript and proposal inbox load against.
  const activeId = active?.id ?? null;
  // Read in the resolve effect without making it a dependency (which would re-fire
  // it on every open/turn and re-pick the session).
  const activeRef = useRef(active);
  activeRef.current = active;
  // Guards a single auto-create per mode entry against a fast double-fire.
  const creatingFor = useRef<ConvMode | null>(null);

  // Only this mode's agents belong on the tab; the picker creates under them too.
  const agentOptions = mode ? MODE_AGENTS[mode] : ["curator", "teacher", "jerv"];
  const visibleSessions = mode
    ? sessions.filter((s) => MODE_AGENTS[mode].includes(s.agent))
    : sessions;

  // Load the chat list once the surface comes on screen (the whole list, across
  // agents; the resolve effect narrows it to the mode).
  useEffect(() => {
    if (!enabled) return;
    let stale = false;
    listSessions()
      .then((all) => {
        if (stale) return;
        setSessions(all);
        setLoaded(true);
      })
      .catch(() => {
        if (!stale) setLoaded(true);
      });
    return () => {
      stale = true;
    };
  }, [enabled, listSessions]);

  // Resolve which chat the tab lands on whenever the mode changes (or the list
  // first loads). Open the group's most recent non-archived chat; with autoStart
  // and none to open, start a fresh one; without it, fall back to the picker.
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on mode/loaded; reads the freshest sessions/active via state + ref.
  useEffect(() => {
    if (!enabled || !mode || !loaded) return;
    const latest = latestForMode(sessions, mode);
    if (latest) {
      if (latest.id !== activeRef.current?.id) open(latest);
      return;
    }
    if (autoStart) {
      if (creatingFor.current !== mode) {
        creatingFor.current = mode;
        void create(newSessionBody(mode))
          .then(open)
          .catch(() => {})
          .finally(() => {
            if (creatingFor.current === mode) creatingFor.current = null;
          });
      }
      return;
    }
    // No chat and no auto-start — surface the picker rather than chat against none.
    setActive(null);
    setPanel("sessions");
  }, [enabled, mode, loaded, autoStart]);

  // The review inbox, scoped to the open chat: its own staged proposals plus the
  // session-less background ones. Keyed on `activeId` so switching chats reloads
  // it (a proposal staged in one chat must not linger in another). Reload it
  // whenever the panel is opened and after each turn too — the agent can stage a
  // Proposal mid-conversation, so a once-on-mount fetch would leave the list stale
  // (it'd read "Nothing staged"). Failures leave it empty.
  const reloadProposals = useCallback(() => {
    listProposals(activeId ?? undefined)
      .then((all) => setProposals(all))
      .catch(() => {});
  }, [listProposals, activeId]);

  // Refetch the session list and re-sync the open session by id (its title may
  // have just been auto-generated server-side; the id is unchanged so the
  // transcript effect doesn't re-fire).
  const reloadSessions = useCallback(() => {
    listSessions()
      .then((all) => {
        setSessions(all);
        setActive((cur) => (cur ? (all.find((s) => s.id === cur.id) ?? cur) : cur));
      })
      .catch(() => {});
  }, [listSessions]);

  useEffect(() => {
    if (enabled) reloadProposals();
  }, [enabled, reloadProposals]);

  useEffect(() => {
    if (enabled && panel === "proposals") reloadProposals();
  }, [enabled, panel, reloadProposals]);

  // Replay the active session's stored transcript on open/switch (keyed on id, so
  // a live turn's own setMessages never triggers a reload). A failure just leaves
  // the conversation empty.
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

  async function send(textRaw: string, opts?: { appointmentId?: string }): Promise<void> {
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
    // Reuse the note-capture warm fix (only when capture is on and fresh) so the
    // location tool can answer from the phone's current spot.
    const coords = freshCoords();
    try {
      for await (const event of chat({
        session_id: active.id,
        message: text,
        history,
        ...(opts?.appointmentId ? { appointment_id: opts.appointmentId } : {}),
        ...(coords ? { latitude: coords.latitude, longitude: coords.longitude } : {}),
      })) {
        setMessages((ms) => applyEvent(ms, event));
      }
      setMessages((ms) => (ms[ms.length - 1]?.streaming ? endStream(ms, "end_turn") : ms));
    } catch {
      setMessages((ms) => endStream(ms, "error"));
    } finally {
      setBusy(false);
      // The turn may have staged a Proposal — refresh the review inbox.
      reloadProposals();
      // Refresh the session so the panel/top bar pick up server-side changes the
      // turn caused: an auto-generated title (first turn) and the card metadata
      // (turn count, preview, staged count).
      reloadSessions();
    }
  }

  async function create(body: SessionCreate): Promise<AgentSession> {
    const created = await createSession(body);
    setSessions((prev) => [created, ...prev]);
    return created;
  }

  // Re-clicking the active tab. Reuse the open chat when it's already an empty one
  // of the mode's default agent (so a repeated tap doesn't pile up blanks); else
  // spin up a fresh chat — Curator with full domain access, or a new Jerv.
  function startFresh(): void {
    if (!mode) return;
    const empty = active && messages.length === 0 && (active.turn_count ?? 0) === 0;
    if (empty && active.agent === NEW_AGENT[mode]) {
      setPanel("none");
      return;
    }
    void create(newSessionBody(mode))
      .then(open)
      .catch(() => {});
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

  async function archive(id: string): Promise<void> {
    await archiveSession(id);
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, status: "archived" } : s)));
  }

  async function unarchive(id: string): Promise<void> {
    await unarchiveSession(id);
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, status: "active" } : s)));
  }

  async function rescope(id: string, domainScopes: string[]): Promise<void> {
    await rescopeSession(id, domainScopes);
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, domain_scopes: domainScopes } : s)),
    );
    if (active?.id === id) setActive({ ...active, domain_scopes: domainScopes });
  }

  return {
    active,
    sessions: visibleSessions,
    agentOptions,
    proposals,
    panel,
    setPanel,
    openProposal,
    setOpenProposal,
    messages,
    busy,
    canSend: !busy && active !== null,
    send: (text, opts) => void send(text, opts),
    create,
    startFresh,
    open,
    rename: (id, title) => void rename(id, title).catch(() => {}),
    remove: (id) => void remove(id).catch(() => {}),
    archive: (id) => void archive(id).catch(() => {}),
    unarchive: (id) => void unarchive(id).catch(() => {}),
    rescope: (id, scopes) => void rescope(id, scopes).catch(() => {}),
  };
}
