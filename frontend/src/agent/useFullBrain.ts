// The Full Brain controller: one hook that owns the whole surface's state so the
// home screen can render the transcript and panels in the page body while the
// omnibox — the universal composer — drives `send`. Lifting it here is what lets
// the composer live apart from the conversation it feeds.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { freshCoords } from "../location";
import { isForeground } from "../visibility";
import {
  type TranscriptMessage,
  applyEvent,
  endStream,
  streamingAssistant,
  userMessage,
} from "./transcript";
import type {
  AgentSession,
  ChatAttachment,
  ChatEvent,
  ChatRequest,
  ProposalSummary,
  SessionCreate,
  TranscriptTurn,
} from "./types";

export type Panel = "none" | "sessions" | "proposals";

// A shared empty transcript so the active chat's `messages` keeps a stable reference
// when its buffer is absent (no needless re-renders of the conversation).
const EMPTY_MESSAGES: TranscriptMessage[] = [];

/** What a live turn is doing, for the session picker's activity glyph: an image tool
 * mid-flight reads as a render; everything else (reasoning, other tools, answering)
 * reads as thinking. */
export type TurnKind = "thinking" | "rendering";
export interface ActiveTurn {
  sessionId: string;
  kind: TurnKind;
}
const IMAGE_TOOLS = new Set(["generate_image", "edit_image"]);
function turnKind(buffer: TranscriptMessage[] | undefined): TurnKind {
  const last = buffer?.[buffer.length - 1];
  if (!last || last.role !== "assistant") return "thinking";
  const running = last.tools.find((t) => t.ok === undefined);
  return running && IMAGE_TOOLS.has(running.name) ? "rendering" : "thinking";
}

/** Live context-window fill for the open chat, from the stream's `usage` events.
 * `used` is the latest step's prompt + output (the fullest the context has been);
 * `base` is the turn's FIRST model call — the carried-forward floor (system + text
 * history + the new message, before any tool scaffolding piles on). Their gap is the
 * turn's transient tool I/O, which the meter shades separately so the intra-turn spike
 * reads apart from what actually survives into the next turn. Both over the model's
 * total `window`; null until the first usage event of a session. */
export interface ContextUsage {
  used: number;
  base: number;
  window: number;
}

/** Thrown when a chat-attachment upload fails mid-send: the turn is aborted
 * before anything lands in the transcript, so the composer can keep the typed
 * text and staged files for a retry (rather than losing them). */
export class AttachmentUploadError extends Error {
  constructor() {
    super("chat attachment upload failed");
    this.name = "AttachmentUploadError";
  }
}

/** The two conversation tabs and the agents each owns. Full Brain is the Curator
 * (your knowledge base, full domain access); Research is Jerv (web), Teacher
 * (study tutor), or the Archivist (Gmail organizer) — none read your notes. A null
 * mode means the surface is off screen (Entry / capture modes), so the controller
 * does no network work. */
export type ConvMode = "research" | "fullbrain";
const MODE_AGENTS: Record<ConvMode, readonly string[]> = {
  research: ["jerv", "teacher", "archivist"],
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

// Recovery poll cadence after a dropped stream: the turn keeps running detached
// server-side, so we re-check the transcript until the finished exchange lands. The
// ceiling clears a long image edit (minutes) before giving up to a real error.
const RECONCILE_INTERVAL_MS = 3000;
const RECONCILE_TIMEOUT_MS = 360_000;

export interface FullBrainDeps {
  listSessions: () => Promise<AgentSession[]>;
  createSession: (body: SessionCreate) => Promise<AgentSession>;
  chat: (body: ChatRequest, signal?: AbortSignal) => AsyncGenerator<ChatEvent>;
  chatResume: (runId: string, after: number, signal?: AbortSignal) => AsyncGenerator<ChatEvent>;
  cancelChatRun: (runId: string) => Promise<void>;
  listProposals: (sessionId?: string) => Promise<ProposalSummary[]>;
  getTranscript: (sessionId: string) => Promise<TranscriptTurn[]>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  archiveSession: (id: string) => Promise<void>;
  unarchiveSession: (id: string) => Promise<void>;
  rescopeSession: (id: string, domainScopes: string[]) => Promise<void>;
  uploadChatAttachment: (sessionId: string, file: File) => Promise<ChatAttachment>;
  getChatCapabilities: () => Promise<{ supports_vision: boolean; can_edit_images: boolean }>;
}

const LIVE: FullBrainDeps = {
  listSessions: api.listSessions,
  createSession: api.createSession,
  chat: api.chat,
  chatResume: api.chatResume,
  cancelChatRun: api.cancelChatRun,
  listProposals: api.listProposals,
  getTranscript: api.getTranscript,
  renameSession: api.renameSession,
  deleteSession: api.deleteSession,
  archiveSession: api.archiveSession,
  unarchiveSession: api.unarchiveSession,
  rescopeSession: api.rescopeSession,
  uploadChatAttachment: api.uploadChatAttachment,
  getChatCapabilities: api.getChatCapabilities,
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
      ...(tool.web_sources?.length ? { webSources: tool.web_sources } : {}),
      ...(tool.proposal ? { proposal: tool.proposal } : {}),
      ...(tool.entities?.length ? { entities: tool.entities } : {}),
      ...(tool.text_offset !== undefined ? { textOffset: tool.text_offset } : {}),
    })),
    // Rebuild the rich tool-result views (e.g. a list_card) so they replay too.
    views: t.tools.flatMap((tool) => (tool.view ? [tool.view] : [])),
    streaming: false,
    reasoning: t.reasoning ?? "",
    thinking: false,
    // The owner's attached files (user turns only) replay as bubble chips.
    ...(t.attachments?.length ? { attachments: t.attachments } : {}),
  };
}

// The model only ever receives prior turns as text, so an assistant turn that
// generated images appends a compact, machine-readable reference — each image's id
// (for edit_image's source_image_id) and seed (to reproduce or tweak) — so a later
// "edit that" or "use the same seed" turn can act on a picture it can't otherwise see.
function historyContent(m: TranscriptMessage): string {
  if (m.role === "user") {
    // An attached image rides the model context for ONE turn (its bytes aren't re-sent),
    // but its id must persist so a later "is the person female?" / "make it night" turn can
    // pass it to analyze_image or edit_image instead of guessing an id ("latest").
    const imgs = (m.attachments ?? []).filter((a) => a.media_type.startsWith("image/"));
    if (imgs.length === 0) return m.text;
    const refs = imgs.map((a) => `source_attachment_id=${a.id} (${a.filename})`).join("; ");
    return `${m.text}\n\n[Images the owner attached this turn — ${refs}]`;
  }
  if (m.role !== "assistant") return m.text;
  const refs = m.views
    .filter((v) => v.view === "generated_image")
    .map((v) => {
      const d = v.data;
      const id = typeof d.image_id === "string" ? d.image_id : "";
      if (!id) return "";
      const seed = typeof d.seed === "number" ? ` seed=${d.seed}` : "";
      const prompt = typeof d.prompt === "string" && d.prompt ? ` (${d.prompt})` : "";
      return `source_image_id=${id}${seed}${prompt}`;
    })
    .filter(Boolean);
  return refs.length
    ? `${m.text}\n\n[Images you generated this turn — ${refs.join("; ")}]`
    : m.text;
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
  /** The chat with a turn streaming right now (and what it's doing), or null when
   * idle — drives the session picker's per-row activity glyph so an in-flight
   * thinking/render is visible even from another chat. At most one at a time (busy
   * gates sends). */
  activeTurn: ActiveTurn | null;
  /** A turn can be sent only once a session (read scope) is chosen and no stream
   * is in flight. */
  canSend: boolean;
  /** Live context-window fill for the open chat, or null before the first usage
   * event — drives the composer's context-usage meter. */
  usage: ContextUsage | null;
  /** Abort the in-flight turn (the composer's Stop button). A no-op when idle; the
   * partial answer streamed so far stays on screen, settled as "Stopped". */
  stop: () => void;
  /** `appointmentId` rides a calendar handoff so the agent resolves that exact
   * appointment; the user bubble still shows only `text`. `files` are uploaded
   * first (in order) and ride the turn as attachments. */
  send: (text: string, opts?: { appointmentId?: string; files?: File[] }) => Promise<boolean>;
  /** Whether the agent's model can accept images — gates the chat attach
   * affordance. Defaults to false until the capability check answers (the safe
   * default: never offer an attach the model would reject; the paperclip simply
   * appears once vision is confirmed). */
  supportsVision: boolean;
  /** Whether the on-box image tools are configured. When true an attached image is
   * useful to jerv even without vision (it can analyze_image / edit_image it by id),
   * so the composer keeps offering attach in that mode. */
  canEditImages: boolean;
  create: (body: SessionCreate) => Promise<AgentSession>;
  /** Re-clicking the active tab: start a new chat with that mode's default agent.
   * Reuses the open chat if it's already an empty one of that same agent, so a
   * repeated tap doesn't pile up blank sessions. */
  startFresh: () => void;
  open: (session: AgentSession) => void;
  /** Open a session by id (a Tasks run → its session). Records the request, fetches
   * the list if needed, and opens it once loaded — suppressing the mode's auto-open
   * of the latest chat in the meantime so the targeted session isn't clobbered. */
  requestOpen: (id: string) => void;
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
  const { listSessions, createSession, chat, chatResume, cancelChatRun } = deps;
  const { listProposals, getTranscript } = deps;
  const { renameSession, deleteSession, archiveSession, unarchiveSession } = deps;
  const { rescopeSession, uploadChatAttachment, getChatCapabilities } = deps;
  const enabled = mode !== null;
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [active, setActive] = useState<AgentSession | null>(null);
  // A pending targeted open (requestOpen) — while set, the mode effect won't auto-
  // open the latest chat, so the requested session isn't clobbered before it loads.
  const pendingOpenRef = useRef<string | null>(null);
  const [panel, setPanel] = useState<Panel>("none");
  const [proposals, setProposals] = useState<ProposalSummary[]>([]);
  const [openProposal, setOpenProposal] = useState<string | null>(null);
  // Live transcript keyed by session id, so an in-flight turn streams into ITS
  // session's buffer even while another chat is on screen — switching away and back
  // can't lose the running render. The visible `messages` is just this map's entry
  // for the active chat.
  const [messagesBySession, setMessagesBySession] = useState<Record<string, TranscriptMessage[]>>(
    {},
  );
  const [busy, setBusy] = useState(false);
  // The chat a turn is streaming into right now (reactive twin of turnSessionRef), so
  // the picker can glyph that row's activity. Null when idle.
  const [activeTurnSessionId, setActiveTurnSessionId] = useState<string | null>(null);
  // Live context-window fill from the stream's `usage` events; cleared when the
  // open chat changes (a different session has its own context).
  const [usage, setUsage] = useState<ContextUsage | null>(null);
  // The in-flight turn's abort handle — the Stop button calls `.abort()`, which
  // closes the SSE fetch and unwinds the run server-side. Null when idle.
  const abortRef = useRef<AbortController | null>(null);
  // The detached turn's server-side run id (from the stream's synthetic `run` event).
  // Stop cancels against it since aborting the fetch no longer ends the turn. Null when idle.
  const runIdRef = useRef<string | null>(null);
  // The session whose turn is streaming right now. The transcript-reload effect leaves
  // this session's buffer alone (its live turn isn't in the stored transcript yet), and
  // a chat switch preserves it — so returning to the chat still shows the running turn.
  const turnSessionRef = useRef<string | null>(null);
  // The agent model's vision capability, false until the check answers — the safe
  // default keeps the chat attach affordance hidden rather than offering one the
  // model would 415. It only ever flips true, so the paperclip appears once
  // vision is confirmed and never flashes a broken state on first paint.
  const [supportsVision, setSupportsVision] = useState(false);
  const [canEditImages, setCanEditImages] = useState(false);
  // The open chat's id — the key the transcript and proposal inbox load against.
  const activeId = active?.id ?? null;
  // The visible transcript: the active chat's buffer (empty until loaded). A stable
  // empty array keeps the reference steady across renders when there's nothing to show.
  const messages = (activeId !== null ? messagesBySession[activeId] : undefined) ?? EMPTY_MESSAGES;
  // The live turn's session + what it's doing, recomputed as its buffer streams (so the
  // glyph flips thinking → rendering when an image tool starts).
  const activeTurn: ActiveTurn | null = activeTurnSessionId
    ? { sessionId: activeTurnSessionId, kind: turnKind(messagesBySession[activeTurnSessionId]) }
    : null;
  // Update one session's buffer, leaving the others untouched — the streaming reducer
  // and the transcript load both go through here so a turn only ever edits its own chat.
  const setSessionMessages = useCallback(
    (sessionId: string, updater: (ms: TranscriptMessage[]) => TranscriptMessage[]) => {
      setMessagesBySession((prev) => ({ ...prev, [sessionId]: updater(prev[sessionId] ?? []) }));
    },
    [],
  );
  // Read in the resolve effect without making it a dependency (which would re-fire
  // it on every open/turn and re-pick the session).
  const activeRef = useRef(active);
  activeRef.current = active;
  // Guards a single auto-create per mode entry against a fast double-fire.
  const creatingFor = useRef<ConvMode | null>(null);

  // Only this mode's agents belong on the tab; the picker creates under them too.
  const agentOptions = mode ? MODE_AGENTS[mode] : ["curator", "teacher", "jerv"];
  // A spawned sub-agent child carries its PERSONA as its agent ("research"/"review"/
  // "summarize"), not the tab's spawner agent, so a plain mode filter would drop every
  // child and the SessionsPanel rail would never see them. Keep a child whenever its
  // parent is a mode-visible top-level chat (so it nests under that chat) — and only
  // then, so a research-mode child never leaks as an orphan row into the fullbrain tab.
  const topLevelIds = new Set(
    mode
      ? sessions
          .filter((s) => !s.parent_session_id && MODE_AGENTS[mode].includes(s.agent))
          .map((s) => s.id)
      : [],
  );
  const visibleSessions = mode
    ? sessions.filter((s) =>
        s.parent_session_id
          ? topLevelIds.has(s.parent_session_id)
          : MODE_AGENTS[mode].includes(s.agent),
      )
    : sessions;

  // Probe the agent model's vision capability once the surface comes on screen —
  // it gates the chat attach affordance. A failed check leaves it false (attach
  // stays hidden), the safe default.
  useEffect(() => {
    if (!enabled) return;
    let stale = false;
    getChatCapabilities()
      .then((c) => {
        if (stale) return;
        setSupportsVision(c.supports_vision);
        setCanEditImages(c.can_edit_images);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [enabled, getChatCapabilities]);

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
    // A targeted open is pending — let the fulfill effect open it; don't auto-open
    // (or auto-create) this mode's latest chat over the top of it.
    if (pendingOpenRef.current !== null) return;
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

  // Fulfill a pending targeted open (requestOpen) once its session is in the list.
  useEffect(() => {
    const want = pendingOpenRef.current;
    if (!enabled || want === null) return;
    const found = sessions.find((s) => s.id === want);
    if (found) {
      pendingOpenRef.current = null;
      open(found);
    }
  }, [enabled, sessions]);

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
    // A turn is streaming live into this chat's buffer — its in-flight render isn't in
    // the stored transcript yet, so reloading would wipe it. Leave the live buffer be;
    // returning to the chat keeps showing the running turn.
    if (activeId === turnSessionRef.current) return;
    let stale = false;
    // A different chat has its own context — drop the prior meter until this one's
    // first turn reports usage (token counts aren't part of the stored transcript).
    setUsage(null);
    getTranscript(activeId)
      .then((turns) => {
        if (!stale) setSessionMessages(activeId, () => turns.map(fromTurn));
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [enabled, activeId, getTranscript, setSessionMessages]);

  async function send(
    textRaw: string,
    opts?: { appointmentId?: string; files?: File[] },
  ): Promise<void> {
    const text = textRaw.trim();
    const files = opts?.files ?? [];
    if ((!text && files.length === 0) || busy) return;
    // No scope yet — surface the picker rather than chatting against nothing.
    if (!active) {
      setPanel("sessions");
      return;
    }
    setBusy(true);
    // Upload any staged files first (in order), so their ids ride the turn and the
    // chips render immediately on the user bubble. An upload failure aborts the
    // send WITHOUT touching the transcript — the omnibox keeps the typed text and
    // staged files so the owner can retry; nothing half-sent lands in the chat.
    let attachments: ChatAttachment[] = [];
    if (files.length > 0) {
      try {
        attachments = [];
        for (const file of files) {
          attachments.push(await uploadChatAttachment(active.id, file));
        }
      } catch {
        setBusy(false);
        throw new AttachmentUploadError();
      }
    }
    const attachmentIds = attachments.map((a) => a.id);
    const history = messages.map((m) => ({ role: m.role, content: historyContent(m) }));
    // Snapshot before the optimistic append so a dropped-stream recovery knows how
    // many turns predate this exchange (reconcile waits for the transcript to grow).
    const baseline = messages.length;
    const turnSessionId = active.id;
    // Mark which chat this turn streams into, so its buffer survives a switch away and
    // back, and its own (pre-turn) transcript reload doesn't clobber the live render.
    turnSessionRef.current = turnSessionId;
    setActiveTurnSessionId(turnSessionId);
    setSessionMessages(turnSessionId, (ms) => [
      ...ms,
      userMessage(text, attachments),
      streamingAssistant(),
    ]);
    // Reuse the note-capture warm fix (only when capture is on and fresh) so the
    // location tool can answer from the phone's current spot.
    const coords = freshCoords();
    const controller = new AbortController();
    abortRef.current = controller;
    runIdRef.current = null;
    const body: ChatRequest = {
      session_id: turnSessionId,
      message: text,
      history,
      ...(opts?.appointmentId ? { appointment_id: opts.appointmentId } : {}),
      ...(coords ? { latitude: coords.latitude, longitude: coords.longitude } : {}),
      ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
    };
    // Uploads succeeded and the turn is under way — `send` resolves HERE so the composer
    // clears the typed text and staged files immediately, rather than staying populated
    // for the whole turn (and, on a dropped connection, the multi-minute recovery). The
    // stream and any reconnect recovery run in the background; `busy` stays true until
    // they finish, so a second turn can't start and clobber this one's optimistic bubbles.
    void runTurn(body, controller, turnSessionId, baseline);
  }

  // Stream one turn to settlement in the background: fold its events into the live
  // bubble, and on a dropped connection recover the finished exchange from the
  // transcript rather than flashing an error. Owns the turn's busy/abort lifecycle, so
  // it must always reach its `finally` (hence the broad catch around the stream).
  async function runTurn(
    body: ChatRequest,
    controller: AbortController,
    turnSessionId: string,
    baseline: number,
  ): Promise<void> {
    // How many SERVER frames we've folded — the offset a reconnect resumes from. The
    // synthetic `run` event is client-made (from the X-Run-Id header), so it doesn't count.
    let framesSeen = 0;
    // The turn's first usage event is its carried-forward floor (history + system +
    // new message, no tools yet); later steps in the same turn only stack transient
    // tool I/O on top. Captured once so the meter can shade base vs. transient.
    let turnBase: number | null = null;
    // Fold one server event into the turn's own chat buffer (not whatever chat is on
    // screen — the owner may be viewing another session while this one streams). Usage
    // rides the whole conversation, so it's tracked apart from the transcript reducer.
    const fold = (event: ChatEvent): void => {
      if (event.type === "usage") {
        if (turnBase === null) turnBase = event.input_tokens;
        setUsage({
          used: event.input_tokens + event.output_tokens,
          base: turnBase,
          window: event.context_window,
        });
      } else {
        setSessionMessages(turnSessionId, (ms) => applyEvent(ms, event));
        // A child was just minted server-side (children persist eagerly) — refresh the
        // sessions list so the manager's rail shows the sub-agent nested under this chat
        // WHILE it runs, not only after the turn settles.
        if (event.type === "subagent_spawned") reloadSessions();
      }
    };
    // Reconnect to the still-running turn and resume folding its live events from where
    // the dropped stream left off. Returns true when it settled live (saw `done`) or was
    // Stopped; false to fall back to the transcript (the run already finished → 404, or
    // the reconnect dropped again before completing).
    const resumeLive = async (): Promise<boolean> => {
      const runId = runIdRef.current;
      if (!runId) return false;
      let settled = false;
      try {
        for await (const event of chatResume(runId, framesSeen, controller.signal)) {
          framesSeen += 1;
          fold(event);
          if (event.type === "done") settled = true;
        }
      } catch {
        if (controller.signal.aborted) {
          setSessionMessages(turnSessionId, (ms) => endStream(ms, "stopped"));
          return true;
        }
        return false;
      }
      return settled;
    };
    try {
      for await (const event of chat(body, controller.signal)) {
        if (event.type === "run") {
          runIdRef.current = event.run_id;
          continue;
        }
        framesSeen += 1;
        fold(event);
      }
      setSessionMessages(turnSessionId, (ms) =>
        ms[ms.length - 1]?.streaming ? endStream(ms, "end_turn") : ms,
      );
    } catch {
      if (controller.signal.aborted) {
        setSessionMessages(turnSessionId, (ms) => endStream(ms, "stopped"));
      } else {
        // The connection dropped (the PWA was backgrounded and the OS closed the socket).
        // The turn runs on detached server-side — RECONNECT and resume its live stream so
        // thinking/render progress picks up where it left off. Only if the run is already
        // gone (or the reconnect drops again) do we recover the finished exchange from the
        // transcript. Both target the turn's own buffer, so they land whether or not that
        // chat is open.
        const resumed = await resumeLive();
        if (!resumed) {
          // The live stream dropped AND the reconnect failed — the turn is unreachable
          // from here. Force-stop the detached server turn now so it (and its sub-agents'
          // in-flight LLM calls) don't keep grinding the GPU for the whole reconcile
          // window after the UI has given up. Idempotent: a no-op if the run already
          // finished. Then recover whatever partial it persisted, else settle to error.
          const rid = runIdRef.current;
          if (rid) void cancelChatRun(rid).catch(() => {});
          const recovered = await reconcile(turnSessionId, baseline);
          if (recovered) setSessionMessages(turnSessionId, () => recovered.map(fromTurn));
          else setSessionMessages(turnSessionId, (ms) => endStream(ms, "error"));
        }
      }
    } finally {
      abortRef.current = null;
      // Cleared only now (not before reconcile) so a Stop during the multi-minute
      // recovery still cancels the detached turn by id; a stale id can't linger into
      // the next turn.
      runIdRef.current = null;
      // The turn settled — release its chat so reopening it now reloads the stored
      // transcript (which carries the finished render) instead of the live buffer, and
      // drop the picker's activity glyph for it.
      if (turnSessionRef.current === turnSessionId) turnSessionRef.current = null;
      setActiveTurnSessionId((cur) => (cur === turnSessionId ? null : cur));
      setBusy(false);
      // The turn may have staged a Proposal — refresh the review inbox.
      reloadProposals();
      // Refresh the session so the panel/top bar pick up server-side changes the
      // turn caused: an auto-generated title (first turn) and the card metadata
      // (turn count, preview, staged count).
      reloadSessions();
    }
  }

  // Poll the transcript after a dropped stream until the detached turn's finished
  // exchange lands (the turn count grows past the pre-send baseline), or the ceiling
  // gives up. Keyed on the turn's own session (not the active one), so recovery lands
  // even if the owner is viewing another chat. Skips polling while backgrounded.
  async function reconcile(sessionId: string, baseline: number): Promise<TranscriptTurn[] | null> {
    const deadline = Date.now() + RECONCILE_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (isForeground()) {
        try {
          const turns = await getTranscript(sessionId);
          if (turns.length > baseline) return turns;
        } catch {}
      }
      await new Promise((r) => setTimeout(r, RECONCILE_INTERVAL_MS));
    }
    return null;
  }

  // The composer's Stop. The turn runs detached server-side, so cancel it by run id;
  // aborting the fetch only closes our stream (the `send` catch then settles the
  // partial bubble "stopped"). `busy` clears in the send finally.
  function stop(): void {
    const runId = runIdRef.current;
    if (runId) void cancelChatRun(runId).catch(() => {});
    abortRef.current?.abort();
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
    const switching = session.id !== active?.id;
    setActive(session);
    setPanel("none");
    // Re-opening the current chat must NOT disturb its buffer (its id is unchanged, so
    // the id-keyed effect wouldn't re-fire to repopulate it). On an actual switch, drop
    // the other chats' cached buffers (the effect reloads the opened one) but PRESERVE
    // the chat with a live turn, so returning to it still shows the running render.
    if (!switching) return;
    setMessagesBySession((prev) => {
      const live = turnSessionRef.current;
      return live && prev[live] ? { [live]: prev[live] } : {};
    });
  }

  function requestOpen(id: string): void {
    pendingOpenRef.current = id;
    const found = sessions.find((s) => s.id === id);
    if (found) {
      pendingOpenRef.current = null;
      open(found);
      return;
    }
    // Not in the loaded list (or the surface was off screen) — fetch so the fulfill
    // effect can open it. Best-effort: a failed fetch just leaves the request pending.
    listSessions()
      .then((all) => {
        setSessions(all);
        setLoaded(true);
      })
      .catch(() => {});
  }

  async function rename(id: string, title: string): Promise<void> {
    await renameSession(id, title);
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)));
    if (active?.id === id) setActive({ ...active, title });
  }

  async function remove(id: string): Promise<void> {
    await deleteSession(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
    // Drop the deleted chat's buffer; if it was the open one, clear the active chat too.
    setMessagesBySession((prev) => {
      const { [id]: _gone, ...rest } = prev;
      return rest;
    });
    // Don't leave the picker's activity glyph pointing at a chat that no longer exists.
    setActiveTurnSessionId((cur) => (cur === id ? null : cur));
    if (active?.id === id) setActive(null);
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
    activeTurn,
    canSend: !busy && active !== null,
    usage,
    stop,
    supportsVision,
    canEditImages,
    // Resolves true once the turn is under way (files uploaded, stream started),
    // false when an upload aborted the send — so the composer keeps its staged
    // files for a retry instead of clearing them.
    send: (text, opts) =>
      send(text, opts).then(
        () => true,
        (err) => {
          if (err instanceof AttachmentUploadError) return false;
          return true; // the stream runs in the background now; any failure settles there
        },
      ),
    create,
    startFresh,
    open,
    requestOpen,
    rename: (id, title) => void rename(id, title).catch(() => {}),
    remove: (id) => void remove(id).catch(() => {}),
    archive: (id) => void archive(id).catch(() => {}),
    unarchive: (id) => void unarchive(id).catch(() => {}),
    rescope: (id, scopes) => void rescope(id, scopes).catch(() => {}),
  };
}
