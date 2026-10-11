// The owner's half of the map share: pick the player the link opens on, copy the link,
// revoke old ones. The visitor's half is `McShareApp`.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, type MinecraftMapPlayer, type MinecraftMapShare, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import { CopyIcon, LinkIcon } from "../components/icons";
import { agoOf } from "../minecraft";
import { mcShareUrl } from "../minecraftMapSource";

/** Online first, then whoever was seen most recently. */
function byRecent(players: MinecraftMapPlayer[]): MinecraftMapPlayer[] {
  return [...players].sort((a, b) => Number(b.online) - Number(a.online) || b.at - a.at);
}

export function ShareSheet({
  slot,
  world,
  players,
  nowMs,
  onClose,
}: {
  slot: string;
  world: string;
  players: MinecraftMapPlayer[];
  nowMs: number;
  onClose: () => void;
}) {
  const people = useMemo(() => byRecent(players), [players]);
  const [pick, setPick] = useState<string | null>(people[0]?.xuid ?? null);
  const [link, setLink] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [links, setLinks] = useState<MinecraftMapShare[]>([]);

  const load = useCallback(async () => {
    try {
      setLinks(await api.minecraftMapShares());
    } catch {
      setLinks([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    if (!pick) return;
    setBusy(true);
    setProblem(null);
    try {
      const made = await api.minecraftMintMapShare(slot, pick);
      setLink(mcShareUrl(made.token));
      await load();
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Couldn't make the link.");
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setProblem("Couldn't reach the clipboard — select the link and copy it.");
    }
  }

  async function send() {
    if (!link) return;
    try {
      await navigator.share({ title: `${world} — Minecraft map`, url: link });
    } catch {
      // Dismissing the share sheet is not an error worth saying.
    }
  }

  async function revoke(id: string) {
    try {
      await api.minecraftRevokeMapShare(id);
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Couldn't revoke that link.");
    }
    await load();
  }

  const chosen = people.find((p) => p.xuid === pick);
  const canSend = typeof navigator.share === "function";

  return (
    <Sheet title="Share the map" onClose={onClose}>
      <div className="mc-share">
        <p className="mc-note">
          A view-only link to <b>{world}</b>: every player&apos;s live position, travel history and
          timeline, opened on the player you pick. Nothing else on JBrain. It works until you revoke
          it.
        </p>

        {link ? (
          <div className="mc-share-ready">
            <label className="mc-share-head" htmlFor="mc-share-url">
              Send this link{chosen ? ` · opens on ${chosen.gamertag}` : ""}
            </label>
            <input
              id="mc-share-url"
              className="mc-share-url"
              readOnly
              value={link}
              onFocus={(e) => e.currentTarget.select()}
            />
            <div className="mc-btnrow">
              <button type="button" className="mc-btn mc-btn-primary" onClick={() => void copy()}>
                <CopyIcon size={16} />
                {copied ? "Copied" : "Copy link"}
              </button>
              {canSend && (
                <button type="button" className="mc-btn" onClick={() => void send()}>
                  <LinkIcon size={16} />
                  Send…
                </button>
              )}
            </div>
          </div>
        ) : people.length ? (
          <fieldset className="mc-share-pick">
            <legend className="mc-share-head">Open the map on</legend>
            {people.map((p) => (
              <label key={p.xuid} className="mc-share-player">
                <input
                  type="radio"
                  name="mc-share-player"
                  checked={pick === p.xuid}
                  onChange={() => setPick(p.xuid)}
                />
                <span className="mc-navtx">
                  <span className="ops-card-title">{p.gamertag}</span>
                  <span className="mc-navmeta">
                    {p.online ? "online now" : `last seen ${agoOf(p.at, nowMs)}`}
                  </span>
                </span>
                {p.online && <span className="mc-dot mc-dot-ok" aria-hidden="true" />}
              </label>
            ))}
            <button
              type="button"
              className="mc-btn mc-btn-primary"
              disabled={!pick || busy}
              onClick={() => void create()}
            >
              <LinkIcon size={16} />
              {busy ? "Making the link…" : "Create link"}
            </button>
          </fieldset>
        ) : (
          <p className="mc-note">
            Nobody has played in this world yet, so there&apos;s no one to open the map on.
          </p>
        )}

        {problem && (
          <p className="mc-note mc-share-problem" role="alert">
            {problem}
          </p>
        )}

        {links.length > 0 && (
          <div className="mc-share-links">
            <div className="mc-share-head">Active links</div>
            {links.map((l) => (
              <div key={l.id} className="mc-share-link">
                <span className="mc-navtx">
                  <span className="ops-card-title">{l.gamertag}</span>
                  <span className="mc-navmeta">
                    {l.slot === slot ? world : `Slot ${l.slot.replace(/\D/g, "")}`} ·{" "}
                    {l.view_count
                      ? `opened ${l.view_count} time${l.view_count === 1 ? "" : "s"}`
                      : "not opened yet"}
                    {l.last_viewed_at ? ` · last ${agoOf(l.last_viewed_at, nowMs)}` : ""}
                  </span>
                </span>
                <button
                  type="button"
                  className="mc-btn mc-btn-danger"
                  aria-label={`Revoke the link for ${l.gamertag}`}
                  onClick={() => void revoke(l.id)}
                >
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </Sheet>
  );
}
