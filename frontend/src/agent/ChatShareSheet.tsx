// The chat share sheet (docs/mocks/chat-share, variant B): mint a revocable, no-login link to
// one chat, copy or send it (shown once — only its hash is stored), see how often it was
// opened, and revoke it. Anyone with the link reads the whole chat, tool steps included, as it
// stands each time they open it. A chat that can read or has read a firewalled domain, mail or
// memory warns first and takes a second tap to publish, because its turns can quote them. The
// server decides from the stored turns (a re-scoped chat keeps what it read) and refuses an
// unconfirmed mint, so the warning can't be skipped by a stale answer here.

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { ApiError, type ChatShareLink, type MintedChatShare, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import { CopyIcon, EyeIcon, LinkIcon, ShieldIcon } from "../components/icons";
import { chatShareUrl } from "./chatShare";
import type { AgentSession } from "./types";

// The server's sensitivity reasons, by wire code, in the words the owner sees.
const SENSITIVE: Record<string, string> = {
  health: "Medical",
  finance: "Financial",
  location: "Location",
  mail: "Email",
  memory: "Memory",
};

function sensitiveLabels(codes: readonly string[]): string[] {
  return codes.flatMap((code) => (SENSITIVE[code] ? [SENSITIVE[code] as string] : []));
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function usage(link: ChatShareLink): string {
  const opened =
    link.view_count === 0
      ? "Not opened yet"
      : `Opened ${link.view_count} time${link.view_count === 1 ? "" : "s"}`;
  return link.last_viewed_at ? `${opened} · last ${when(link.last_viewed_at)}` : opened;
}

async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}

export function ChatShareSheet({
  session,
  onClose,
  onChanged,
}: {
  session: AgentSession;
  onClose: () => void;
  /** A link was minted or revoked — the Chats list refreshes its link glyph. */
  onChanged?: (() => void) | undefined;
}): ReactNode {
  const [links, setLinks] = useState<ChatShareLink[] | null>(null);
  const [minted, setMinted] = useState<MintedChatShare | null>(null);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [status, setStatus] = useState("");
  // The session's own scopes until the server answers, so the warning never starts absent.
  const [sensitive, setSensitive] = useState<string[]>(() =>
    session.domain_scopes.filter((code) => code in SENSITIVE),
  );

  const firewalled = sensitiveLabels(sensitive);
  const turns = session.turn_count ?? 0;

  const reload = useCallback(async () => {
    try {
      setLinks(await api.chatShares(session.id));
    } catch {
      setStatus("Couldn’t load this chat’s links.");
    }
  }, [session.id]);

  const loadSensitivity = useCallback(async (): Promise<string[] | null> => {
    try {
      const codes = await api.chatShareSensitivity(session.id);
      setSensitive(codes);
      return codes;
    } catch {
      return null;
    }
  }, [session.id]);

  useEffect(() => {
    void reload();
    void loadSensitivity();
  }, [reload, loadSensitivity]);

  async function mint(): Promise<void> {
    setBusy(true);
    try {
      const m = await api.mintChatShare(session.id, armed);
      setMinted(m);
      setStatus(
        (await copyText(chatShareUrl(m.token)))
          ? "Link created and copied"
          : "Link created. Copy it below.",
      );
      onChanged?.();
      void reload();
      setArmed(false);
    } catch (err) {
      // The chat read something private since the sheet loaded: show why and re-arm.
      if (err instanceof ApiError && err.status === 409 && (await loadSensitivity())?.length) {
        setArmed(true);
        setStatus("");
        return;
      }
      setStatus("Couldn’t create the link. Try again.");
      setArmed(false);
    } finally {
      setBusy(false);
    }
  }

  async function send(url: string): Promise<void> {
    if (typeof navigator.share === "function") {
      try {
        await navigator.share({ title: session.title, url });
        return;
      } catch {
        // A cancelled share sheet is not an error; fall back to the clipboard.
      }
    }
    setStatus((await copyText(url)) ? "Link copied" : "Couldn’t copy. Select the link instead.");
  }

  async function revoke(id: string): Promise<void> {
    try {
      await api.revokeChatShare(id);
      setLinks((xs) => xs?.filter((x) => x.id !== id) ?? xs);
      if (minted?.id === id) setMinted(null);
      setStatus("Link revoked");
      onChanged?.();
    } catch {
      setStatus("Couldn’t revoke the link. Try again.");
    }
  }

  // The fresh link's URL shows once; older links can only be revoked (their secret is gone).
  const others = (links ?? []).filter((l) => l.id !== minted?.id);
  const url = minted ? chatShareUrl(minted.token) : null;

  return (
    <Sheet title="Share chat" onClose={onClose}>
      <div className="cshare">
        <p className="cshare-chat">
          {session.title || "Untitled chat"} · {turns} turn{turns === 1 ? "" : "s"}
        </p>
        <p className="cshare-sub">
          Anyone with the link can read the whole chat, tool steps included. Each time they open it,
          they see it as it is then, so turns you add later show up on their next load.
        </p>

        {firewalled.length > 0 && (
          <div className="cshare-warn">
            <ShieldIcon size={16} />
            <span>
              This chat can read or has read your <b>{firewalled.join(", ")}</b>. Anything it quoted
              from them will be visible to anyone with the link.
            </span>
          </div>
        )}

        {minted && url && (
          <>
            <div className="cshare-link">
              <span className="cshare-url">{url}</span>
              <button
                type="button"
                className="cshare-copy"
                onClick={async () =>
                  setStatus(
                    (await copyText(url))
                      ? "Link copied"
                      : "Couldn’t copy. Select the link instead.",
                  )
                }
              >
                <CopyIcon size={14} /> Copy
              </button>
            </div>
            <button type="button" className="cshare-send" onClick={() => void send(url)}>
              Send with…
            </button>
            <p className="cshare-once">Copy it now. The link isn’t shown again.</p>
            <div className="cshare-row">
              <EyeIcon size={14} />
              <span className="cshare-usage">{usage(minted)}</span>
              <button
                type="button"
                className="cshare-revoke"
                onClick={() => void revoke(minted.id)}
              >
                Revoke
              </button>
            </div>
          </>
        )}

        {!minted && (
          <button
            type="button"
            className={`cshare-mint${armed ? " cshare-mint-armed" : ""}`}
            disabled={busy}
            onClick={() => {
              if (firewalled.length > 0 && !armed) {
                setArmed(true);
                return;
              }
              void mint();
            }}
          >
            <LinkIcon size={17} />
            {busy ? "Creating…" : armed ? "Tap again to share" : "Create share link"}
          </button>
        )}

        {others.length > 0 && (
          <div className="cshare-list">
            <div className="cshare-list-h">Active links</div>
            {others.map((l) => (
              <div key={l.id} className="cshare-row">
                <LinkIcon size={14} />
                <span className="cshare-usage">
                  Created {when(l.created_at)} · {usage(l)}
                </span>
                <button type="button" className="cshare-revoke" onClick={() => void revoke(l.id)}>
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}

        <p className="cshare-status" aria-live="polite">
          {status}
        </p>
      </div>
    </Sheet>
  );
}
