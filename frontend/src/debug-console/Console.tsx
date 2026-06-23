// The owner debug console — a standalone, token-authenticated page (not part of
// the cookie-authed PWA). It drives the /api/debug/* surface: prompt completion,
// read-only SQL, logs, and live LLM routing, with the token's own kill switch
// (Suspend / Revoke) top-right, and a LIVE activity pane that shows every command
// hitting the box — including ones an external assistant runs, not just this tab's.
//
// API calls are SAME-ORIGIN (relative paths): the page talks to whatever host
// served it, so the LAN-only console works against the box over jbrain.local even
// though the token it carries points an external assistant at the public host. The
// token supplies only the bearer key; its embedded host is for those other clients.
//
// The key is cached in localStorage so a refresh auto-reconnects; it is cleared on
// Revoke. (A deliberate convenience trade — see the PWA copy on the token's reach.)

import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { type DebugToken, decodeToken } from "./payload";

type CmdType = "complete" | "sql" | "logs" | "routing" | "switch" | "model";
type SessionState = "active" | "suspended" | "revoked";

interface HistoryEntry {
  id: number;
  type: string; // command label, also drives the badge color (dbg-b-<type>)
  summary: string;
  detail: string;
  status: "ok" | "err" | "pending";
  output: string;
}

interface ActivityEvent {
  seq: number;
  ts: string;
  method: string;
  path: string;
  status: number;
  kind: string;
  detail: string;
  client: string;
}

interface Whoami {
  label: string;
  scopes: string[];
}

interface CallResult {
  ok: boolean;
  status: number;
  text: string;
}

const STORAGE_KEY = "jbrain.debug.token";
const CLIENT_KEY = "jbrain.debug.client";

// A STABLE per-browser id (persisted), so the console recognises its own calls
// across refreshes and filters them out of the live feed. A fresh id per page-load
// would make every reconnect's whoami look like "another client".
function clientIdFor(): string {
  const fresh = (globalThis.crypto?.randomUUID?.() ?? String(Math.random())).slice(0, 16);
  try {
    const saved = localStorage.getItem(CLIENT_KEY);
    if (saved) return saved;
    localStorage.setItem(CLIENT_KEY, fresh);
  } catch {
    /* private mode — a per-session id is fine */
  }
  return fresh;
}

const CMD_LABELS: Record<CmdType, string> = {
  complete: "complete — run a prompt",
  sql: "sql — read-only query",
  logs: "logs — tail a service",
  routing: "routing — show live table",
  switch: "switch — route a task",
  model: "model — load / unload",
};

function pretty(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

function loadSavedToken(): DebugToken | null {
  const fromHash = decodeToken(window.location.hash.slice(1));
  if (fromHash) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(fromHash));
    } catch {
      /* private mode — fall back to in-memory only */
    }
    return fromHash;
  }
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) return JSON.parse(saved) as DebugToken;
  } catch {
    /* ignore */
  }
  return null;
}

export function Console() {
  const clientId = useRef<string>(clientIdFor());

  const [token, setToken] = useState<DebugToken | null>(loadSavedToken);
  const [connected, setConnected] = useState(false);
  const [paste, setPaste] = useState("");
  const [whoami, setWhoami] = useState<Whoami | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [session, setSession] = useState<SessionState>("active");

  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const nextId = useRef(1);

  // Composer fields (a superset; each command reads only the ones it needs).
  const [cmd, setCmd] = useState<CmdType>("complete");
  const [text, setText] = useState("");
  const [task, setTask] = useState("");
  const [strength, setStrength] = useState("");
  const [system, setSystem] = useState("");
  const [service, setService] = useState("api");
  const [tail, setTail] = useState("200");
  const [provider, setProvider] = useState("");
  const [effort, setEffort] = useState("");
  const [modelId, setModelId] = useState("");
  const [modelAction, setModelAction] = useState<"load" | "unload">("load");
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  // Same-origin call: relative path, so it targets the host that served the page.
  const call = useCallback(
    async (key: string, method: string, path: string, body?: unknown): Promise<CallResult> => {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${key}`,
        "X-Debug-Client": clientId.current,
      };
      const init: RequestInit = { method, headers };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      try {
        const res = await fetch(path, init);
        return { ok: res.ok, status: res.status, text: await res.text() };
      } catch (e) {
        return { ok: false, status: 0, text: `network error: ${String(e)}` };
      }
    },
    [],
  );

  // Drop the secret from the address bar once read (it is cached in localStorage).
  useEffect(() => {
    if (window.location.hash) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, []);

  const connect = useCallback(
    async (t: DebugToken) => {
      setConnectError(null);
      const res = await call(t.key, "GET", "/api/debug/whoami");
      if (res.ok) {
        setWhoami(JSON.parse(res.text) as Whoami);
        setSession("active");
        setConnected(true);
      } else {
        setConnected(false);
        setConnectError(
          res.status === 401
            ? "This token is invalid, expired, suspended, or revoked."
            : `Could not reach the box (HTTP ${res.status || "—"}).`,
        );
      }
    },
    [call],
  );

  useEffect(() => {
    if (token) void connect(token);
  }, [token, connect]);

  // Live activity: poll the server feed while connected, and fold in any command
  // issued by ANOTHER client (e.g. the assistant over the public host) — our own
  // calls carry our client id and are already shown locally, so we skip them.
  useEffect(() => {
    if (!token || !connected || session !== "active") return;
    let stop = false;
    let after: number | undefined;
    const tick = async () => {
      const path =
        after === undefined ? "/api/debug/activity" : `/api/debug/activity?after=${after}`;
      const res = await call(token.key, "GET", path);
      if (stop || !res.ok) return;
      const data = JSON.parse(res.text) as { events: ActivityEvent[]; last: number };
      after = data.last;
      const external = data.events.filter((e) => e.client !== clientId.current);
      if (external.length) {
        setHistory((h) => [
          ...external
            .slice()
            .reverse()
            .map((e) => ({
              id: -e.seq,
              type: e.kind,
              // Show the actual command (SQL/prompt/routing change) when present,
              // so the feed reads as "what ran", not just the route.
              summary: e.detail || `${e.method} ${e.path.replace("/api/debug/", "")}`,
              detail: `${fmtTime(e.ts)} · HTTP ${e.status}`,
              status: (e.status < 400 ? "ok" : "err") as "ok" | "err",
              output: `Issued from another client (e.g. the assistant).\n\n${e.method} ${e.path} → HTTP ${e.status}\n${e.ts}${e.detail ? `\n\n${e.detail}` : ""}`,
            })),
          ...h,
        ]);
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 1500);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [token, connected, session, call]);

  const record = useCallback((entry: Omit<HistoryEntry, "id">): number => {
    const id = nextId.current++;
    setHistory((h) => [{ id, ...entry }, ...h]);
    setSelected(id);
    return id;
  }, []);

  const settle = useCallback((id: number, patch: Partial<HistoryEntry>) => {
    setHistory((h) => h.map((e) => (e.id === id ? { ...e, ...patch } : e)));
  }, []);

  const runCommand = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!token || session !== "active") return;

      let method = "GET";
      let path = "";
      let body: unknown;
      let type = cmd as string;
      let summary = "";

      if (cmd === "complete") {
        if (!text.trim()) return;
        method = "POST";
        path = "/api/debug/complete";
        body = {
          user_text: text,
          ...(system ? { system } : {}),
          ...(task ? { task } : {}),
          ...(!task && strength ? { strength } : {}),
        };
        summary = `"${text}"`;
      } else if (cmd === "sql") {
        if (!text.trim()) return;
        method = "POST";
        path = "/api/debug/sql";
        body = { sql: text };
        summary = text;
      } else if (cmd === "logs") {
        if (!service.trim()) return;
        path = `/api/debug/logs/${encodeURIComponent(service)}?tail=${encodeURIComponent(tail || "200")}`;
        summary = `tail ${service} · ${tail || "200"}`;
      } else if (cmd === "routing") {
        path = "/api/debug/llm";
        summary = "show live routing table";
      } else if (cmd === "switch") {
        if (!task.trim() || !provider.trim()) return;
        type = "switch";
        method = "PUT";
        path = "/api/debug/llm";
        body = { tasks: { [task]: { provider, ...(effort ? { reasoning_effort: effort } : {}) } } };
        summary = `${task} → ${provider}${effort ? ` ${effort}` : ""}`;
      } else {
        if (!modelId.trim()) return;
        type = modelAction;
        method = "POST";
        path = `/api/debug/llm/local-models/${encodeURIComponent(modelId)}/${modelAction}`;
        summary = `${modelAction} ${modelId}`;
      }

      const id = record({
        type,
        summary,
        detail: `${method} ${path.split("?")[0]}`,
        status: "pending",
        output: "…",
      });
      const res = await call(token.key, method, path, body);
      settle(id, {
        status: res.ok ? "ok" : "err",
        detail: `HTTP ${res.status || "—"}`,
        output: pretty(res.text),
      });
    },
    [
      token,
      session,
      cmd,
      text,
      system,
      task,
      strength,
      service,
      tail,
      provider,
      effort,
      modelId,
      modelAction,
      record,
      settle,
      call,
    ],
  );

  const suspend = useCallback(async () => {
    if (!token) return;
    const res = await call(token.key, "POST", "/api/debug/suspend-self");
    if (res.ok) setSession("suspended");
  }, [token, call]);

  const revoke = useCallback(async () => {
    if (!token) return;
    const res = await call(token.key, "POST", "/api/debug/revoke-self");
    if (res.ok) {
      setSession("revoked");
      try {
        localStorage.removeItem(STORAGE_KEY); // a dead token must not auto-reconnect
      } catch {
        /* ignore */
      }
    }
    setConfirmRevoke(false);
  }, [token, call]);

  const adoptPaste = useCallback(() => {
    const t = decodeToken(paste);
    if (t) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(t));
      } catch {
        /* ignore */
      }
      setToken(t);
      setPaste("");
    } else {
      setConnectError("That doesn't look like a valid token payload.");
    }
  }, [paste]);

  const current = useMemo(
    () => history.find((e) => e.id === selected) ?? null,
    [history, selected],
  );

  if (!connected) {
    return (
      <main className="dbg-gate">
        <h1 className="dbg-wordmark">
          JBrain<span className="dbg-dot">.</span> Debug Console
        </h1>
        {token && !connectError ? (
          <p className="dbg-gate-hint">Connecting to {window.location.host}…</p>
        ) : (
          <p className="dbg-gate-hint">
            Paste the debug token payload you minted in the PWA (Settings → Debug access).
          </p>
        )}
        {connectError && <p className="dbg-error">{connectError}</p>}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            adoptPaste();
          }}
        >
          <input
            className="dbg-paste"
            value={paste}
            onChange={(e) => setPaste(e.currentTarget.value)}
            placeholder="eyJ2IjoxLCJ1Ijoi…"
            aria-label="Debug token payload"
          />
          <button className="dbg-send" type="submit">
            Connect
          </button>
        </form>
        {token && connectError && (
          <button
            type="button"
            className="dbg-btn"
            style={{ marginTop: "8px" }}
            onClick={() => void connect(token)}
          >
            Reconnect saved token
          </button>
        )}
      </main>
    );
  }

  return (
    <div className="dbg">
      <header className="dbg-header">
        <span className="dbg-wordmark">
          JBrain<span className="dbg-dot">.</span>
        </span>
        <span className="dbg-crumb">Debug Console</span>
        <span className="dbg-conn">
          <span className={`dbg-status-dot dbg-${session}`} /> {window.location.host}
        </span>
        {whoami && (
          <span className="dbg-meta">
            <span className="dbg-pill">
              token: <b>{whoami.label}</b>
            </span>
            {whoami.scopes.map((s) => (
              <span key={s} className="dbg-pill dbg-scope">
                {s}
              </span>
            ))}
          </span>
        )}
        <span className="dbg-spacer" />
        {session === "active" && (
          <button className="dbg-btn dbg-suspend" type="button" onClick={() => void suspend()}>
            ⏸ Suspend
          </button>
        )}
        {session !== "revoked" && (
          <button
            className="dbg-btn dbg-revoke"
            type="button"
            onClick={() => (confirmRevoke ? void revoke() : setConfirmRevoke(true))}
            onBlur={() => setConfirmRevoke(false)}
          >
            {confirmRevoke ? "Tap to confirm" : "⛔ Revoke"}
          </button>
        )}
      </header>

      {session !== "active" && (
        <div className={`dbg-banner dbg-banner-${session}`}>
          {session === "suspended"
            ? "Token suspended — it no longer authenticates. Resume it from the PWA (Settings → Debug access) to use this console again."
            : "Token revoked — this session is permanently closed. Mint a new token in the PWA to reconnect."}
        </div>
      )}

      <div className="dbg-body">
        <aside className="dbg-pane">
          <h2 className="dbg-section">Live activity</h2>
          <div className="dbg-history">
            {history.length === 0 && <p className="dbg-empty">No commands yet.</p>}
            {history.map((e) => (
              <button
                key={e.id}
                type="button"
                className={`dbg-cmd${e.id === selected ? " dbg-sel" : ""}`}
                onClick={() => setSelected(e.id)}
              >
                <span className={`dbg-badge dbg-b-${e.type}`}>{e.type}</span>
                <span className="dbg-summary">
                  <span className="dbg-t">{e.summary}</span>
                  <span className="dbg-s">{e.detail}</span>
                </span>
                <span className={`dbg-dotstat dbg-${e.status}`} />
              </button>
            ))}
          </div>
        </aside>

        <main className="dbg-main">
          <form className="dbg-composer" onSubmit={runCommand}>
            <div className="dbg-row">
              <select
                className="dbg-select"
                value={cmd}
                onChange={(e) => setCmd(e.currentTarget.value as CmdType)}
                aria-label="Command"
                disabled={session !== "active"}
              >
                {(Object.keys(CMD_LABELS) as CmdType[]).map((c) => (
                  <option key={c} value={c}>
                    {CMD_LABELS[c]}
                  </option>
                ))}
              </select>
              <button className="dbg-send" type="submit" disabled={session !== "active"}>
                Send ▸
              </button>
            </div>

            {(cmd === "complete" || cmd === "sql") && (
              <textarea
                className="dbg-input dbg-grow"
                rows={cmd === "sql" ? 2 : 3}
                value={text}
                onChange={(e) => setText(e.currentTarget.value)}
                placeholder={cmd === "sql" ? "select code, name from app.domains" : "user prompt…"}
                aria-label={cmd === "sql" ? "SQL" : "Prompt"}
                disabled={session !== "active"}
              />
            )}
            {cmd === "complete" && (
              <div className="dbg-row">
                <input
                  className="dbg-input"
                  value={task}
                  onChange={(e) => setTask(e.currentTarget.value)}
                  placeholder="task (e.g. agent.turn)"
                  aria-label="Task"
                />
                <input
                  className="dbg-input"
                  value={strength}
                  onChange={(e) => setStrength(e.currentTarget.value)}
                  placeholder="strength (low/med/high)"
                  aria-label="Strength"
                />
                <input
                  className="dbg-input dbg-grow"
                  value={system}
                  onChange={(e) => setSystem(e.currentTarget.value)}
                  placeholder="system (optional)"
                  aria-label="System"
                />
              </div>
            )}
            {cmd === "logs" && (
              <div className="dbg-row">
                <input
                  className="dbg-input dbg-grow"
                  value={service}
                  onChange={(e) => setService(e.currentTarget.value)}
                  placeholder="service (api|worker|db|…)"
                  aria-label="Service"
                />
                <input
                  className="dbg-input"
                  value={tail}
                  onChange={(e) => setTail(e.currentTarget.value)}
                  placeholder="tail"
                  aria-label="Tail"
                />
              </div>
            )}
            {cmd === "switch" && (
              <div className="dbg-row">
                <input
                  className="dbg-input"
                  value={task}
                  onChange={(e) => setTask(e.currentTarget.value)}
                  placeholder="task"
                  aria-label="Task"
                />
                <input
                  className="dbg-input dbg-grow"
                  value={provider}
                  onChange={(e) => setProvider(e.currentTarget.value)}
                  placeholder="provider:spec"
                  aria-label="Provider"
                />
                <input
                  className="dbg-input"
                  value={effort}
                  onChange={(e) => setEffort(e.currentTarget.value)}
                  placeholder="effort (optional)"
                  aria-label="Effort"
                />
              </div>
            )}
            {cmd === "model" && (
              <div className="dbg-row">
                <select
                  className="dbg-select"
                  value={modelAction}
                  onChange={(e) => setModelAction(e.currentTarget.value as "load" | "unload")}
                  aria-label="Model action"
                >
                  <option value="load">load</option>
                  <option value="unload">unload</option>
                </select>
                <input
                  className="dbg-input dbg-grow"
                  value={modelId}
                  onChange={(e) => setModelId(e.currentTarget.value)}
                  placeholder="model id (e.g. gpt-oss-120b)"
                  aria-label="Model id"
                />
              </div>
            )}
          </form>

          <section className="dbg-output">
            {current ? (
              <>
                <div className="dbg-ohead">
                  <span className="dbg-otitle">
                    {current.type} · {current.summary}
                  </span>
                  <span className={`dbg-kv dbg-${current.status}`}>{current.detail}</span>
                </div>
                <pre className="dbg-pre">{current.output}</pre>
              </>
            ) : (
              <p className="dbg-empty">Run a command, or watch activity stream in on the left.</p>
            )}
          </section>
        </main>
      </div>
    </div>
  );
}
