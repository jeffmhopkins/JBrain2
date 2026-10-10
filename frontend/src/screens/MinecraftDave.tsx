// The Minecraft screen's door to Minecraft_Dave (MINECRAFT_BEDROCK_PLAN.md "P1–P3"):
// start a chat with the persona, and say which player is "me" so that chat begins about
// the owner rather than asking who it is talking to.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { ChatIcon, ChevronRightIcon } from "../components/icons";

export const DAVE_AGENT = "minecraft_dave";

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

export function DaveSection({
  onOpenSession,
}: {
  /** Hands a freshly made session to the shell, which reveals the chat surface (the
   *  same handoff a Tasks run uses). Absent → no Ask row, as there is nowhere to go. */
  onOpenSession?: ((sessionId: string, agent: string) => void) | undefined;
}) {
  // Held as typed; the server normalises spacing and refuses anything that can't be a
  // gamertag, so its 422 reason is shown rather than a guess made here.
  const [tag, setTag] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [tagError, setTagError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        setSaved(s.minecraft_gamertag ?? "");
        setTag(s.minecraft_gamertag ?? "");
      })
      .catch(() => setLoadFailed(true));
  }, []);

  async function save(value: string) {
    setSaving(true);
    setTagError(null);
    try {
      const next = await api.updateSettings({ minecraft_gamertag: value });
      setSaved(next.minecraft_gamertag ?? "");
      setTag(next.minecraft_gamertag ?? "");
    } catch (err) {
      setTagError(err instanceof ApiError ? err.message : "That gamertag wasn't accepted.");
    } finally {
      setSaving(false);
    }
  }

  async function ask() {
    if (!onOpenSession) return;
    setAsking(true);
    setAskError(null);
    try {
      // Dave reads no owner data, so the chat starts with an empty firewall scope.
      const created = await api.createSession({ domain_scopes: [], agent: DAVE_AGENT });
      onOpenSession(created.id, DAVE_AGENT);
    } catch (err) {
      setAskError(`Couldn't start a chat — ${message(err)}`);
      setAsking(false);
    }
  }

  const ready = saved !== null;
  const dirty = ready && tag.trim() !== saved;

  return (
    <>
      <h3 className="mc-sect">
        Minecraft_Dave<span className="mc-sect-r">goals, progress, memory</span>
      </h3>
      <section className="mc-card">
        {onOpenSession && (
          <button type="button" className="mc-navrow" disabled={asking} onClick={() => void ask()}>
            <span className="mc-navico" aria-hidden="true">
              <ChatIcon size={20} />
            </span>
            <span className="mc-navtx">
              <span className="ops-card-title">Ask Minecraft_Dave</span>
              {askError ? (
                <span className="mc-navmeta bad" role="alert">
                  {askError}
                </span>
              ) : (
                <span className="mc-navmeta">
                  {asking
                    ? "Starting a chat…"
                    : saved
                      ? `Starts about ${saved} — who's on, goals, where things are`
                      : "Who's on, each player's goals, where things are"}
                </span>
              )}
            </span>
            <ChevronRightIcon size={18} />
          </button>
        )}
        <div className={`mc-pad${onOpenSession ? " mc-dave-tag" : ""}`}>
          <div className="mc-fld">
            <label htmlFor="mc-my-tag">Your gamertag</label>
            <input
              id="mc-my-tag"
              className="mc-inp"
              value={tag}
              placeholder={loadFailed ? "couldn't read it" : "not set"}
              maxLength={21}
              disabled={!ready}
              spellCheck={false}
              autoCapitalize="off"
              autoComplete="off"
              onChange={(e) => {
                setTag(e.target.value);
                setTagError(null);
              }}
            />
            <span className="mc-note">
              Minecraft_Dave chats start out about this player. Ask about someone else and the chat
              switches to them.
            </span>
          </div>
          {tagError !== null && (
            <p className="mc-note mc-bad" role="alert">
              {tagError}
            </p>
          )}
          <div className="mc-btnrow">
            {saved ? (
              <button
                type="button"
                className="mc-btn"
                aria-label="Clear gamertag"
                disabled={saving}
                onClick={() => void save("")}
              >
                Clear
              </button>
            ) : null}
            <button
              type="button"
              className="mc-btn mc-btn-primary mc-grow"
              // Its own name: the Server settings card below has a Save of its own.
              aria-label="Save gamertag"
              disabled={!dirty || saving}
              onClick={() => void save(tag.trim())}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </section>
    </>
  );
}
