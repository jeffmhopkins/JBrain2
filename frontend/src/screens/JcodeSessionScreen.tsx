// Code mode (jcode) — the terminal-first session (docs/reference/DESIGN.md "jcode", 2-tab
// Variant A, docs/mocks/jcode-session-2tab-a-fullbleed.html). One session, two views:
// Terminal (a real shell in the sandbox over xterm.js — the way you drive the coder) and
// Preview (the sandbox dev server at the session's own host address). A slim header carries the
// session + model chip; owner actions (Reset / Share / Stop / Delete) live in a ⋯ menu so
// the terminal gets the whole screen. Exiting the shell pauses the session; Restart (here
// or from the launcher) resumes it.

import "@xterm/xterm/css/xterm.css";
import { type MouseEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import {
  ChevronLeftIcon,
  GlobeIcon,
  MoreIcon,
  RefreshIcon,
  TerminalIcon,
} from "../components/icons";
import { shareUrl } from "../jcode/share";
import {
  KEY_SEQ,
  type Modifier,
  type TerminalHandle,
  attachTerminal,
  terminalWsUrl,
} from "../jcode/terminal";
import type { JcodeModelStatus, JcodePreview, JcodeSession, JcodeShare } from "../jcode/types";

// Rough cold-load read rate (s/GB) for the loading-bar's FALLBACK time estimate, used only
// when the gateway reports no real load fraction. Either way the bar caps short of 100% and
// completes only when the gateway confirms the model resident.
const LOAD_SEC_PER_GB = 1.2;

type Tab = "term" | "prev";

// The bottom helper row for the interactive terminal: keys a soft keyboard can't send on
// its own (Esc, Tab, arrows) plus sticky Ctrl/Alt that fold the next typed character.
// Shown only on touch devices (a physical keyboard sends these directly) via CSS. The keys
// use onMouseDown→preventDefault so tapping never blurs the terminal — the soft keyboard
// stays up and the modifier applies to the very next keystroke.
function JcodeKeys({
  mod,
  onKey,
  onMod,
}: {
  mod: Modifier | null;
  onKey: (seq: string) => void;
  onMod: (mod: Modifier) => void;
}) {
  const keep = (e: MouseEvent) => e.preventDefault();
  return (
    <div className="jcode-keys" role="toolbar" aria-label="Terminal keys">
      <button
        type="button"
        className="jcode-key"
        onMouseDown={keep}
        onClick={() => onKey(KEY_SEQ.esc)}
      >
        esc
      </button>
      <button
        type="button"
        className="jcode-key"
        onMouseDown={keep}
        onClick={() => onKey(KEY_SEQ.tab)}
      >
        tab
      </button>
      <button
        type="button"
        className={`jcode-key${mod === "ctrl" ? " on" : ""}`}
        aria-pressed={mod === "ctrl"}
        onMouseDown={keep}
        onClick={() => onMod("ctrl")}
      >
        ctrl
      </button>
      <button
        type="button"
        className={`jcode-key${mod === "alt" ? " on" : ""}`}
        aria-pressed={mod === "alt"}
        onMouseDown={keep}
        onClick={() => onMod("alt")}
      >
        alt
      </button>
      <button
        type="button"
        className="jcode-key"
        aria-label="Left"
        onMouseDown={keep}
        onClick={() => onKey(KEY_SEQ.left)}
      >
        ←
      </button>
      <button
        type="button"
        className="jcode-key"
        aria-label="Up"
        onMouseDown={keep}
        onClick={() => onKey(KEY_SEQ.up)}
      >
        ↑
      </button>
      <button
        type="button"
        className="jcode-key"
        aria-label="Down"
        onMouseDown={keep}
        onClick={() => onKey(KEY_SEQ.down)}
      >
        ↓
      </button>
      <button
        type="button"
        className="jcode-key"
        aria-label="Right"
        onMouseDown={keep}
        onClick={() => onKey(KEY_SEQ.right)}
      >
        →
      </button>
    </div>
  );
}

// The interactive terminal: a real shell in the sandbox via xterm.js over the terminal WS.
// xterm is dynamically imported so it (and its CSS) only load when used, and so tests can
// mock it without the canvas renderer touching jsdom. The WS pump + resize wiring lives in
// jcode/terminal.ts; this owns the xterm lifecycle and the mobile key row. It stays mounted
// across tab switches (the stage hides it with CSS, doesn't unmount it) so flipping to
// Preview and back keeps the SAME shell — `visible` lets it re-fit when shown again. `onClosed`
// fires when the socket closes while still mounted (a shell exit / server pause) — NOT on our
// own unmount (stop / restart / leaving the screen), which sets `disposed` first.
function JcodeTerminal({
  sid,
  visible,
  onClosed,
}: { sid: string; visible: boolean; onClosed?: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const handle = useRef<TerminalHandle | null>(null);
  const fitRef = useRef<{ fit: () => void } | null>(null);
  const termRef = useRef<{ focus: () => void } | null>(null);
  const onClosedRef = useRef(onClosed);
  onClosedRef.current = onClosed;
  // The armed soft-keyboard modifier, mirrored into state so the key row can highlight it
  // (the handle owns the source of truth and reports changes — including auto-clear after a
  // key is folded — back through onModifierChange).
  const [mod, setMod] = useState<Modifier | null>(null);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let disposed = false;
    let cleanup = () => {};
    void (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed || !host.current) return;
      const term = new Terminal({
        fontSize: 8,
        fontFamily: "ui-monospace, Menlo, Consolas, monospace",
        cursorBlink: true,
        theme: { background: "#0b0b0c", foreground: "#e6e6e6" },
      });
      const fit = new FitAddon();
      fitRef.current = fit;
      termRef.current = term;
      term.loadAddon(fit);
      term.open(el);
      fit.fit();
      const ws = new WebSocket(terminalWsUrl(sid));
      const h = attachTerminal(term, ws, setMod);
      handle.current = h;
      ws.onclose = () => {
        if (!disposed) {
          term.write("\r\n\x1b[2m— session ended —\x1b[0m\r\n");
          onClosedRef.current?.();
        }
      };
      // Refit on the panel's ACTUAL size, not just window resizes. A share-link recipient
      // on desktop gets a full-width screen (.jcode-screen--wide), but the window never
      // resizes after load — so a mount-time fit that measured before the wide layout
      // settled would leave the terminal frozen at a narrow mobile column, the rest of the
      // panel bare background. Observing the host refits the moment its real width lands
      // (the observer fires on first observe) and on every later change, filling the panel.
      const ro = new ResizeObserver(() => fit.fit());
      ro.observe(el);
      term.focus();
      cleanup = () => {
        ro.disconnect();
        h.detach();
        handle.current = null;
        fitRef.current = null;
        termRef.current = null;
        ws.close();
        term.dispose();
      };
    })();
    return () => {
      disposed = true;
      cleanup();
    };
  }, [sid]);

  // Re-fit + refocus when the terminal becomes visible again (switching back from Preview).
  // While hidden the host is display:none, so a window resize in the meantime couldn't lay
  // the terminal out; refit on show so the PTY's winsize matches the panel again.
  useEffect(() => {
    if (visible) {
      fitRef.current?.fit();
      termRef.current?.focus();
    }
  }, [visible]);

  // Tapping a modifier toggles it (tap again to disarm); a control key sends straight through.
  const toggleMod = (m: Modifier) => handle.current?.setModifier(mod === m ? null : m);
  const sendKey = (seq: string) => handle.current?.sendKey(seq);

  return (
    <>
      <div className="jcode-cli" ref={host} />
      <JcodeKeys mod={mod} onKey={sendKey} onMod={toggleMod} />
    </>
  );
}

const SHARE_TTL_OPTIONS: { hours: number; label: string }[] = [
  { hours: 1, label: "1h" },
  { hours: 24, label: "24h" },
  { hours: 24 * 7, label: "7d" },
  { hours: 24 * 30, label: "30d" },
];

// The owner's share-link manager (a modal over the session), modelled on the debug-token
// minting UX: create a single-use, time-boxed link, copy the secret once, and see / revoke
// the live links. "Single-use" = the link binds to the FIRST browser that opens it; the
// list flags each as opened or still unused so a stale link is easy to spot and revoke.
function JcodeShareManager({ sid, onClose }: { sid: string; onClose: () => void }) {
  const [shares, setShares] = useState<JcodeShare[] | null>(null);
  const [label, setLabel] = useState("");
  const [ttl, setTtl] = useState(24);
  const [minted, setMinted] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  const reload = () => {
    api
      .jcodeListShares(sid)
      .then(setShares)
      .catch(() => setShares([]));
  };
  // biome-ignore lint/correctness/useExhaustiveDependencies: reload reads sid only
  useEffect(reload, [sid]);

  async function mint() {
    setBusy(true);
    setError(null);
    try {
      const t = await api.jcodeMintShare(sid, ttl);
      setMinted(shareUrl(sid, t.token));
      setCopied(false);
      setLabel("");
      reload();
    } catch {
      setError("Couldn't create a link.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    try {
      await api.jcodeRevokeShare(sid, id);
    } finally {
      reload();
    }
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: a lightweight overlay panel, not a native <dialog>
    <div className="jcode-modal" role="dialog" aria-modal="true" aria-label="Share links">
      <div className="jcode-modal-card">
        <div className="jcode-modal-head">
          <span>Share this session</span>
          <button type="button" className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <p className="jcode-empty">
          A link opens this one session on any browser — no owner key needed. It's single-use: the
          first person to open it binds it to their browser, and the link is dead for anyone else.
          Time-boxed and revocable.
        </p>
        <div className="jcode-share-mint">
          <input
            className="jcode-share-input"
            value={label}
            placeholder="Label (e.g. Sarah's laptop)"
            aria-label="Link label"
            onChange={(e) => setLabel(e.currentTarget.value)}
          />
          <div className="jcode-share-ttl" aria-label="Link lifetime">
            {SHARE_TTL_OPTIONS.map((o) => (
              <button
                key={o.hours}
                type="button"
                aria-pressed={ttl === o.hours}
                className={`jcode-act${ttl === o.hours ? " armed" : ""}`}
                onClick={() => setTtl(o.hours)}
              >
                {o.label}
              </button>
            ))}
          </div>
          <button type="button" className="jcode-act teal" disabled={busy} onClick={mint}>
            {busy ? "Creating…" : "Create link"}
          </button>
        </div>
        {error && <p className="jcode-empty jcode-share-error">{error}</p>}
        {minted && (
          <div className="jcode-share-minted">
            <p className="jcode-empty">Copy it now — the secret is shown once.</p>
            <input
              className="jcode-share-input"
              readOnly
              value={minted}
              aria-label="Share link"
              onFocus={(e) => e.currentTarget.select()}
            />
            <div className="jcode-actions">
              <button
                type="button"
                className="jcode-act"
                onClick={() => {
                  void navigator.clipboard?.writeText(minted);
                  setCopied(true);
                }}
              >
                {copied ? "Copied ✓" : "Copy link"}
              </button>
              <button type="button" className="jcode-act" onClick={() => setMinted(null)}>
                Done
              </button>
            </div>
          </div>
        )}
        {shares && shares.length > 0 && (
          <ul className="jcode-share-list" aria-label="Active links">
            {shares.map((s) => (
              <li key={s.id} className="jcode-share-row">
                <div>
                  <span className="jcode-share-name">{s.label}</span>
                  <span className={`jcode-share-state${s.redeemed_at ? " used" : ""}`}>
                    {s.redeemed_at ? "opened" : "unused"}
                  </span>
                  <p className="jcode-empty">
                    {s.expires_at
                      ? `expires ${new Date(s.expires_at).toLocaleString()}`
                      : "no expiry"}
                  </p>
                </div>
                <button
                  type="button"
                  className="jcode-act danger"
                  onClick={() => (revoking === s.id ? revoke(s.id) : setRevoking(s.id))}
                  onBlur={() => setRevoking(null)}
                >
                  {revoking === s.id ? "Tap to confirm" : "Revoke"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

const TABS: { id: Tab; label: string; icon: typeof TerminalIcon }[] = [
  { id: "term", label: "Terminal", icon: TerminalIcon },
  { id: "prev", label: "Preview", icon: GlobeIcon },
];

// Append a changing cache-buster so a Refresh re-fetches the dev page instead of the
// browser's cached copy. The dev server ignores the unknown param; we only add it once a
// refresh has happened, so the first load keeps the clean address.
function withReloadNonce(url: string, nonce: number): string {
  return `${url}${url.includes("?") ? "&" : "?"}_r=${nonce}`;
}

export function JcodeSessionScreen({
  session,
  onClose,
  shared = false,
}: {
  session: JcodeSession;
  onClose: () => void;
  // True when reached via a redeemed share link (not the owner's launcher): owner-only
  // controls (Reset / Stop / Delete / Share, the model poll) are hidden — those routes 403
  // a share principal anyway. Share recipients still get both tabs (terminal + preview).
  shared?: boolean;
}) {
  const [tab, setTab] = useState<Tab>("term");
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirm, setConfirm] = useState<"reset" | "delete" | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  // Paused (terminal exited / explicit Stop). Seeded from the launcher's status so opening a
  // stopped session lands on the Restart prompt rather than a dead terminal.
  const [stopped, setStopped] = useState(session.status === "stopped");
  const [restarting, setRestarting] = useState(false);
  // Bumped on restart to force a fresh terminal mount (a new shell + socket).
  const [mountNonce, setMountNonce] = useState(0);
  const [preview, setPreview] = useState<JcodePreview | null>(null);
  const [pvBusy, setPvBusy] = useState(false);
  // Bumped by the Refresh control to force-reload the preview iframe past the browser
  // cache: a dev server with no HMR (e.g. a plain static server) never tells the iframe
  // to reload, so the nonce changes the src's query AND remounts the iframe.
  const [pvNonce, setPvNonce] = useState(0);
  const [model, setModel] = useState<JcodeModelStatus | null>(null);
  // Set when the owner confirms the swap (or a warm is already in flight): it re-arms the
  // poll to track the load and flips the load prompt over to the progress bar.
  const [warmRequested, setWarmRequested] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const loadStart = useRef(Date.now());
  // Highest percent shown so far this load — the bar must never slide backwards (the real
  // log fraction can briefly lag the time estimate, or a poll can report a lower number).
  const shownPct = useRef(0);

  // Poll the coder's warm state so the loading bar tracks the real load while it comes onto
  // the box. We key the bar off `warming` — the backend's warm-task signal — NOT `loaded`:
  // the gateway lists a model resident the moment a load is *requested*, so `loaded` races
  // true before the weights finish. Keep polling until settled. /jcode/model is owner-only,
  // so a share recipient skips the poll entirely (the chip's default label covers it).
  useEffect(() => {
    if (shared) return;
    let stale = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const s = await api.jcodeModelStatus();
        if (stale) return;
        setModel(s);
        const idle = !s.loaded && !s.warming && !warmRequested;
        if (!s.hosting || (s.loaded && !s.warming) || idle) return;
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
  }, [shared, warmRequested]);

  // The load prompt: hosting on, the coder not on the box, and no warm yet — ask before
  // evicting whatever's resident. Once the owner confirms (warmRequested) the bar takes over.
  const needsLoad =
    !shared && model?.hosting === true && !model.loaded && !model.warming && !warmRequested;
  const loading =
    model?.hosting === true && (model.warming === true || (warmRequested && !model.loaded));

  async function warmModel() {
    setWarmRequested(true);
    setModel((m) => (m ? { ...m, warming: true } : m));
    try {
      setModel(await api.jcodeWarmModel());
    } catch {
      setWarmRequested(false);
    }
  }
  // Tick the estimate while loading so the bar advances between polls, anchored to when
  // warming actually began (not screen mount).
  useEffect(() => {
    if (!loading) return;
    loadStart.current = Date.now();
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [loading]);
  // Prefer the gateway's real load fraction (weights actually read in) when it reports one;
  // fall back to the time estimate while it doesn't. The real signal is capped at 99 and the
  // estimate at 96 — neither completes the bar on its own. Completion is the load going
  // resident (`loading` → false), which drops the overlay; the bar never has to reach 100.
  const sizeGb = model?.size_gb ?? 0;
  const elapsedSec = (now - loadStart.current) / 1000;
  const estPct =
    sizeGb > 0 ? Math.min(96, Math.round((elapsedSec / (sizeGb * LOAD_SEC_PER_GB)) * 100)) : 0;
  const realPct =
    model?.progress != null ? Math.min(99, Math.max(0, Math.round(model.progress * 100))) : null;
  if (!loading) shownPct.current = 0;
  else shownPct.current = Math.max(shownPct.current, realPct ?? estPct);
  const loadPct = shownPct.current;
  // A friendly served-context label for the model chip ("256k" for the coder's full window).
  const ctxLabel = model?.context_window ? `${Math.round(model.context_window / 1024)}k` : null;

  // Fetch the preview status the first time the Preview tab is opened (the feature flag +
  // any already-open preview). Failures leave it null → a neutral empty state.
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
      // Keep the prior port (so the empty state shows the right copy) but clear the url.
      setPreview((p) => ({ ...(p ?? { enabled: true }), enabled: true, url: null }));
    } finally {
      setPvBusy(false);
    }
  }

  async function stopSession() {
    setMenuOpen(false);
    setStopped(true); // unmounts the terminal (cleanly closing its socket) before/while we ask the server
    try {
      await api.jcodeStopSession(session.id);
    } catch {
      // Best-effort: the terminal exit already pauses it server-side in the usual case.
    }
  }

  async function restartSession() {
    setRestarting(true);
    try {
      await api.jcodeRestartSession(session.id);
      setStopped(false);
      setMountNonce((n) => n + 1); // force a fresh terminal mount
      setTab("term");
    } catch {
      // leave the stopped prompt up so the owner can retry
    } finally {
      setRestarting(false);
    }
  }

  async function doConfirm() {
    if (confirm === "reset") {
      await api.jcodeResetSession(session.id);
    } else if (confirm === "delete") {
      await api.jcodeDeleteSession(session.id);
      onClose();
      return;
    }
    setConfirm(null);
    setMenuOpen(false);
  }

  // The terminal mounts when the session is live and the coder is ready — otherwise the stage
  // shows the load prompt / loading bar / stopped state in its place. It's independent of the
  // active tab: once live the terminal stays mounted across a switch to Preview and back (the
  // stage hides it with CSS, never unmounts it) so the SAME shell — and everything running in
  // it — survives the round trip. Unmounting would drop the socket, and the control server
  // kills the PTY when the socket drops, so reconnecting would hand back a fresh bash.
  const terminalLive = !stopped && !needsLoad && !loading;

  return (
    <section className={`jcode-screen${shared ? " jcode-screen--wide" : ""}`}>
      <header className="jcode-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to sessions">
          <ChevronLeftIcon size={22} />
        </button>
        <span className="jcode-sesshead">
          <span className={`jcode-sd${stopped ? "" : " live"}`} />
          <span className="jcode-repo">{session.repo || "scratch"}</span>
          <span className="jcode-branch">@ {session.work_branch || session.branch}</span>
        </span>
        <span className="jcode-modelchip">
          {model?.model ?? "qwen3-coder-next"}
          {ctxLabel ? ` · ${ctxLabel}` : ""} · on-box
        </span>
        <div className="jcode-tabsinline" role="tablist" aria-label="Session views">
          {TABS.map((t) => {
            const Glyph = t.icon;
            return (
              <button
                key={t.id}
                type="button"
                role="tab"
                // Icon-only in the bar — the label is the accessible name + tooltip.
                aria-selected={tab === t.id}
                aria-label={t.label}
                title={t.label}
                className={`jcode-tabin ${t.id}${tab === t.id ? " on" : ""}`}
                onClick={() => setTab(t.id)}
              >
                <Glyph size={18} />
              </button>
            );
          })}
        </div>
        {!shared && (
          <span className="jcode-menuwrap">
            <button
              type="button"
              className="icon-btn"
              aria-label="Session actions"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => {
                // Closing the menu disarms any tap-again confirm, so a stale "Tap
                // again — deletes" can't carry over to the next time it's opened.
                setConfirm(null);
                setMenuOpen((o) => !o);
              }}
            >
              <MoreIcon size={20} />
            </button>
            {menuOpen && (
              <div className="jcode-menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  className="jcode-menu-item"
                  onClick={() => (confirm === "reset" ? doConfirm() : setConfirm("reset"))}
                >
                  {confirm === "reset" ? "Tap again — wipes changes" : "Reset sandbox"}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="jcode-menu-item"
                  onClick={() => {
                    setMenuOpen(false);
                    setShareOpen(true);
                  }}
                >
                  Share link…
                </button>
                {preview?.url && (
                  <>
                    <div className="jcode-menu-sep" />
                    <button
                      type="button"
                      role="menuitem"
                      className="jcode-menu-item"
                      onClick={() => {
                        void navigator.clipboard?.writeText(preview.url ?? "");
                        setMenuOpen(false);
                      }}
                    >
                      Copy preview address
                    </button>
                  </>
                )}
                <div className="jcode-menu-sep" />
                {!stopped && (
                  <button
                    type="button"
                    role="menuitem"
                    className="jcode-menu-item"
                    onClick={stopSession}
                  >
                    Stop session
                  </button>
                )}
                <button
                  type="button"
                  role="menuitem"
                  className="jcode-menu-item danger"
                  onClick={() => (confirm === "delete" ? doConfirm() : setConfirm("delete"))}
                >
                  {confirm === "delete" ? "Tap again — deletes session" : "Delete"}
                </button>
              </div>
            )}
          </span>
        )}
      </header>

      <div className="jcode-stage">
        {/* The terminal panel stays in the tree across tab switches, hidden (not unmounted)
            while Preview is active, so its shell socket — and the live shell — persist. */}
        <div className="jcode-clipanel" style={{ display: tab === "term" ? undefined : "none" }}>
          {terminalLive ? (
            <JcodeTerminal
              key={`${session.id}:${mountNonce}`}
              sid={session.id}
              visible={tab === "term"}
              onClosed={() => setStopped(true)}
            />
          ) : tab === "term" ? (
            stopped ? (
              <div className="jcode-overlay" aria-label="Session stopped">
                <RefreshIcon size={40} />
                <h3>Session stopped</h3>
                <p>
                  Processes were halted and the checkout paused — your files are preserved.
                  {shared
                    ? " Ask the owner to restart it."
                    : " Restart to pick up where you left off (also from the session manager)."}
                </p>
                {!shared && (
                  <button
                    type="button"
                    className="jcode-act teal"
                    disabled={restarting}
                    onClick={restartSession}
                  >
                    {restarting ? "Restarting…" : "Restart session"}
                  </button>
                )}
              </div>
            ) : needsLoad && model ? (
              <div className="jcode-overlay" aria-label="Load model">
                <div className="jcode-modelask">
                  <div className="jcode-modelask-head">Load {model.model} onto the box?</div>
                  <p className="jcode-modelask-body">
                    The terminal's <code>claude</code> runs the coder on-box (~
                    {Math.round(model.size_gb)} GB, about a minute, served at its full{" "}
                    {ctxLabel ?? "native"} context). {(() => {
                      const evicts = model.resident.filter((r) => r !== model.served);
                      return evicts.length > 0
                        ? `Loading it will unload ${evicts.join(", ")}.`
                        : "Nothing else is loaded right now.";
                    })()}
                  </p>
                  <button type="button" className="jcode-act teal" onClick={warmModel}>
                    Load model
                  </button>
                </div>
              </div>
            ) : loading && model ? (
              <div className="jcode-overlay" aria-label="Loading model">
                <div className="jcode-modelload">
                  <div className="jcode-modelload-row">
                    <span>Loading {model.model} onto the box…</span>
                    <span className="jcode-modelload-pct">{loadPct}%</span>
                  </div>
                  <div className="jcode-modelload-track">
                    <div className="jcode-modelload-fill" style={{ width: `${loadPct}%` }} />
                  </div>
                </div>
              </div>
            ) : null
          ) : null}
        </div>

        {tab === "prev" &&
          (preview?.url ? (
            // The live preview rendered inline as an iframe — the Preview tab *is* the dev
            // page. Copy-the-address lives in the ⋯ menu so the page gets the full panel;
            // the iframe itself shows the proxy's "start your dev server" 502 until it's up.
            <div className="jcode-pvframe">
              <iframe
                // key + the cache-busting query both force a full reload on refresh; a
                // dev server with no HMR won't reload the iframe on its own.
                key={pvNonce}
                className="jcode-pviframe"
                title="Dev server preview"
                src={pvNonce === 0 ? preview.url : withReloadNonce(preview.url, pvNonce)}
              />
              <button
                type="button"
                className="jcode-pvrefresh"
                title="Reload preview"
                aria-label="Reload preview"
                onClick={() => setPvNonce((n) => n + 1)}
              >
                <RefreshIcon size={18} />
              </button>
            </div>
          ) : (
            <div className="jcode-panel">
              {preview === null ? (
                <p className="jcode-empty">Loading…</p>
              ) : !preview.enabled ? (
                <p className="jcode-empty">
                  Web preview is turned off on this server. It's on by default with code mode —
                  restore it by removing <code>JCODE_PREVIEW_ENABLED=false</code> from
                  <code> .env</code> and re-running <code>jcode-setup.sh</code>.
                </p>
              ) : (
                <div className="jcode-preview">
                  <p className="jcode-empty">
                    This session has its own preview address. Open it, then run your dev server
                    {preview.port ? (
                      <>
                        {" "}
                        on <code>:{preview.port}</code> (it's <code>$PORT</code> in the shell)
                      </>
                    ) : null}{" "}
                    — it appears here once it's up.
                  </p>
                  <button
                    type="button"
                    className="jcode-act teal"
                    disabled={pvBusy}
                    onClick={openPreview}
                  >
                    {pvBusy ? "Opening…" : "Open preview"}
                  </button>
                </div>
              )}
            </div>
          ))}
      </div>

      {shareOpen && !shared && (
        <JcodeShareManager sid={session.id} onClose={() => setShareOpen(false)} />
      )}
    </section>
  );
}
