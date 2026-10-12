// The chat share sheet (docs/mocks/chat-share, variant B): mint a revocable, no-login link to
// one chat, copy or send it (shown once — only its hash is stored), see how often it was
// opened, and revoke it. Anyone with the link reads the whole chat, tool steps included, as it
// stands each time they open it. A chat that could read a firewalled domain warns first and
// takes a second tap to publish, because its turns can quote those notes.

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { type ChatShareLink, type MintedChatShare, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import { CopyIcon, EyeIcon, LinkIcon, ShieldIcon } from "../components/icons";
import { chatShareUrl } from "./chatShare";
import type { AgentSession } from "./types";

// The firewalled domains, by wire code, in the words the owner sees.
const FIREWALLED: Record<string, string> = {
  health: "Medical",
  finance: "Financial",
  location: "Location",
};

function firewalledLabels(scopes: readonly string[]): string[] {
  return scopes.flatMap((code) => (FIREWALLED[code] ? [FIREWALLED[code] as string] : []));
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

  const firewalled = firewalledLabels(session.domain_scopes);
  const turns = session.turn_count ?? 0;

  const reload = useCallback(async () => {
    try {
      setLinks(await api.chatShares(session.id));
    } catch {
      setStatus("Couldn’t load this chat’s links.");
    }
  }, [session.id]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function mint(): Promise<void> {
    setBusy(true);
    try {
      const m = await api.mintChatShare(session.id);
      setMinted(m);
      setStatus(
        (await copyText(chatShareUrl(m.token)))
          ? "Link created and copied"
          : "Link created. Copy it below.",
      );
      onChanged?.();
      void reload();
    } catch {
      setStatus("Couldn’t create the link. Try again.");
    } finally {
      setBusy(false);
      setArmed(false);
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
              This chat can read your <b>{firewalled.join(", ")}</b> notes. Anything it quoted from
              them will be visible to anyone with the link.
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
