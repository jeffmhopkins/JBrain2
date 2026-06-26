// Code mode (jcode) — the tabbed session (docs/DESIGN.md "jcode", variant C, icon
// tabs). One session, four views behind an icon-only segmented control: Chat (the
// live coding turn over SSE), Terminal (the raw tool/command log derived from the
// stream), Preview (an ephemeral tunnel to the sandbox dev server, Wave J4), and Diff
// (a placeholder until the diff feed lands). The Chat tab is the workhorse; it streams
// api.jcodeTurn frames.

import { useEffect, useRef, useState } from "react";
import { AgentStatusLine } from "../agent/FullBrainSurface";
import { Markdown } from "../agent/markdown";
import type { AgentStatus } from "../agent/status";
import { usePacedText } from "../agent/usePacedText";
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
import type { JcodeEvent, JcodeModelStatus, JcodePreview, JcodeSession } from "../jcode/types";

// Rough cold-load read rate (s/GB) for the loading-bar estimate — the bar caps at 96%
// until the gateway confirms the model resident, then completes.
const LOAD_SEC_PER_GB = 1.2;

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

// Friendly verb + object per CLI tool the coding agent uses, for the "what's it doing
// now" status line (mirrors agent/status.ts but for jcode's Claude Code toolset). An
// unmapped tool still reads sensibly via the generic "Using <name>" fallback.
const TOOL_VERBS: Record<string, { label: string; emphasis?: string }> = {
  Read: { label: "Reading", emphasis: "a file" },
  Write: { label: "Writing", emphasis: "a file" },
  Edit: { label: "Editing", emphasis: "a file" },
  MultiEdit: { label: "Editing", emphasis: "files" },
  Bash: { label: "Running", emphasis: "a command" },
  Glob: { label: "Finding", emphasis: "files" },
  Grep: { label: "Searching", emphasis: "the code" },
  WebFetch: { label: "Fetching", emphasis: "a page" },
  WebSearch: { label: "Searching", emphasis: "the web" },
  TodoWrite: { label: "Planning", emphasis: "the work" },
  Task: { label: "Delegating", emphasis: "a subtask" },
};

// Derive the live "Thinking… / Editing a file" status for the line above the composer,
// from the last jcode turn. Returns null when idle. Reuses the chat's AgentStatusLine
// view (it takes a plain AgentStatus), so jcode reads the same as Full Brain.
function jcodeStatus(
  item: Extract<Item, { kind: "jcode" }> | null,
  busy: boolean,
): AgentStatus | null {
  if (!item) return null;
  if (busy) {
    const running = item.tools.find((t) => !t.done);
    if (running) {
      const v = TOOL_VERBS[running.tool];
      if (v)
        return { kind: "tool", label: v.label, ...(v.emphasis ? { emphasis: v.emphasis } : {}) };
      return { kind: "tool", label: "Using", emphasis: running.tool };
    }
    if (item.text) return { kind: "answering", label: "Writing the answer" };
    return { kind: "thinking", label: "Thinking it through" };
  }
  const used = item.tools.length;
  return {
    kind: "done",
    label: used ? `Done · ${used} tool${used > 1 ? "s" : ""} used` : "Done",
  };
}

// One assistant turn: markdown-rendered prose (paced into view so a fast local model's
// block-paste reads as steady typing) plus the CLI tool steps it ran. A sub-component so
// usePacedText — a hook — runs per item rather than in a map callback.
function JcodeBubble({
  item,
  streaming,
}: {
  item: Extract<Item, { kind: "jcode" }>;
  streaming: boolean;
}) {
  const shown = usePacedText(item.text, streaming);
  return (
    <div className="jcode-bubble">
      {shown && <Markdown text={shown} streaming={streaming} />}
      {item.tools.length > 0 && (
        <div className="jcode-tools">
          {item.tools.map((tool, j) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: append-only tool list
            <div className={`jcode-tool${tool.done ? " done" : ""}`} key={j}>
              <span className="jcode-tool-name">{tool.tool}</span>
              <span className="jcode-tool-label">{tool.label}</span>
              <span className="jcode-tool-state">{tool.done ? "✓" : "…"}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

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
  const [preview, setPreview] = useState<JcodePreview | null>(null);
  const [pvBusy, setPvBusy] = useState(false);
  const [model, setModel] = useState<JcodeModelStatus | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const loadStart = useRef(Date.now());
  const runId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  // Poll the coder's warm state so the loading bar tracks the real load while it comes
  // onto the box. We key the bar off `warming` — the backend's warm-task signal — NOT
  // `loaded`: the gateway lists a model as resident the moment a load is *requested*, so
  // `loaded` races true before the weights finish and would hide the bar mid-load. The
  // `hosting && !loaded` fallback keeps the bar honest when opening an EXISTING session
  // whose model was since evicted (no fresh warm fires there). Keep polling until settled
  // (hosting off, or resident and no warm in flight). A failed poll just retries.
  useEffect(() => {
    let stale = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const s = await api.jcodeModelStatus();
        if (stale) return;
        setModel(s);
        if (!s.hosting || (s.loaded && !s.warming)) return;
      } catch {
        if (stale) return;
      }
      timer = setTimeout(poll, 2000);
    };
    poll();
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, []);

  const loading = model?.hosting === true && (model.warming === true || !model.loaded);
  // Tick the estimate while loading so the bar advances between polls, and anchor the
  // estimate to when warming actually began (not screen mount).
  useEffect(() => {
    if (!loading) return;
    loadStart.current = Date.now();
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [loading]);
  const sizeGb = model?.size_gb ?? 0;
  const elapsedSec = (now - loadStart.current) / 1000;
  const loadPct =
    sizeGb > 0 ? Math.min(96, Math.round((elapsedSec / (sizeGb * LOAD_SEC_PER_GB)) * 100)) : 0;

  // The live coding turn drives the status line above the composer ("Editing a file…").
  let lastJcode: Extract<Item, { kind: "jcode" }> | null = null;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it && it.kind === "jcode") {
      lastJcode = it;
      break;
    }
  }
  const status = jcodeStatus(lastJcode, busy);

  // Fetch the preview status the first time the Preview tab is opened (the feature
  // flag + any already-live tunnel). Failures leave it null → a neutral empty state.
  useEffect(() => {
    if (tab !== "prev" || preview !== null) return;
    let stale = false;
    api
      .jcodePreviewStatus(session.id)
      .then((p) => {
        if (!stale) setPreview(p);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [tab, preview, session.id]);

  async function openPreview() {
    setPvBusy(true);
    try {
      setPreview(await api.jcodePreviewOpen(session.id));
    } catch {
      setPreview({ enabled: true, url: null });
    } finally {
      setPvBusy(false);
    }
  }

  async function closePreview() {
    setPvBusy(true);
    try {
      await api.jcodePreviewClose(session.id);
      setPreview({ enabled: true, url: null });
    } finally {
      setPvBusy(false);
    }
  }

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
      // A user-initiated Stop aborts the fetch — that's not a failure, so don't
      // annotate the bubble; only a genuine drop reads as interrupted (review S3).
      if (!ctrl.signal.aborted) {
        patchLastJcode((it) => {
          it.text += it.text ? "\n\n(stream interrupted)" : "(stream interrupted)";
        });
      }
    } finally {
      setBusy(false);
      runId.current = null;
      abort.current = null;
    }
  }

  function applyEvent(ev: JcodeEvent) {
    // `done` needs no case — the for-await loop ending IS the completion signal
    // (finally clears `busy`); we only fold text/tool/error frames here.
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

  // Tearing the screen down (Back/unmount) must not strand a live turn: the turn
  // runs DETACHED server-side (like /chat), so aborting the fetch alone leaves the
  // sandbox running it — stop() also fires cancelJcodeRun. The unmount effect aborts
  // the fetch so the generator can't setState after we're gone (review B1).
  useEffect(() => () => abort.current?.abort(), []);

  function closeSession() {
    if (busy) stop();
    onClose();
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
        <button
          type="button"
          className="icon-btn"
          onClick={closeSession}
          aria-label="Back to sessions"
        >
          <ChevronLeftIcon size={22} />
        </button>
        <span className="jcode-sesshead">
          <span className="jcode-sd live" />
          <span className="jcode-repo">{session.repo || "scratch"}</span>
          <span className="jcode-branch">@ {session.work_branch || session.branch}</span>
        </span>
        <span className="jcode-modelchip">{model?.model ?? "qwen3-coder-next"} · on-box</span>
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
          {loading && model && (
            <div className="jcode-modelload" aria-label="Loading model">
              <div className="jcode-modelload-row">
                <span>Loading {model.model} onto the box…</span>
                <span className="jcode-modelload-pct">{loadPct}%</span>
              </div>
              <div className="jcode-modelload-track">
                <div className="jcode-modelload-fill" style={{ width: `${loadPct}%` }} />
              </div>
            </div>
          )}
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
                  <JcodeBubble item={it} streaming={busy && i === items.length - 1} />
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
          {preview === null ? (
            <p className="jcode-empty">Loading…</p>
          ) : !preview.enabled ? (
            <p className="jcode-empty">
              Web preview isn't enabled on this server. Turn it on with
              <code> jcode-setup.sh</code> — it opens a temporary tunnel to the sandbox's dev
              server.
            </p>
          ) : preview.url ? (
            <div className="jcode-preview">
              <div className="jcode-pvurl">
                <a href={preview.url} target="_blank" rel="noreferrer noopener">
                  {preview.url}
                </a>
                <button
                  type="button"
                  className="jcode-act"
                  onClick={() => navigator.clipboard?.writeText(preview.url ?? "")}
                >
                  Copy
                </button>
              </div>
              <p className="jcode-empty">
                A temporary tunnel to the sandbox's dev server — dies with the session, never
                indexed. Anyone with this URL can reach it while it's live.
              </p>
              <button
                type="button"
                className="jcode-act danger"
                disabled={pvBusy}
                onClick={closePreview}
              >
                {pvBusy ? "Stopping…" : "Stop preview"}
              </button>
            </div>
          ) : (
            <div className="jcode-preview">
              <p className="jcode-empty">
                Start your dev server in the sandbox (e.g. <code>npm run dev</code> on{" "}
                <code>:5173</code>), then open a temporary public URL to it.
              </p>
              <button
                type="button"
                className="jcode-act teal"
                disabled={pvBusy}
                onClick={openPreview}
              >
                {pvBusy ? "Opening…" : "Open preview tunnel"}
              </button>
            </div>
          )}
        </div>
      )}

      {tab === "chat" && (
        <div className="jcode-composer">
          <AgentStatusLine status={status} />
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
          {/* Context bar: what the agent is working against — the model, the sandbox
              work-branch, and where it runs. (A live context-window meter lands once the
              turn stream carries token usage.) */}
          <div className="jcode-cfoot">
            <span className="jcode-cdot" />
            <span className="jcode-cmodel">{model?.model ?? "qwen3-coder-next"}</span>
            <span className="jcode-csep">·</span>
            <span className="jcode-cbranch">{session.work_branch || session.branch}</span>
            <span className="jcode-csep">·</span>
            <span>on-box</span>
          </div>
        </div>
      )}
    </section>
  );
}
