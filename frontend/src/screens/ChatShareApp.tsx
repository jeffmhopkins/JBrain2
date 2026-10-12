// The public chat-share entry: when the PWA is opened at /chat/s/{token} (a copied share link),
// this — not the full owner app — mounts. It reads the chat straight from the public
// /api/chat-share endpoint and renders it through the SAME <AgentTranscript> the owner's chat
// uses, so Thought, Worked, step rows and citations read and expand identically. No login, no
// top bar, no omnibox; each load shows the chat as it stands then. Chosen GUI:
// docs/mocks/chat-share (variant B, live as of each load).

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { AgentTranscript } from "../agent/FullBrainSurface";
import { parseChatSharePath } from "../agent/chatShare";
import { ShareModeContext } from "../agent/shareMode";
import type { FullBrain } from "../agent/useFullBrain";
import { fromTurn } from "../agent/useFullBrain";
import { ApiError, type SharedChat, api, setFaviconSource } from "../api/client";
import { LinkIcon, LockIcon, RefreshIcon } from "../components/icons";

type State =
  | { phase: "loading" }
  | { phase: "gone" }
  | { phase: "error" }
  | { phase: "chat"; chat: SharedChat; at: Date };

const noop = (): void => {};

/** The mode a chat ran in, by its agent — the band names it the way the omnibox does. */
function modeLabel(agent: string): string | null {
  if (agent === "curator") return "Brain";
  if (agent === "jerv" || agent === "teacher") return "Research";
  return null;
}

function asOf(at: Date): string {
  return at.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** A read-only stand-in for the live chat hook: the transcript reads the turns and nothing on
 * it can send, stop, open a panel or answer a question. */
function readOnlyChat(chat: SharedChat): FullBrain {
  return {
    active: {
      id: "shared",
      title: chat.title,
      status: "active",
      agent: chat.agent,
      domain_scopes: [],
      subject_ids: [],
      created_at: chat.last_active_at,
      last_active_at: chat.last_active_at,
    },
    sessions: [],
    agentOptions: [],
    proposals: [],
    reloadProposals: noop,
    reloadSessions: noop,
    panel: "none",
    setPanel: noop,
    openProposal: null,
    setOpenProposal: noop,
    messages: chat.turns.map(fromTurn),
    busy: false,
    activeTurn: null,
    canSend: false,
    usage: null,
    stop: noop,
    send: async () => false,
    openQuestions: [],
    answers: {},
    setAnswer: noop,
    requestOpen: noop,
  } as unknown as FullBrain;
}

export function ChatShareApp(): ReactNode {
  const token = parseChatSharePath();
  const [state, setState] = useState<State>({ phase: "loading" });

  useEffect(() => {
    if (!token) {
      setState({ phase: "gone" });
      return;
    }
    // The owner's favicon route is owner-gated; this page's own route serves the icons of the
    // sites this chat reached.
    setFaviconSource(
      (host) =>
        `/api/chat-share/${encodeURIComponent(token)}/favicon?host=${encodeURIComponent(host)}`,
    );
    let stale = false;
    api.chatShareView(token).then(
      (chat) => {
        if (!stale) setState({ phase: "chat", chat, at: new Date() });
      },
      (e: unknown) => {
        if (stale) return;
        setState({ phase: e instanceof ApiError && e.status === 404 ? "gone" : "error" });
      },
    );
    return () => {
      stale = true;
    };
  }, [token]);

  const fb = useMemo(() => (state.phase === "chat" ? readOnlyChat(state.chat) : null), [state]);

  if (state.phase === "loading") {
    return (
      <main className="cs-page">
        <p className="cs-empty" aria-live="polite">
          Loading…
        </p>
      </main>
    );
  }
  if (state.phase === "gone" || state.phase === "error") {
    const gone = state.phase === "gone";
    return (
      <main className="cs-page">
        <div className="cs-gone">
          <span className="cs-gone-disc" aria-hidden="true">
            <LinkIcon size={24} />
          </span>
          <h1 className="cs-gone-ttl">
            {gone ? "This link no longer works" : "Couldn’t load this chat"}
          </h1>
          <p className="cs-gone-sub">
            {gone
              ? "The owner stopped sharing this chat. Ask them for a new link if you still need it."
              : "Check your connection and reload the page."}
          </p>
        </div>
      </main>
    );
  }

  const { chat, at } = state;
  const turns = chat.turns.filter((t) => t.role === "user").length;
  const mode = modeLabel(chat.agent);
  return (
    <main className="cs-page">
      <header className="cs-band">
        <div className="cs-kick">
          <LinkIcon size={14} /> Shared chat · read-only
        </div>
        <h1 className="cs-title">{chat.title || "Untitled chat"}</h1>
        <div className="cs-who">
          <span>As of {asOf(at)}</span>
          {mode && (
            <>
              <span aria-hidden="true">·</span>
              <span className="cs-mode">{mode}</span>
            </>
          )}
          <span aria-hidden="true">·</span>
          <span>
            {turns} turn{turns === 1 ? "" : "s"}
          </span>
          <button type="button" className="cs-reload" onClick={() => window.location.reload()}>
            <RefreshIcon size={12} /> Reload for newer turns
          </button>
        </div>
      </header>
      <ShareModeContext.Provider value={true}>
        <div className="fb-shell cs-shell">
          {fb && <AgentTranscript fb={fb} emptyText="This chat has no turns yet." />}
        </div>
      </ShareModeContext.Provider>
      <footer className="cs-foot">
        <LockIcon size={15} />
        <span>
          Shared from{" "}
          <b className="cs-wordmark">
            JBrain<span>.</span>
          </b>{" "}
          by its owner. You can read this chat; you can’t reply, open its notes, or see anything
          else in their account.
        </span>
      </footer>
    </main>
  );
}
