// The guided-intake recipient PWA (W5): a self-contained, public stepper a stranger
// holding a share link walks — Welcome → Interview → Review → Done. Mirrors the jcode
// share-app pattern (redeem the fragment secret for a session cookie, then run a scoped
// surface) and the binding mock docs/mocks/guided-intake/intake-b-stepper.html.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import { parseIntakeSecret } from "./share";
import "./intake.css";
import type { IntakeConfig } from "./types";

type Phase = "loading" | "welcome" | "interview" | "review" | "done" | "dead";

interface Msg {
  who: "you" | "guide";
  text: string;
  streaming?: boolean;
}

const STEPS = ["Welcome", "Interview", "Review", "Done"];
const STEP_OF: Record<Phase, number> = {
  loading: 0,
  welcome: 0,
  interview: 1,
  review: 2,
  done: 3,
  dead: 0,
};

export function GuidedIntakeApp(): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [config, setConfig] = useState<IntakeConfig | null>(null);
  const [name, setName] = useState("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const streamRef = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  // Redeem the fragment secret once, then strip it from the URL so it can't linger in
  // history or be re-shared — exactly the jcode share flow.
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const secret = parseIntakeSecret();
    if (!secret) {
      setPhase("dead");
      return;
    }
    void (async () => {
      try {
        const cfg = await api.intakeRedeem(secret);
        window.history.replaceState(null, "", "/intake");
        setConfig(cfg);
        setPhase("welcome");
      } catch {
        setPhase("dead");
      }
    })();
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: re-scroll on every new message (the effect reads the DOM, not `messages`, but must re-run on it).
  useEffect(() => {
    streamRef.current?.scrollTo?.({ top: streamRef.current.scrollHeight });
  }, [messages]);

  const runTurn = useCallback(async (text: string) => {
    setBusy(true);
    setError("");
    setMessages((m) => [...m, { who: "guide", text: "", streaming: true }]);
    try {
      for await (const ev of api.intakeChat(text)) {
        if (ev.type === "text_delta") {
          const delta = ev.text;
          setMessages((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (last?.who === "guide") next[next.length - 1] = { ...last, text: last.text + delta };
            return next;
          });
        }
      }
    } catch (e) {
      const detail = e instanceof ApiError ? e.message : "Something went wrong — please try again.";
      setMessages((m) => {
        const next = [...m];
        const last = next[next.length - 1];
        if (last?.who === "guide" && !last.text) next.pop();
        return next;
      });
      setError(detail);
    } finally {
      setMessages((m) =>
        m.map((msg, i) => (i === m.length - 1 ? { ...msg, streaming: false } : msg)),
      );
      setBusy(false);
    }
  }, []);

  function begin(): void {
    setPhase("interview");
    const greeting = name.trim() ? `Hi, I'm ${name.trim()}.` : "Hi, I'm here to help with this.";
    setMessages([{ who: "you", text: greeting }]);
    void runTurn(greeting);
  }

  function send(): void {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { who: "you", text }]);
    void runTurn(text);
  }

  async function confirm(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      await api.intakeConfirm(config?.capture_enterer_name ? name.trim() : "");
      setPhase("done");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't send — please try again.");
    } finally {
      setBusy(false);
    }
  }

  const lastGuide = [...messages].reverse().find((m) => m.who === "guide" && m.text.trim());
  const canBegin = !config?.capture_enterer_name || name.trim().length >= 2;
  const hasExchange = messages.some((m) => m.who === "guide" && m.text.trim());

  return (
    <div className="intake-phone">
      <header className="intake-head">
        <div className="intake-head-top">
          <span className="intake-mark" aria-hidden="true">
            ◆
          </span>
          <div>
            <div className="intake-ttl">A private collection link</div>
            <div className="intake-sub">You've been asked to share some information</div>
          </div>
        </div>
        <ol className="intake-stepper" aria-label="Progress">
          {STEPS.map((label, i) => {
            const at = STEP_OF[phase];
            const cls = i < at ? "done" : i === at ? "active" : "";
            return (
              <li key={label} className={`intake-step ${cls}`}>
                <span className="intake-dot">{i < at ? "✓" : i + 1}</span>
                <span className="intake-nm">{label}</span>
              </li>
            );
          })}
        </ol>
      </header>

      {phase === "loading" && <div className="intake-body intake-center">Opening your link…</div>}

      {phase === "dead" && (
        <div className="intake-body intake-center">
          <h1>This link can't be opened</h1>
          <p className="intake-blurb">
            It may have expired, been used up, or been revoked. Ask whoever sent it to share a fresh
            link.
          </p>
        </div>
      )}

      {phase === "welcome" && config && (
        <>
          <div className="intake-body">
            <h1>You've been invited to share some details</h1>
            <p className="intake-blurb">
              {config.opening_blurb || "Please answer a few questions."}
            </p>
            {config.capture_enterer_name && (
              <div className="intake-field">
                <label htmlFor="intake-name">Your name</label>
                <input
                  id="intake-name"
                  type="text"
                  placeholder="e.g. Carol Hopkins"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
            )}
            <div className="intake-consent">
              <span aria-hidden="true">🛡️</span>
              <span>
                What you share is sent privately to the link's owner for their own records. They
                review it before anything is saved. You can stop at any time.
              </span>
            </div>
          </div>
          <footer className="intake-foot">
            <button
              type="button"
              className="intake-btn primary"
              disabled={!canBegin}
              onClick={begin}
            >
              Begin interview →
            </button>
          </footer>
        </>
      )}

      {phase === "interview" && (
        <>
          <div className="intake-stream" ref={streamRef}>
            {messages.map((m, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript; the last (streaming) message mutates in place, so the index is its stable id.
              <div key={i} className={`intake-msg ${m.who === "you" ? "you" : ""}`}>
                <div className="intake-who">{m.who === "you" ? "You" : "Interviewer"}</div>
                {m.streaming && !m.text ? (
                  <div className="intake-typing" aria-label="typing">
                    <i />
                    <i />
                    <i />
                  </div>
                ) : (
                  <div className="intake-bubble">{m.text}</div>
                )}
              </div>
            ))}
            {error && <div className="intake-error">{error}</div>}
          </div>
          <div className="intake-dock">
            {hasExchange && !busy && (
              <button type="button" className="intake-link-btn" onClick={() => setPhase("review")}>
                Done answering? Review &amp; send →
              </button>
            )}
            <div className="intake-omnibox">
              <textarea
                className="intake-input"
                rows={1}
                placeholder="Type your answer…"
                value={input}
                disabled={busy}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              <button
                type="button"
                className="intake-send"
                aria-label="Send"
                disabled={busy || !input.trim()}
                onClick={send}
              >
                ↑
              </button>
            </div>
          </div>
        </>
      )}

      {phase === "review" && (
        <>
          <div className="intake-body">
            <h1>Does this look right?</h1>
            <div className="intake-sumcard">
              <div className="intake-sc-head">
                <div className="intake-sc-t">Your summary</div>
                <div className="intake-sc-meta">
                  {config?.capture_enterer_name && name.trim()
                    ? `Entered by ${name.trim()}`
                    : "Ready to send"}
                </div>
              </div>
              <div className="intake-sc-body">{lastGuide?.text || "No summary yet."}</div>
            </div>
            <p className="intake-reassure">
              If anything's wrong, go back and tell the interviewer — they'll update it.
            </p>
            {error && <div className="intake-error">{error}</div>}
          </div>
          <footer className="intake-foot intake-row">
            <button
              type="button"
              className="intake-btn"
              disabled={busy}
              onClick={() => setPhase("interview")}
            >
              ← Fix something
            </button>
            <button type="button" className="intake-btn go" disabled={busy} onClick={confirm}>
              Looks right → send
            </button>
          </footer>
        </>
      )}

      {phase === "done" && (
        <div className="intake-body intake-done">
          <div className="intake-done-mark" aria-hidden="true">
            ✓
          </div>
          <h2>Sent for review</h2>
          <p>
            Thank you. Your answers have been sent to the owner, who'll review them before adding
            anything to their records. You can close this page.
          </p>
        </div>
      )}
    </div>
  );
}
