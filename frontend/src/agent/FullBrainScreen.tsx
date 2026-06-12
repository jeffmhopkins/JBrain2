// The Full Brain conversation surface: a streamed transcript over /api/chat plus
// the composer. Text deltas accumulate into the live assistant bubble; tool
// calls/results show as activity rows and tool_view payloads render through the
// component registry (docs/mocks/assistant-lateral-swipe.html is the spec).

import { type ReactNode, useRef, useState } from "react";
import { api } from "../api/client";
import type { AgentSession, ChatEvent, ChatRequest, ViewPayload } from "./types";
import { ToolView } from "./views/registry";

export interface ToolActivity {
  id: string;
  name: string;
  /** undefined while the call is in flight; set when its result arrives. */
  ok?: boolean;
  summary?: string;
}

export interface TranscriptMessage {
  role: "user" | "assistant";
  text: string;
  tools: ToolActivity[];
  views: ViewPayload[];
  streaming: boolean;
  stopReason?: string;
}

function user(text: string): TranscriptMessage {
  return { role: "user", text, tools: [], views: [], streaming: false };
}

function streamingAssistant(): TranscriptMessage {
  return { role: "assistant", text: "", tools: [], views: [], streaming: true };
}

/** Fold one ChatEvent into the transcript, updating the live assistant turn (the
 * last message). Pure so the streaming reducer is unit-testable. */
export function applyEvent(messages: TranscriptMessage[], event: ChatEvent): TranscriptMessage[] {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant") return messages;
  const next: TranscriptMessage = { ...last };
  switch (event.type) {
    case "text_delta":
      next.text += event.text;
      break;
    case "tool_call":
      next.tools = [...next.tools, { id: event.id, name: event.name }];
      break;
    case "tool_result":
      next.tools = next.tools.map((t) =>
        t.id === event.tool_call_id ? { ...t, ok: event.ok, summary: event.summary } : t,
      );
      break;
    case "tool_view":
      next.views = [...next.views, event.view];
      break;
    case "job_enqueued":
      next.tools = [
        ...next.tools,
        { id: event.job_id, name: "queued", ok: true, summary: event.summary },
      ];
      break;
    case "done":
      next.streaming = false;
      next.stopReason = event.stop_reason;
      break;
  }
  return [...messages.slice(0, -1), next];
}

function endStream(messages: TranscriptMessage[], reason: string): TranscriptMessage[] {
  return applyEvent(messages, { type: "done", stop_reason: reason });
}

type ChatFn = (body: ChatRequest) => AsyncGenerator<ChatEvent>;

interface Props {
  session: AgentSession;
  /** Injected for tests; defaults to the live SSE client. */
  chat?: ChatFn;
  /** Pre-fills the composer (carried from the home Full Brain box). */
  initialDraft?: string;
  /** Open the lateral panels. The swipe is an enhancement; these visible
   * buttons are the reliable path (gestures proved flaky on real devices). */
  onOpenSessions?: () => void;
  onOpenProposals?: () => void;
}

export function FullBrainScreen({
  session,
  chat = api.chat,
  initialDraft = "",
  onOpenSessions,
  onOpenProposals,
}: Props): ReactNode {
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [draft, setDraft] = useState(initialDraft);
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);

  async function send(): Promise<void> {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    // History is the completed turns so far; the new message is sent separately.
    const history = messages.map((m) => ({ role: m.role, content: m.text }));
    setMessages((ms) => [...ms, user(text), streamingAssistant()]);
    try {
      for await (const event of chat({ session_id: session.id, message: text, history })) {
        setMessages((ms) => applyEvent(ms, event));
      }
      setMessages((ms) => (ms[ms.length - 1]?.streaming ? endStream(ms, "end_turn") : ms));
    } catch {
      setMessages((ms) => endStream(ms, "error"));
    } finally {
      setBusy(false);
      // Optional-chain the method too: jsdom (tests) has no scrollIntoView.
      endRef.current?.scrollIntoView?.({ block: "end" });
    }
  }

  const scope = session.domain_scopes.length ? session.domain_scopes.join(" · ") : "all domains";

  return (
    <div className="fullbrain">
      <div className="fb-scope">
        <button type="button" className="fb-nav" aria-label="Sessions" onClick={onOpenSessions}>
          ‹ Sessions
        </button>
        <span className="scopechip">Full Brain · {scope}</span>
        <button type="button" className="fb-nav" aria-label="Proposals" onClick={onOpenProposals}>
          Proposals ›
        </button>
      </div>

      <main className="fb-chat" aria-label="Conversation">
        {messages.map((m, i) => (
          // Transcript is append-only; positional key is stable for the turn.
          // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript
          <Bubble key={i} message={m} />
        ))}
        <div ref={endRef} />
      </main>

      <div className="fb-composer">
        <textarea
          aria-label="Message"
          placeholder="Talk it out — full tool access…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <div className="fb-foot">
          <span className="fb-hint">Full tool access · staged writes need approval</span>
          <button
            type="button"
            aria-label="Send"
            disabled={busy || !draft.trim()}
            onClick={() => void send()}
          >
            ➤
          </button>
        </div>
      </div>
    </div>
  );
}

function Bubble({ message }: { message: TranscriptMessage }): ReactNode {
  if (message.role === "user") {
    return <div className="bubble me">{message.text}</div>;
  }
  return (
    <div className="bubble ai">
      {message.text && <span className="fb-text">{message.text}</span>}
      {message.streaming && !message.text && <span className="fb-typing">…</span>}
      {message.tools.map((t) => (
        <div className="tool" key={t.id}>
          <span className={t.ok === false ? "err" : "ok"}>
            {t.ok === undefined ? "running" : t.ok ? "✓" : "✗"}
          </span>
          <span>
            {t.name}
            {t.summary ? ` · ${t.summary}` : ""}
          </span>
        </div>
      ))}
      {message.views.map((v, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
        <ToolView key={i} payload={v} />
      ))}
    </div>
  );
}
