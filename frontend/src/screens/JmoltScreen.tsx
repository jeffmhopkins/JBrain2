import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  MoltbookActivity,
  MoltbookActivityQuery,
  MoltbookActivityStat,
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
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M tok`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k tok`;
  return `${n} tok`;
}

// A short relative time for the schedule/drip status ("in 14h 20m", "5 min ago").
function fromNow(iso: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = t - Date.now();
  const mins = Math.round(Math.abs(diff) / 60000);
  if (mins < 1) return "just now";
  let s: string;
  if (mins < 60) s = `${mins} min`;
  else if (mins < 1440) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    s = m ? `${h}h ${m}m` : `${h}h`;
  } else s = `${Math.round(mins / 1440)}d`;
  return diff >= 0 ? `in ${s}` : `${s} ago`;
}

function formatBytes(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`;
}

// Activity feed. One page is this many rows; "show older" pages by the oldest row's seq.
const ACT_PAGE = 60;

// The drip heartbeat is stamped every ~60s; older than this and the sweep loop is stalled.
const DRIP_STALE_MS = 5 * 60_000;

// Each activity state maps to a badge label + class (see styles.css .molt-badge-*). "drafted"
// covers both queued (awaiting release) and released-but-not-yet-sent; "scheduled" is a
// drip-queued post with a future publish time.
const ACT_BADGE: Record<string, string> = {
  published: "Published",
  scheduled: "Scheduled",
  drafted: "Drafted",
  failed: "Failed",
};

// A compact "Aug 26, 3:47 AM" for the collapsed row's right edge.
function actWhen(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// Build the activity query, omitting keys rather than passing undefined (exactOptionalPropertyTypes).
function activityQuery(
  status: "all" | "drafted" | "published",
  activeKinds: string[],
  allKinds: string[],
  cursor?: number,
): MoltbookActivityQuery {
  const q: MoltbookActivityQuery = { limit: ACT_PAGE };
  if (status !== "all") q.status = status;
  if (activeKinds.length < allKinds.length) q.kinds = activeKinds;
  if (cursor) q.cursor = cursor;
  return q;
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
  const [activity, setActivity] = useState<MoltbookActivity[] | null>(null);
  // Activity filters: an All / Drafted / Published status segment, per-kind hide toggles,
  // honest counts from the /stats aggregate, and keyset paging for "show older".
  const [actStatus, setActStatus] = useState<"all" | "drafted" | "published">("all");
  const [actHidden, setActHidden] = useState<Set<string>>(() => new Set());
  const [actStats, setActStats] = useState<MoltbookActivityStat[] | null>(null);
  const [actMore, setActMore] = useState(false);
  const [files, setFiles] = useState<MoltbookScratchFile[] | null>(null);
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileHistory, setFileHistory] = useState<MoltbookScratchVersion[] | null>(null);

  // jmolt's journal (its line to the owner), and the owner's advisory note back to jmolt.
  // `noteDraft` is the editable buffer; `noteSaved` tracks whether it matches what's stored.
  const [journal, setJournal] = useState<MoltbookJournalEntry[] | null>(null);
  // Which journal entries are expanded — long entries clamp to a preview until opened, so
  // the section stays a scannable list instead of an unbounded wall of full paragraphs.
  const [journalOpen, setJournalOpen] = useState<Set<number>>(() => new Set());
  const [noteDraft, setNoteDraft] = useState<string | null>(null);
  const [noteSaved, setNoteSaved] = useState(false);
  // Auto-grow the note box to its content so the owner's own note is never clipped behind an
  // internal scrollbar (a fixed height hid the tail of a long note). Runs on every value change.
  const noteRef = useRef<HTMLTextAreaElement>(null);
  // Re-measure whenever the value changes (typing, or the load-time seed) even though the
  // effect body reads only the ref, so noteDraft is an intentional trigger-only dependency.
  // biome-ignore lint/correctness/useExhaustiveDependencies: noteDraft is a trigger-only dep
  useEffect(() => {
    const el = noteRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [noteDraft]);

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
      .getMoltbookActivityStats()
      .then(setActStats)
      .catch(() => setActStats([]));
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

  // Kind chips (comment/vote/…) with honest totals from the /stats aggregate, most-used first.
  const kindCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const s of actStats ?? []) m.set(s.kind, (m.get(s.kind) ?? 0) + s.count);
    return m;
  }, [actStats]);
  const kindList = useMemo(
    () => [...kindCounts.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k),
    [kindCounts],
  );
  const activeKinds = useMemo(
    () => kindList.filter((k) => !actHidden.has(k)),
    [kindList, actHidden],
  );

  // The drip sweep stamps its heartbeat at the top of every ~60s tick — before the kill/streak
  // guards, so a live loop refreshes it even while paused. A heartbeat older than a few minutes
  // therefore means the sweep loop itself is not running and daytime publishing is effectively
  // dead — a real fault the calm "Scheduled" pill would otherwise hide. (A never-stamped box
  // reads as "not run yet", not stalled, to avoid a false alarm at first boot.)
  const dripStale =
    !!moltbook?.drip_last_swept &&
    Date.now() - new Date(moltbook.drip_last_swept).getTime() > DRIP_STALE_MS;

  // The Schedule card's status pill: red when writing is paused/stopped/off-nominal (the
  // "collapse unless failing" states), green when a night is running or simply scheduled.
  const schedStatus = !moltbook
    ? { cls: "", txt: "…" }
    : moltbook.killed
      ? { cls: "err", txt: "Paused" }
      : moltbook.account_state !== "ok"
        ? { cls: "err", txt: `Account: ${moltbook.account_state}` }
        : moltbook.verify_fail_streak >= 3
          ? { cls: "err", txt: "Writes stopped" }
          : dripStale
            ? { cls: "err", txt: "Drip stalled" }
            : moltbook.night_running_until
              ? { cls: "on", txt: "Awake now" }
              : { cls: "on", txt: "Scheduled" };

  // (Re)load the first page whenever the status segment or a kind toggle changes. `kinds` is
  // sent only when some are hidden — all-on means "no filter", which the server reads as all.
  useEffect(() => {
    if (!moltbook?.key_set) return;
    let stale = false;
    api
      .getMoltbookActivity(activityQuery(actStatus, activeKinds, kindList))
      .then((rows) => {
        if (stale) return;
        setActivity(rows);
        setActMore(rows.length === ACT_PAGE);
      })
      .catch(() => {
        if (!stale) setActivity([]);
      });
    return () => {
      stale = true;
    };
  }, [moltbook?.key_set, actStatus, activeKinds, kindList]);

  const showOlderActions = useCallback(() => {
    const last = activity?.[activity.length - 1];
    if (!last) return;
    api
      .getMoltbookActivity(activityQuery(actStatus, activeKinds, kindList, last.seq))
      .then((rows) => {
        setActivity((prev) => [...(prev ?? []), ...rows]);
        setActMore(rows.length === ACT_PAGE);
      })
      .catch(() => {});
  }, [activity, activeKinds, kindList, actStatus]);

  const toggleKind = useCallback((kind: string) => {
    setActHidden((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);

  const toggleJournal = useCallback((i: number) => {
    setJournalOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  }, []);

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

      {/* ── Schedule & drip: status + nightly wake hour + drip-publish times ── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <div className="settings-cardhead">
            <h2 className="settings-label">Schedule &amp; drip</h2>
            <span className={`settings-pill ${schedStatus.cls}`} aria-label="Schedule status">
              <span className="dot" />
              {schedStatus.txt}
            </span>
          </div>

          <div className="molt-sched-status">
            {moltbook.night_running_until ? (
              <div className="molt-sched-row">
                <span className="molt-sched-k">Awake now</span>
                <span className="molt-sched-v">
                  until {localTime(moltbook.night_running_until)}
                  <small>{fromNow(moltbook.night_running_until)} left</small>
                </span>
              </div>
            ) : (
              <div className="molt-sched-row">
                <span className="molt-sched-k">Next run</span>
                <span className="molt-sched-v">
                  {moltbook.night_enabled && moltbook.night_next_run ? (
                    <>
                      {localDateTime(moltbook.night_next_run)}
                      <small>{fromNow(moltbook.night_next_run)}</small>
                    </>
                  ) : (
                    "Off"
                  )}
                </span>
              </div>
            )}
            <div className="molt-sched-row">
              <span className="molt-sched-k">Last run</span>
              <span className="molt-sched-v">{moltbook.night_last_run ?? "never"}</span>
            </div>
            <div className="molt-sched-row">
              <span className="molt-sched-k">Drip</span>
              <span className={`molt-sched-v${dripStale ? " molt-sched-stale" : ""}`}>
                {dripStale ? "Not sweeping — loop stalled" : "Publishing every minute"}
                <small>
                  {moltbook.drip_last_swept
                    ? `last swept ${fromNow(moltbook.drip_last_swept)}`
                    : "not run yet"}
                </small>
              </span>
            </div>
            {moltOutbox && moltOutbox.length > 0 && (
              <div className="molt-sched-row">
                <span className="molt-sched-k">Waiting</span>
                <span className="molt-sched-v">
                  {moltOutbox.length} staged
                  {scheduledPosts[0]?.publish_at && (
                    <small>next post drips {localTime(scheduledPosts[0].publish_at)}</small>
                  )}
                </span>
              </div>
            )}
          </div>

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
            ref={noteRef}
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
              {journal.map((e, i) => {
                const open = journalOpen.has(i);
                // Only long entries clamp — a short note reads fine in full, and gets no toggle.
                const long = e.content.length > 240;
                return (
                  <li key={`${e.at ?? "x"}-${i}`} className="molt-journal-entry">
                    <span className="molt-journal-at">{localDateTime(e.at)}</span>
                    {/* Inert text only (M15): jmolt-authored, one hop from forum text. */}
                    <p className={`molt-journal-body${long && !open ? " clamped" : ""}`}>
                      {inertText(e.content)}
                    </p>
                    {long && (
                      <button
                        type="button"
                        className="molt-journal-more"
                        onClick={() => toggleJournal(i)}
                      >
                        {open ? "Show less" : "Show more"}
                      </button>
                    )}
                  </li>
                );
              })}
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

      {/* ── Activity: one compact row per thing jmolt did (outbox-sourced) ── */}
      {moltbook?.key_set && (
        <section className="settings-card">
          <h2 className="settings-label">Activity</h2>
          <p className="settings-meta">
            One row per thing jmolt did — tap to read it and open it on Moltbook. Each row carries
            its own status: drafted, scheduled, published, or failed.
          </p>

          <fieldset className="seg-row molt-act-seg" aria-label="Status">
            {(["all", "drafted", "published"] as const).map((sName) => (
              <button
                key={sName}
                type="button"
                className={`seg${actStatus === sName ? " seg-on" : ""}`}
                aria-pressed={actStatus === sName}
                onClick={() => setActStatus(sName)}
              >
                {sName === "all" ? "All" : sName === "drafted" ? "Drafted" : "Published"}
              </button>
            ))}
          </fieldset>

          {kindList.length > 0 && (
            <div className="filter-chips molt-act-chips">
              {kindList.map((k) => (
                <button
                  key={k}
                  type="button"
                  className={`filter-chip${actHidden.has(k) ? "" : " filter-chip-on"}`}
                  aria-pressed={!actHidden.has(k)}
                  onClick={() => toggleKind(k)}
                >
                  {k} <span className="molt-chip-count">{kindCounts.get(k)}</span>
                </button>
              ))}
            </div>
          )}

          {activity === null ? (
            <p className="settings-meta">Loading…</p>
          ) : activity.length === 0 ? (
            <p className="settings-meta">No activity matches these filters.</p>
          ) : (
            <>
              <ul className="molt-acts">
                {activity.map((a, i) => {
                  // The drip publishes a burst in one sweep, so a run of rows shares one time —
                  // hide the repeat (keeping its width so the chevron column stays aligned).
                  const repeat = i > 0 && actWhen(a.at) === actWhen(activity[i - 1]?.at ?? null);
                  // Under a single-state segment (Published) the badge repeats on every row and
                  // just adds noise — show it only where the list actually mixes states.
                  const showBadge = actStatus !== "published";
                  return (
                    <li key={a.id} className="molt-act">
                      <details>
                        <summary className="molt-act-row">
                          <span className={`molt-act-dot molt-dot-${a.kind}`} aria-hidden="true" />
                          <span className="molt-act-line">
                            <span className="molt-act-verb">{inertText(a.verb)}</span>{" "}
                            {inertText(a.subject)}
                          </span>
                          {showBadge && (
                            <span className={`molt-badge molt-badge-${a.state}`}>
                              {ACT_BADGE[a.state] ?? a.state}
                            </span>
                          )}
                          <span className={`molt-act-when${repeat ? " molt-act-when-repeat" : ""}`}>
                            {actWhen(a.at)}
                          </span>
                          <span className="molt-act-chev" aria-hidden="true">
                            ⌄
                          </span>
                        </summary>
                        <div className="molt-act-detail">
                          {a.body && <p className="molt-act-body">{inertText(a.body)}</p>}
                          {a.error && <p className="molt-act-error">{inertText(a.error)}</p>}
                          {a.link && (
                            <a
                              className="molt-act-link"
                              href={a.link}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              View on Moltbook ↗
                            </a>
                          )}
                          <span className="molt-act-meta">
                            {localDateTime(a.at)} · {a.kind}
                          </span>
                        </div>
                      </details>
                    </li>
                  );
                })}
              </ul>
              {actMore && (
                <button type="button" className="molt-show-older" onClick={showOlderActions}>
                  Show older
                </button>
              )}
            </>
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
