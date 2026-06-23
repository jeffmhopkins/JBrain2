// The owner debug console — a standalone, token-authenticated page (not part of
// the cookie-authed PWA). It drives the /api/debug/* surface: prompt completion,
// read-only SQL, logs, and live LLM routing, with the token's own kill switch
// (Suspend / Revoke) top-right. The bearer key lives only in this tab's memory
// (from the URL fragment or a paste) and is never persisted.

import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { type DebugToken, decodeToken } from "./payload";

type CmdType = "complete" | "sql" | "logs" | "routing" | "switch" | "model";
type SessionState = "active" | "suspended" | "revoked";

interface HistoryEntry {
  id: number;
  type: CmdType | "whoami";
  summary: string;
  detail: string;
  status: "ok" | "err" | "pending";
  output: string;
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

async function call(
  token: DebugToken,
  method: string,
  path: string,
  body?: unknown,
): Promise<CallResult> {
  const headers: Record<string, string> = { Authorization: `Bearer ${token.key}` };
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(`${token.base}${path}`, init);
    return { ok: res.ok, status: res.status, text: await res.text() };
  } catch (e) {
    return { ok: false, status: 0, text: `network error: ${String(e)}` };
  }
}

function pretty(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

const CMD_LABELS: Record<CmdType, string> = {
  complete: "complete — run a prompt",
  sql: "sql — read-only query",
  logs: "logs — tail a service",
  routing: "routing — show live table",
  switch: "switch — route a task",
  model: "model — load / unload",
};

export function Console() {
  const [token, setToken] = useState<DebugToken | null>(() =>
    decodeToken(window.location.hash.slice(1)),
  );
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

  // Drop the secret from the address bar once read, so a copied URL never leaks it.
  useEffect(() => {
    if (token && window.location.hash) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, [token]);

  const connect = useCallback(async (t: DebugToken) => {
    const res = await call(t, "GET", "/api/debug/whoami");
    if (res.ok) {
      const body = JSON.parse(res.text) as Whoami;
      setWhoami(body);
      setSession("active");
      setConnectError(null);
    } else {
      setConnectError(
        res.status === 401
          ? "This token is invalid, expired, suspended, or revoked."
          : `Could not reach the box (HTTP ${res.status || "—"}).`,
      );
    }
  }, []);

  useEffect(() => {
    if (token) void connect(token);
  }, [token, connect]);

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
      let type: HistoryEntry["type"] = cmd;
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
        type = "model";
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
      const res = await call(token, method, path, body);
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
    ],
  );

  const suspend = useCallback(async () => {
    if (!token) return;
    const res = await call(token, "POST", "/api/debug/suspend-self");
    if (res.ok) setSession("suspended");
  }, [token]);

  const revoke = useCallback(async () => {
    if (!token) return;
    const res = await call(token, "POST", "/api/debug/revoke-self");
    if (res.ok) setSession("revoked");
    setConfirmRevoke(false);
  }, [token]);

  const current = useMemo(
    () => history.find((e) => e.id === selected) ?? null,
    [history, selected],
  );

  if (!token) {
    return (
      <main className="dbg-gate">
        <h1 className="dbg-wordmark">
          JBrain<span className="dbg-dot">.</span> Debug Console
        </h1>
        <p className="dbg-gate-hint">
          Paste the debug token payload you minted in the PWA (Settings → Debug access).
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const t = decodeToken(paste);
            if (t) setToken(t);
            else setConnectError("That doesn't look like a valid token payload.");
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
        {connectError && <p className="dbg-error">{connectError}</p>}
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
          <span className={`dbg-status-dot dbg-${session}`} />{" "}
          {token.base.replace(/^https?:\/\//, "")}
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
          <h2 className="dbg-section">Commands sent</h2>
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
              <p className="dbg-empty">Run a command to see its output here.</p>
            )}
          </section>
        </main>
      </div>
    </div>
  );
}
