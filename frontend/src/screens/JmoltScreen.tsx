import { useCallback, useEffect, useState } from "react";

import type { MoltbookOutboxItem, MoltbookRegisterResult, MoltbookSettings } from "../api/client";
import { ApiError, api } from "../api/client";
import { inertText, outboxPreview } from "../moltbookSafe";

// jmolt's own launcher screen (docs/plans/JMOLT_PLAN.md): the account + operating switches,
// the review queue of everything it staged, and the nightly-run schedule. Everything is
// operated here from the PWA — no terminal. Third-party / jmolt-authored text (the outbox
// payloads) is rendered as INERT text only (M15).

const HOURS = Array.from({ length: 24 }, (_, h) => h);

function hourLabel(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}

function localTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

export function JmoltScreen() {
  const [moltbook, setMoltbook] = useState<MoltbookSettings | null>(null);
  const [moltName, setMoltName] = useState("jmolt");
  const [moltDesc, setMoltDesc] = useState("");
  const [moltRegistering, setMoltRegistering] = useState(false);
  const [moltClaim, setMoltClaim] = useState<MoltbookRegisterResult | null>(null);
  const [moltClaimState, setMoltClaimState] = useState<string | null>(null);
  const [moltError, setMoltError] = useState<string | null>(null);
  const [moltOutbox, setMoltOutbox] = useState<MoltbookOutboxItem[] | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .getMoltbookSettings()
      .then((s) => {
        if (!stale) setMoltbook(s);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  const loadMoltbookOutbox = useCallback(() => {
    api
      .getMoltbookOutbox()
      .then(setMoltOutbox)
      .catch(() => setMoltOutbox([]));
  }, []);
  useEffect(() => {
    if (moltbook?.key_set) loadMoltbookOutbox();
  }, [moltbook?.key_set, loadMoltbookOutbox]);

  function actOnMoltbookOutbox(id: string, action: "release" | "discard") {
    void api
      .actOnMoltbookOutbox(id, action)
      .then(loadMoltbookOutbox)
      .catch((e: unknown) =>
        setMoltError(e instanceof ApiError ? e.message : "Could not update the queue."),
      );
  }

  function clearMoltbookStreak() {
    void api.updateMoltbookSettings({ clear_streak: true }).then(setMoltbook);
  }

  function registerMoltbook() {
    const name = moltName.trim();
    if (!name) return;
    setMoltError(null);
    setMoltRegistering(true);
    api
      .registerMoltbook(name, moltDesc.trim())
      .then((claim) => {
        setMoltClaim(claim);
        return api.getMoltbookSettings().then(setMoltbook);
      })
      .catch((e: unknown) =>
        setMoltError(e instanceof ApiError ? e.message : "Registration failed."),
      )
      .finally(() => setMoltRegistering(false));
  }

  function toggleMoltbookAutonomy() {
    if (moltbook === null) return;
    void api.updateMoltbookSettings({ autonomy: !moltbook.autonomy }).then(setMoltbook);
  }

  function toggleMoltbookKill() {
    if (moltbook === null) return;
    void api.updateMoltbookSettings({ killed: !moltbook.killed }).then(setMoltbook);
  }

  function toggleNightEnabled() {
    if (moltbook === null) return;
    void api.updateMoltbookSettings({ night_enabled: !moltbook.night_enabled }).then(setMoltbook);
  }

  function setNightHour(hour: number) {
    void api.updateMoltbookSettings({ night_hour: hour }).then(setMoltbook);
  }

  function disconnectMoltbook() {
    setMoltClaim(null);
    setMoltClaimState(null);
    void api.updateMoltbookSettings({ clear_key: true }).then(setMoltbook);
  }

  function checkMoltbookClaim() {
    setMoltClaimState(null);
    api
      .getMoltbookClaimStatus()
      .then((s) => setMoltClaimState(s.status))
      .catch((e: unknown) =>
        setMoltClaimState(e instanceof ApiError ? e.message : "could not check"),
      );
  }

  const outbox = moltOutbox ?? [];
  const scheduledPosts = outbox
    .filter((i) => i.publish_at)
    .sort((a, b) => (a.publish_at ?? "").localeCompare(b.publish_at ?? ""));

  return (
    <main className="screen-body settings">
      {/* ── Account + operating switches ─────────────────────────────── */}
      <section className="settings-card">
        <div className="settings-cardhead">
          <h2 className="settings-label">Account</h2>
          <span
            className={`settings-pill${moltbook?.key_set ? " on" : ""}`}
            aria-label="Moltbook status"
          >
            <span className="dot" />
            {moltbook === null
              ? "…"
              : moltbook.killed
                ? "Paused"
                : moltbook.key_set
                  ? `@${moltbook.handle || "registered"}`
                  : "Not registered"}
          </span>
        </div>
        <p className="settings-meta">
          jmolt is an autonomous persona that spends one hour a night on Moltbook, the social
          network of AI agents. Register its account here, then verify it (email + a tweet from your
          X account) to activate it. Nothing it writes goes public while the autonomy switch is off
          — you review and release each item.
        </p>

        {!moltbook?.key_set && (
          <>
            <label className="settings-field">
              Handle
              <input
                type="text"
                autoComplete="off"
                placeholder="jmolt"
                value={moltName}
                onChange={(e) => setMoltName(e.target.value)}
              />
            </label>
            <label className="settings-field">
              Bio (optional)
              <input
                type="text"
                autoComplete="off"
                placeholder="an autonomous experiment"
                value={moltDesc}
                onChange={(e) => setMoltDesc(e.target.value)}
              />
            </label>
            <div className="settings-actions">
              <button
                type="button"
                className="seg"
                disabled={moltRegistering || !moltName.trim()}
                onClick={registerMoltbook}
              >
                {moltRegistering ? "Registering…" : "Register jmolt"}
              </button>
            </div>
          </>
        )}

        {moltClaim && (
          <div className="settings-meta">
            <p>
              Registered as <strong>@{moltClaim.handle}</strong>. To activate it, open the claim
              link, verify your email, and post the verification tweet from the X account you want
              this agent tied to:
            </p>
            <p>
              <a href={moltClaim.claim_url} target="_blank" rel="noreferrer">
                {moltClaim.claim_url}
              </a>
            </p>
            <p>
              Reference code: <code>{moltClaim.verification_code}</code>
            </p>
          </div>
        )}

        {moltbook?.key_set && (
          <>
            <div className="settings-switch-row">
              <span className="settings-meta" style={{ margin: 0 }}>
                Autonomy — auto-publish what jmolt writes (off = queue for your review)
              </span>
              <button
                type="button"
                role="switch"
                aria-label="Autonomy"
                aria-checked={moltbook.autonomy}
                className={`settings-switch${moltbook.autonomy ? " on" : ""}`}
                onClick={toggleMoltbookAutonomy}
              >
                <span className="knob" />
              </button>
            </div>
            <div className="settings-switch-row">
              <span className="settings-meta" style={{ margin: 0 }}>
                Pause — halt jmolt's nightly run and daytime publishing entirely
              </span>
              <button
                type="button"
                role="switch"
                aria-label="Pause jmolt"
                aria-checked={moltbook.killed}
                className={`settings-switch${moltbook.killed ? " on" : ""}`}
                onClick={toggleMoltbookKill}
              >
                <span className="knob" />
              </button>
            </div>
            <p className="settings-meta">Bio header (fixed): {moltbook.disclosure}</p>
            {moltbook.account_state !== "ok" && (
              <p className="settings-meta settings-error">
                {moltbook.account_state === "tamper"
                  ? "Possible key leak: a post appeared on jmolt's profile that did not go through this queue. Writing is paused — rotate the Moltbook key."
                  : moltbook.account_state === "suspended"
                    ? "Moltbook reports the account as suspended. The nightly run and publishing are paused."
                    : "Moltbook has flagged or rate-limited the account."}
              </p>
            )}
            {moltbook.verify_fail_streak > 0 && (
              <p className="settings-meta">
                Verification failures in a row: {moltbook.verify_fail_streak}
                {moltbook.verify_fail_streak >= 3 && " — writing is stopped until you clear it."}{" "}
                <button type="button" className="seg" onClick={clearMoltbookStreak}>
                  Clear streak
                </button>
              </p>
            )}
            <div className="settings-actions">
              <button type="button" className="seg" onClick={checkMoltbookClaim}>
                Check claim status
              </button>
              <button type="button" className="seg" onClick={disconnectMoltbook}>
                Disconnect
              </button>
            </div>
            {moltClaimState && <p className="settings-meta">Claim status: {moltClaimState}</p>}
          </>
        )}
        {moltError && <p className="settings-meta settings-error">{moltError}</p>}
      </section>

      {/* ── Pending posts (the review queue) ─────────────────────────── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Pending posts</h2>
          <p className="settings-meta">
            Everything jmolt has staged, awaiting your release. While autonomy is off, nothing here
            goes public until you release it.
          </p>
          {outbox.length === 0 ? (
            <p className="settings-meta">Nothing staged right now.</p>
          ) : (
            <ul className="molt-outbox">
              {outbox.map((item) => (
                <li key={item.id} className="molt-outbox-item">
                  <div className="molt-outbox-body">
                    {/* Inert text only (M15): React escapes it; inertText strips
                        invisibles + defangs links. */}
                    <span className="molt-outbox-kind">
                      {inertText(item.kind)}
                      {item.status === "released" ? " (released)" : ""}
                      {item.publish_at ? ` · ${localTime(item.publish_at)}` : ""}
                    </span>
                    <span className="molt-outbox-preview">{outboxPreview(item.payload)}</span>
                    {item.error && <span className="settings-error">{inertText(item.error)}</span>}
                  </div>
                  <div className="molt-outbox-actions">
                    {item.status === "queued" && (
                      <button
                        type="button"
                        className="seg"
                        onClick={() => actOnMoltbookOutbox(item.id, "release")}
                      >
                        Release
                      </button>
                    )}
                    <button
                      type="button"
                      className="seg"
                      onClick={() => actOnMoltbookOutbox(item.id, "discard")}
                    >
                      Discard
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* ── Schedule: nightly wake hour + drip-publish times ─────────── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Schedule</h2>
          <div className="settings-switch-row">
            <span className="settings-meta" style={{ margin: 0 }}>
              Nightly run — wake for an hour each night (off keeps the account + daytime publishing,
              but jmolt won't wake)
            </span>
            <button
              type="button"
              role="switch"
              aria-label="Nightly run"
              aria-checked={moltbook.night_enabled}
              className={`settings-switch${moltbook.night_enabled ? " on" : ""}`}
              onClick={toggleNightEnabled}
            >
              <span className="knob" />
            </button>
          </div>
          <p className="settings-meta">
            Wake time (your local hour) — currently {hourLabel(moltbook.night_hour)}
          </p>
          <div className="molt-hours" aria-label="Nightly wake hour">
            {HOURS.map((h) => (
              <button
                key={h}
                type="button"
                aria-pressed={h === moltbook.night_hour}
                aria-label={`Wake at ${hourLabel(h)}`}
                className={`molt-hour${h === moltbook.night_hour ? " on" : ""}`}
                onClick={() => setNightHour(h)}
              >
                {hourLabel(h)}
              </button>
            ))}
          </div>
          <div className="settings-subsection">
            <p className="settings-meta" style={{ margin: 0 }}>
              Queued to publish today
            </p>
            {scheduledPosts.length === 0 ? (
              <p className="settings-meta">No posts scheduled to drip out yet.</p>
            ) : (
              <ul className="molt-outbox">
                {scheduledPosts.map((item) => (
                  <li key={item.id} className="molt-outbox-item">
                    <div className="molt-outbox-body">
                      <span className="molt-outbox-kind">{localTime(item.publish_at ?? "")}</span>
                      <span className="molt-outbox-preview">{outboxPreview(item.payload)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      )}
    </main>
  );
}
