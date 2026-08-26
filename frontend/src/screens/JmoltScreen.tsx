import { useCallback, useEffect, useState } from "react";

import type {
  MoltbookAction,
  MoltbookJournalEntry,
  MoltbookNight,
  MoltbookOutboxItem,
  MoltbookRegisterResult,
  MoltbookScratchFile,
  MoltbookScratchVersion,
  MoltbookSettings,
  MoltbookTurn,
} from "../api/client";
import { ApiError, api } from "../api/client";
import { inertText, outboxPreview } from "../moltbookSafe";

// jmolt's own launcher screen (docs/plans/JMOLT_PLAN.md): the account + operating switches,
// the review queue of everything it staged, and the nightly-run schedule. Everything is
// operated here from the PWA — no terminal. Third-party / jmolt-authored text (the outbox
// payloads) is rendered as INERT text only (M15).

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

function localDateTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString([], {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function formatTokens(n: number | null): string {
  if (n == null) return "";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k tok` : `${n} tok`;
}

function formatBytes(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`;
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

  // History browser: jmolt's nights (the date spine), the open night's transcript, its
  // action ledger, and its notebook (files + a selected file's contents/history).
  const [nights, setNights] = useState<MoltbookNight[] | null>(null);
  const [openNight, setOpenNight] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<MoltbookTurn[] | null>(null);
  const [actions, setActions] = useState<MoltbookAction[] | null>(null);
  const [files, setFiles] = useState<MoltbookScratchFile[] | null>(null);
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileHistory, setFileHistory] = useState<MoltbookScratchVersion[] | null>(null);

  // jmolt's journal (its line to the owner), and the owner's advisory note back to jmolt.
  // `noteDraft` is the editable buffer; `noteSaved` tracks whether it matches what's stored.
  const [journal, setJournal] = useState<MoltbookJournalEntry[] | null>(null);
  const [noteDraft, setNoteDraft] = useState<string | null>(null);
  const [noteSaved, setNoteSaved] = useState(false);

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

  // Load the history spine once registered: nights, the action ledger, and the notebook.
  useEffect(() => {
    if (!moltbook?.key_set) return;
    api
      .getMoltbookNights()
      .then(setNights)
      .catch(() => setNights([]));
    api
      .getMoltbookActions()
      .then(setActions)
      .catch(() => setActions([]));
    api
      .getMoltbookFiles()
      .then(setFiles)
      .catch(() => setFiles([]));
    api
      .getMoltbookJournal()
      .then(setJournal)
      .catch(() => setJournal([]));
  }, [moltbook?.key_set]);

  // Seed the advisory-note editor from the stored value the first time settings arrive,
  // without clobbering an in-progress edit (only seed while the draft is still unset).
  useEffect(() => {
    if (moltbook && noteDraft === null) setNoteDraft(moltbook.advisory_note);
  }, [moltbook, noteDraft]);

  function selectNight(sessionId: string) {
    if (openNight === sessionId) {
      setOpenNight(null);
      return;
    }
    setOpenNight(sessionId);
    setTranscript(null);
    api
      .getMoltbookTranscript(sessionId)
      .then(setTranscript)
      .catch(() => setTranscript([]));
  }

  function selectFile(filename: string) {
    if (openFile === filename) {
      setOpenFile(null);
      return;
    }
    setOpenFile(filename);
    setFileContent(null);
    setFileHistory(null);
    api
      .getMoltbookFileContent(filename)
      .then((r) => setFileContent(r.content ?? ""))
      .catch(() => setFileContent(""));
  }

  function loadFileHistory(filename: string) {
    if (fileHistory !== null) {
      setFileHistory(null);
      return;
    }
    api
      .getMoltbookFileHistory(filename)
      .then(setFileHistory)
      .catch(() => setFileHistory([]));
  }

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

  function saveAdvisoryNote() {
    if (noteDraft === null) return;
    void api.updateMoltbookSettings({ advisory_note: noteDraft }).then((s) => {
      setMoltbook(s);
      setNoteSaved(true);
    });
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
          <p className="settings-meta">Wake time (your local hour)</p>
          <div className="molt-wake-row">
            <span>Wake time</span>
            <span className="molt-wake-value">
              <strong>{hourLabel(moltbook.night_hour)}</strong>
              <span className="molt-wake-chevron" aria-hidden="true">
                ›
              </span>
            </span>
            {/* A real <input type=time> stretched over the row, so tapping opens the
                device's native picker. night_hour is hour-only; minutes snap to :00. */}
            <input
              type="time"
              step={3600}
              value={hourLabel(moltbook.night_hour)}
              aria-label="Nightly wake hour"
              onChange={(e) => {
                const hour = Number.parseInt(e.target.value.split(":")[0] ?? "", 10);
                if (!Number.isNaN(hour) && hour !== moltbook.night_hour) setNightHour(hour);
              }}
            />
          </div>
          <p className="settings-meta molt-wake-hint">
            jmolt wakes on the hour — minutes snap back to :00.
          </p>
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

      {/* ── Notes to jmolt: the owner's advisory note (human → jmolt) ─────── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Notes to jmolt</h2>
          <p className="settings-meta">
            A note jmolt reads at the start of its next night. It knows this is really from you —
            but it's a comment, not a command: jmolt weighs it and decides for itself, and it never
            changes jmolt's rules or switches. Leave it blank for none.
          </p>
          <textarea
            className="molt-note"
            aria-label="Note to jmolt"
            rows={4}
            placeholder="e.g. No pressure, but you mentioned the tide-pool submol — might be worth a look."
            value={noteDraft ?? ""}
            onChange={(e) => {
              setNoteDraft(e.target.value);
              setNoteSaved(false);
            }}
          />
          <div className="molt-note-actions">
            <button
              type="button"
              className="seg"
              disabled={noteDraft === null || noteDraft === moltbook.advisory_note}
              onClick={saveAdvisoryNote}
            >
              Save note
            </button>
            {noteSaved && noteDraft === moltbook.advisory_note && (
              <span className="settings-meta" style={{ margin: 0 }}>
                Saved — jmolt will see it next night.
              </span>
            )}
          </div>
        </section>
      )}

      {/* ── Journal: jmolt's own line to you (jmolt → human) ──────────────── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Journal</h2>
          <p className="settings-meta">
            jmolt's own words to you, night by night — what it did, what it's turning over. It
            writes these itself; you never edit them.
          </p>
          {journal === null ? (
            <p className="settings-meta">Loading…</p>
          ) : journal.length === 0 ? (
            <p className="settings-meta">Nothing yet — jmolt hasn't left a journal entry.</p>
          ) : (
            <ul className="molt-journal">
              {journal.map((e, i) => (
                <li key={`${e.at ?? "x"}-${i}`} className="molt-journal-entry">
                  <span className="molt-journal-at">{localDateTime(e.at)}</span>
                  {/* Inert text only (M15): jmolt-authored, one hop from forum text. */}
                  <p className="molt-journal-body">{inertText(e.content)}</p>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* ── Nights: the run history (date spine) + each night's transcript ─── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Nights</h2>
          <p className="settings-meta">
            Every night jmolt has run, newest first. Tap one to read its full transcript — what it
            thought and said that hour.
          </p>
          {nights === null ? (
            <p className="settings-meta">Loading…</p>
          ) : nights.length === 0 ? (
            <p className="settings-meta">No nights yet — jmolt hasn't run.</p>
          ) : (
            <ul className="molt-nights">
              {nights.map((n) => (
                <li key={n.session_id} className="molt-night">
                  <button
                    type="button"
                    className={`molt-night-head${openNight === n.session_id ? " on" : ""}`}
                    aria-expanded={openNight === n.session_id}
                    onClick={() => selectNight(n.session_id)}
                  >
                    <span className="molt-night-when">{localDateTime(n.at)}</span>
                    <span className="molt-night-meta">
                      <span className={`molt-status molt-status-${n.status ?? "none"}`}>
                        {n.status ?? "—"}
                      </span>
                      {n.sittings > 1 && <span>{n.sittings} sittings</span>}
                      {n.steps != null && <span>{n.steps} steps</span>}
                      {n.cost_tokens != null && <span>{formatTokens(n.cost_tokens)}</span>}
                    </span>
                  </button>
                  {openNight === n.session_id && (
                    <div className="molt-transcript">
                      {transcript === null ? (
                        <p className="settings-meta">Loading transcript…</p>
                      ) : transcript.length === 0 ? (
                        <p className="settings-meta">No transcript recorded for this night.</p>
                      ) : (
                        transcript.map((t, i) => (
                          <div
                            key={`${i}-${t.at ?? ""}`}
                            className={`molt-turn molt-turn-${t.role}`}
                          >
                            <span className="molt-turn-role">{t.role}</span>
                            {t.content && <p className="molt-turn-body">{inertText(t.content)}</p>}
                            {t.reasoning && (
                              <p className="molt-turn-reasoning">{inertText(t.reasoning)}</p>
                            )}
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* ── Activity: the action ledger (posts, votes, fetches) ───────────── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Activity</h2>
          <p className="settings-meta">
            Everything jmolt did — posts, comments, votes, follows, and each web search or fetch —
            newest first, with the content it reacted to.
          </p>
          {actions === null ? (
            <p className="settings-meta">Loading…</p>
          ) : actions.length === 0 ? (
            <p className="settings-meta">No actions logged yet.</p>
          ) : (
            <ul className="molt-actions">
              {actions.map((a, i) => (
                <li key={`${i}-${a.at ?? ""}`} className="molt-action">
                  <span className="molt-action-head">
                    <span className="molt-action-kind">{inertText(a.action)}</span>
                    <span className="molt-action-when">{localDateTime(a.at)}</span>
                  </span>
                  {a.target && <span className="molt-action-target">{inertText(a.target)}</span>}
                  {a.reacted_to && (
                    <span className="molt-action-reacted">re: {inertText(a.reacted_to)}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* ── Notebook: the scratchpad files + each file's history ──────────── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Notebook</h2>
          <p className="settings-meta">
            jmolt's scratchpad — the only memory it carries between nights. Tap a file to read it,
            or show its history to walk back through earlier versions.
          </p>
          {files === null ? (
            <p className="settings-meta">Loading…</p>
          ) : files.length === 0 ? (
            <p className="settings-meta">jmolt hasn't written any notes yet.</p>
          ) : (
            <ul className="molt-files">
              {files.map((f) => (
                <li key={f.filename} className="molt-file">
                  <button
                    type="button"
                    className={`molt-file-head${openFile === f.filename ? " on" : ""}`}
                    aria-expanded={openFile === f.filename}
                    onClick={() => selectFile(f.filename)}
                  >
                    <span className="molt-file-name">{inertText(f.filename)}</span>
                    <span className="molt-file-meta">
                      {formatBytes(f.bytes)} · {localDateTime(f.updated_at)}
                    </span>
                  </button>
                  {openFile === f.filename && (
                    <div className="molt-file-body">
                      {fileContent === null ? (
                        <p className="settings-meta">Loading…</p>
                      ) : (
                        <pre className="molt-file-content">{inertText(fileContent)}</pre>
                      )}
                      <div className="settings-actions">
                        <button
                          type="button"
                          className="seg"
                          onClick={() => loadFileHistory(f.filename)}
                        >
                          {fileHistory === null ? "Show history" : "Hide history"}
                        </button>
                      </div>
                      {fileHistory !== null &&
                        (fileHistory.length === 0 ? (
                          <p className="settings-meta">No earlier versions.</p>
                        ) : (
                          <ul className="molt-versions">
                            {fileHistory.map((v, i) => (
                              <li key={`${i}-${v.at ?? ""}`} className="molt-version">
                                <span className="molt-version-when">
                                  {v.op} · {localDateTime(v.at)} · {formatBytes(v.bytes)}
                                </span>
                                <pre className="molt-file-content">{inertText(v.content)}</pre>
                              </li>
                            ))}
                          </ul>
                        ))}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </main>
  );
}
