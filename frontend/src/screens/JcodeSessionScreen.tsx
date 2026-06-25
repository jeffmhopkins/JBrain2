// Code mode (jcode) — the tabbed session (docs/DESIGN.md "jcode", variant C, icon
// tabs). One session, four views behind an icon-only segmented control: Chat (the
// live coding turn over SSE), Terminal (the raw tool/command log derived from the
// stream), Diff and Preview (placeholders until the diff feed / preview tunnel land
// in later waves). The Chat tab is the workhorse; it streams api.jcodeTurn frames.

import { useRef, useState } from "react";
import { api } from "../api/client";
import {
  ChevronLeftIcon,
  GitCompareIcon,
  GlobeIcon,
  MessageIcon,
  SendIcon,
  StopIcon,
  TerminalIcon,
} from "../components/icons";
import type { JcodeEvent, JcodeSession } from "../jcode/types";

type Tab = "chat" | "diff" | "term" | "prev";

interface Tool {
  tool: string;
  label: string;
  done: boolean;
}
type Item = { kind: "you"; text: string } | { kind: "jcode"; text: string; tools: Tool[] };

const TABS: { id: Tab; label: string; icon: typeof MessageIcon }[] = [
  { id: "chat", label: "Chat", icon: MessageIcon },
  { id: "diff", label: "Diff", icon: GitCompareIcon },
  { id: "term", label: "Terminal", icon: TerminalIcon },
  { id: "prev", label: "Preview", icon: GlobeIcon },
];

export function JcodeSessionScreen({
  session,
  onClose,
}: {
  session: JcodeSession;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("chat");
  const [items, setItems] = useState<Item[]>([]);
  const [term, setTerm] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"reset" | "delete" | null>(null);
  const runId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  function patchLastJcode(fn: (it: Extract<Item, { kind: "jcode" }>) => void) {
    setItems((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        const it = next[i];
        if (it && it.kind === "jcode") {
          const copy = { ...it, tools: [...it.tools] };
          fn(copy);
          next[i] = copy;
          break;
        }
      }
      return next;
    });
  }

  async function send() {
    const prompt = input.trim();
    if (!prompt || busy) return;
    setInput("");
    setBusy(true);
    setItems((p) => [...p, { kind: "you", text: prompt }, { kind: "jcode", text: "", tools: [] }]);
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      for await (const ev of api.jcodeTurn(session.id, prompt, ctrl.signal)) {
        applyEvent(ev);
      }
    } catch {
      patchLastJcode((it) => {
        it.text += it.text ? "\n\n(stream interrupted)" : "(stream interrupted)";
      });
    } finally {
      setBusy(false);
      runId.current = null;
      abort.current = null;
    }
  }

  function applyEvent(ev: JcodeEvent) {
    if (ev.type === "run") {
      runId.current = ev.run_id;
      return;
    }
    if (ev.type === "text" && ev.text) {
      patchLastJcode((it) => {
        it.text += ev.text;
      });
    } else if (ev.type === "tool_use") {
      const label = String(ev.data?.command ?? ev.tool ?? "tool");
      patchLastJcode((it) => it.tools.push({ tool: ev.tool ?? "tool", label, done: false }));
      setTerm((t) => [...t, `$ ${label}`]);
    } else if (ev.type === "tool_result") {
      patchLastJcode((it) => {
        const last = it.tools[it.tools.length - 1];
        if (last) last.done = true;
      });
      const out = ev.text || (ev.data?.ok ? "ok" : "");
      if (out) setTerm((t) => [...t, out]);
    } else if (ev.type === "error" && ev.text) {
      patchLastJcode((it) => {
        it.text += `\n\n⚠ ${ev.text}`;
      });
    }
  }

  function stop() {
    abort.current?.abort();
    if (runId.current) void api.cancelJcodeRun(runId.current);
  }

  async function doConfirm() {
    if (confirm === "reset") {
      await api.jcodeResetSession(session.id);
      setTerm((t) => [...t, "— sandbox reset —"]);
    } else if (confirm === "delete") {
      await api.jcodeDeleteSession(session.id);
      onClose();
      return;
    }
    setConfirm(null);
  }

  return (
    <section className="jcode-screen">
      <header className="jcode-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to sessions">
          <ChevronLeftIcon size={22} />
        </button>
        <span className="jcode-sesshead">
          <span className="jcode-sd live" />
          <span className="jcode-repo">{session.repo || "scratch"}</span>
          <span className="jcode-branch">@ {session.work_branch || session.branch}</span>
        </span>
        <span className="jcode-modelchip">qwen 80B-A3B · on-box</span>
      </header>

      <div className="jcode-actions">
        <button
          type="button"
          className={`jcode-act${confirm === "reset" ? " armed" : ""}`}
          onClick={() => (confirm === "reset" ? doConfirm() : setConfirm("reset"))}
        >
          {confirm === "reset" ? "Tap again — wipes changes" : "Reset"}
        </button>
        <button
          type="button"
          className={`jcode-act danger${confirm === "delete" ? " armed" : ""}`}
          onClick={() => (confirm === "delete" ? doConfirm() : setConfirm("delete"))}
        >
          {confirm === "delete" ? "Tap again — deletes session" : "Delete"}
        </button>
      </div>

      <div className="jcode-tabs" role="tablist" aria-label="Session views">
        {TABS.map((t) => {
          const Glyph = t.icon;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              aria-label={t.label}
              title={t.label}
              className={`jcode-tab ${t.id}${tab === t.id ? " on" : ""}`}
              onClick={() => setTab(t.id)}
            >
              <Glyph size={20} />
            </button>
          );
        })}
      </div>

      {tab === "chat" && (
        <div className="jcode-panel">
          {items.length === 0 ? (
            <p className="jcode-empty">
              Tell jcode what to build — it works on the box, in the sandbox.
            </p>
          ) : (
            items.map((it, i) =>
              it.kind === "you" ? (
                // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript, stable order
                <div className="jcode-msg you" key={i}>
                  <div className="jcode-bubble you">{it.text}</div>
                </div>
              ) : (
                // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript, stable order
                <div className="jcode-msg" key={i}>
                  <div className="jcode-bubble">
                    {it.text}
                    {it.tools.map((tool, j) => (
                      // biome-ignore lint/suspicious/noArrayIndexKey: append-only tool list
                      <span className="jcode-tool" key={j}>
                        <span className="jcode-tool-name">{tool.tool}</span>
                        <span className="jcode-tool-label">{tool.label}</span>
                        <span className={`jcode-tool-state${tool.done ? " ok" : ""}`}>
                          {tool.done ? "✓" : "…"}
                        </span>
                      </span>
                    ))}
                  </div>
                </div>
              ),
            )
          )}
        </div>
      )}

      {tab === "term" && (
        <div className="jcode-panel">
          <pre className="jcode-term">{term.length === 0 ? "$ █" : `${term.join("\n")}\n$ █`}</pre>
        </div>
      )}

      {tab === "diff" && (
        <div className="jcode-panel">
          <p className="jcode-empty">
            File changes show here as jcode edits the checkout. (Structured diffs land in a later
            update.)
          </p>
        </div>
      )}

      {tab === "prev" && (
        <div className="jcode-panel">
          <p className="jcode-empty">
            A web preview — a temporary tunnel to the sandbox's dev server — arrives in a later
            update.
          </p>
        </div>
      )}

      {tab === "chat" && (
        <div className="jcode-composer">
          <div className="jcode-cbox">
            <textarea
              rows={1}
              placeholder="Tell jcode what to build…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            {busy ? (
              <button type="button" className="jcode-send stop" onClick={stop} aria-label="Stop">
                <StopIcon size={18} />
              </button>
            ) : (
              <button
                type="button"
                className="jcode-send"
                onClick={send}
                disabled={!input.trim()}
                aria-label="Send"
              >
                <SendIcon size={18} />
              </button>
            )}
          </div>
          <div className="jcode-cfoot">
            <span className="jcode-cdot" /> Qwen3-Coder-Next 80B-A3B · on-box
          </div>
        </div>
      )}
    </section>
  );
}
