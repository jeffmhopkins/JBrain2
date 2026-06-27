// The external-LLM session screen: a token-gated public endpoint that exposes the
// on-box coder to a remote Claude. Shows the endpoint URL + how to wire it, the secret
// (only right after minting — never recoverable), live token-usage stats, an on/off
// toggle, and delete. Opened from the jcode launcher (a new "External" session type).

import { useState } from "react";
import { api } from "../api/client";
import { ChevronLeftIcon } from "../components/icons";
import type { ExternalSession } from "../jcode/types";

function fmt(n: number): string {
  return n.toLocaleString();
}

export function ExternalSessionScreen({
  session,
  secret = null,
  url,
  onClose,
  onChanged,
}: {
  session: ExternalSession;
  // The bearer secret, present ONLY immediately after minting (shown once).
  secret?: string | null;
  // The endpoint base URL the remote points ANTHROPIC_BASE_URL at.
  url: string;
  onClose: () => void;
  // Called after a toggle/delete so the launcher's list refreshes.
  onChanged: () => void;
}) {
  const [enabled, setEnabled] = useState(session.enabled);
  const [busy, setBusy] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [copied, setCopied] = useState<"url" | "secret" | null>(null);

  async function toggle() {
    const next = !enabled;
    setEnabled(next); // optimistic
    setBusy(true);
    try {
      await api.externalSetEnabled(session.id, next);
      onChanged();
    } catch {
      setEnabled(!next); // revert on failure
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await api.externalRevoke(session.id);
      onChanged();
      onClose();
    } finally {
      setBusy(false);
    }
  }

  function copy(what: "url" | "secret", value: string) {
    void navigator.clipboard?.writeText(value);
    setCopied(what);
  }

  return (
    <section className="jcode-screen">
      <header className="jcode-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
        </button>
        <span className="jcode-sesshead">
          <span className={`jcode-sd${enabled ? " live" : ""}`} />
          <span className="jcode-repo">{session.label || "external session"}</span>
          <span className="jcode-branch">external LLM</span>
        </span>
      </header>

      <div className="jcode-body jcode-extbody">
        {/* On/off — the kill switch. */}
        <div className="jcode-extrow">
          <div>
            <span className="jcode-extlabel">Endpoint {enabled ? "enabled" : "off"}</span>
            <p className="jcode-empty">
              {enabled
                ? "Accepting requests while the coder is loaded."
                : "Off — the remote gets refused until you switch it on."}
            </p>
          </div>
          <button
            type="button"
            className={`jcode-toggle${enabled ? " on" : ""}`}
            role="switch"
            aria-checked={enabled}
            aria-label="Enabled"
            disabled={busy}
            onClick={toggle}
          >
            <span className="jcode-toggle-knob" />
          </button>
        </div>

        {/* The secret — shown once, right after minting. */}
        {secret && (
          <div className="jcode-extcard jcode-extsecret">
            <span className="jcode-extlabel">Access token</span>
            <p className="jcode-empty">Copy it now — it's shown once and can't be recovered.</p>
            <input
              className="jcode-share-input"
              readOnly
              value={secret}
              aria-label="Access token"
              onFocus={(e) => e.currentTarget.select()}
            />
            <button type="button" className="jcode-act" onClick={() => copy("secret", secret)}>
              {copied === "secret" ? "Copied ✓" : "Copy token"}
            </button>
          </div>
        )}

        {/* The endpoint + how to wire a remote Claude to it. */}
        <div className="jcode-extcard">
          <span className="jcode-extlabel">Endpoint</span>
          <input
            className="jcode-share-input"
            readOnly
            value={url}
            aria-label="Endpoint URL"
            onFocus={(e) => e.currentTarget.select()}
          />
          <button type="button" className="jcode-act" onClick={() => copy("url", url)}>
            {copied === "url" ? "Copied ✓" : "Copy URL"}
          </button>
          <p className="jcode-empty">
            Point a remote Claude at it: <code>ANTHROPIC_BASE_URL={url}</code> and{" "}
            <code>ANTHROPIC_AUTH_TOKEN=&lt;token&gt;</code>. Requests run on your loaded coder.
          </p>
        </div>

        {/* Cumulative usage. */}
        <div className="jcode-extcard">
          <span className="jcode-extlabel">Token usage</span>
          <div className="jcode-extstats">
            <div className="jcode-extstat">
              <span className="jcode-extnum">{fmt(session.in_tokens)}</span>
              <span className="jcode-extcap">input</span>
            </div>
            <div className="jcode-extstat">
              <span className="jcode-extnum">{fmt(session.out_tokens)}</span>
              <span className="jcode-extcap">output</span>
            </div>
            <div className="jcode-extstat">
              <span className="jcode-extnum">{fmt(session.requests)}</span>
              <span className="jcode-extcap">requests</span>
            </div>
          </div>
          <p className="jcode-empty">
            {session.last_used_at
              ? `Last used ${new Date(session.last_used_at).toLocaleString()}`
              : "Not used yet."}
            {session.expires_at
              ? ` · expires ${new Date(session.expires_at).toLocaleString()}`
              : " · no expiry"}
          </p>
        </div>

        <button
          type="button"
          className="jcode-act danger jcode-extdelete"
          disabled={busy}
          onClick={() => (confirmDel ? remove() : setConfirmDel(true))}
          onBlur={() => setConfirmDel(false)}
        >
          {confirmDel ? "Tap again — deletes this endpoint" : "Delete endpoint"}
        </button>
      </div>
    </section>
  );
}
