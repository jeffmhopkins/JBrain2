// GrowBot — the desk robot's screen (DESIGN.md "GrowBot — the desk robot's screen"; binding
// mock docs/mocks/growbot-personas/b-three-tabs.html, the owner's pick "B, three tabs"). One
// body, several personas: a persona is a creature's soul, and it lives on a phone alone or
// wears the body. Three tabs, one per job — Personas, Body, Phones — and a page per persona.
// Every move says what happened: a move is a violet banner until OK, an outcome a toast.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  type GrowbotFlashJob,
  type GrowbotFlashRequest,
  type GrowbotOverview,
  type GrowbotPersona,
  type GrowbotRobot,
  type GrowbotStatus,
  type GrowbotTicketFor,
  api,
} from "../api/client";
import { GrowbotAddSheet } from "../components/GrowbotAddSheet";
import { GrowbotFail, GrowbotLog, GrowbotPhases } from "../components/GrowbotJob";
import { GrowbotPersonaPage } from "../components/GrowbotPersonaPage";
import {
  GrowbotAvatar,
  GrowbotImportSheet,
  GrowbotLaunchSheet,
  GrowbotNewPersonaSheet,
  GrowbotWhere,
  NEARBY,
  type Say,
} from "../components/GrowbotPersonaSheets";
import { GrowbotPhoneSheet } from "../components/GrowbotPhoneSheet";
import { Sheet } from "../components/Sheet";
import { useArm } from "../components/growbotArm";
import {
  CERT_URL,
  type Places,
  ago,
  bodyState,
  certNoted,
  day,
  handover,
  keyMadeAt,
  learnThisPhone,
  noteCert,
  noteLaunchHere,
  onPhone,
  phoneName,
  riderOf,
  robotName,
  storedThisPhone,
} from "../growbot";
import { useForeground } from "../visibility";
import "./growbot.css";

const STATUS_POLL_MS = 15_000;
const JOB_POLL_MS = 1_000;
// A launch opens the creature in another tab; one look a little later catches the hand-over
// on a screen that never went to the background (a desk browser, split screen).
const AFTER_LAUNCH_MS = 8_000;

type Tab = "personas" | "body" | "phones";

type SheetState =
  | { kind: "add"; target: string }
  | { kind: "rekey"; code: string }
  | { kind: "cert"; pid: string; code: string | null }
  | { kind: "launch"; pid: string }
  | { kind: "other"; pid: string | null; code: string | null }
  | { kind: "new"; seed: string | null }
  | { kind: "import"; over: string | null }
  | null;

interface Toast {
  text: string;
  href?: string;
  undo?: () => void;
  label?: string;
  k: number;
}

interface GrowbotScreenProps {
  /** jpanel's Flash tab handed a Pico over: open straight on Add a robot. */
  openAdd?: boolean;
  onOpenAddConsumed?: () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function GrowbotScreen({ openAdd, onOpenAddConsumed }: GrowbotScreenProps = {}) {
  const [status, setStatus] = useState<GrowbotStatus | null>(null);
  const [overview, setOverview] = useState<GrowbotOverview | null>(null);
  const [thisPhone, setThisPhone] = useState<string | null>(storedThisPhone);
  const [error, setError] = useState("");
  const [job, setJob] = useState<GrowbotFlashJob | null>(null);
  const [sheet, setSheet] = useState<SheetState>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [banner, setBanner] = useState<{ text: string; k: number } | null>(null);
  const [tab, setTab] = useState<Tab>("personas");
  const [page, setPage] = useState<string | null>(null);
  const [rekeyError, setRekeyError] = useState("");
  const [rekeyLogOpen, setRekeyLogOpen] = useState(false);
  const { isArmed, arm } = useArm();
  const foreground = useForeground();
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seq = useRef(0);
  const prev = useRef<GrowbotOverview | null>(null);
  const robotsRef = useRef<GrowbotRobot[]>([]);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  const say: Say = useCallback((text, opts = {}) => {
    setToast({ text, ...opts, k: ++seq.current });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(
      () => setToast(null),
      opts.href ? 10_000 : opts.undo ? 7_000 : 4_200,
    );
  }, []);

  const tell = useCallback((text: string) => {
    setBanner({ text, k: ++seq.current });
    setToast(null);
  }, []);

  /** Take a new overview, learning this phone first so the banner can say "this phone", then
   *  saying what moved since the last one — whether this screen moved it or another phone. */
  const take = useCallback(
    (next: GrowbotOverview): string | null => {
      const here = learnThisPhone(next);
      setThisPhone(here);
      const last = prev.current;
      prev.current = next;
      setOverview(next);
      if (!last) return null;
      const said = handover(last, next, {
        thisPhone: here,
        phones: next.phones,
        robots: robotsRef.current,
      });
      if (said) tell(said);
      return said;
    },
    [tell],
  );

  const loadStatus = useCallback(async () => {
    try {
      const [s, ov] = await Promise.all([
        api.growbotStatus(),
        // A box before G4 has no personas; the robots still show.
        api
          .growbotPersonas()
          .catch(() => null),
      ]);
      if (!mounted.current) return;
      robotsRef.current = s.robots;
      setStatus(s);
      if (ov) take(ov);
      setError("");
    } catch (e) {
      if (mounted.current) setError(message(e));
    }
  }, [take]);

  const loadJob = useCallback(async () => {
    try {
      const j = await api.growbotFlashJob();
      setJob(j);
      return j;
    } catch {
      // A missed poll is retried on the next tick; the job runs on the box regardless.
      return null;
    }
  }, []);

  useEffect(() => {
    if (!foreground) return;
    void loadStatus();
    void loadJob();
    const id = setInterval(() => void loadStatus(), STATUS_POLL_MS);
    return () => clearInterval(id);
  }, [foreground, loadStatus, loadJob]);

  // Polled every second only while something is flashing; a finished job re-reads the robots,
  // since a flash adds or rekeys one.
  const running = job?.state === "running";
  useEffect(() => {
    if (!running || !foreground) return;
    const id = setInterval(() => {
      void loadJob().then((j) => {
        if (j && j.state !== "running") void loadStatus();
      });
    }, JOB_POLL_MS);
    return () => clearInterval(id);
  }, [running, foreground, loadJob, loadStatus]);

  useEffect(() => {
    if (!openAdd) return;
    setPage(null);
    setTab("body");
    setSheet({ kind: "add", target: "new" });
    onOpenAddConsumed?.();
  }, [openAdd, onOpenAddConsumed]);

  // Each tab keeps its own scroll.
  const bodyRef = useRef<HTMLElement>(null);
  const scrolls = useRef<Record<string, number>>({});
  const shownTab = useRef<Tab>(tab);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || shownTab.current === tab) return;
    shownTab.current = tab;
    el.scrollTop = scrolls.current[tab] ?? 0;
  }, [tab]);
  function pickTab(t: Tab) {
    if (bodyRef.current) scrolls.current[tab] = bodyRef.current.scrollTop;
    setTab(t);
  }

  const robots = status?.robots ?? [];
  const up = status?.service !== "down";
  const personas = overview?.personas ?? [];
  const phones = overview?.phones ?? [];
  const pl: Places = { thisPhone, phones, robots };
  const persona = (id: string | null) => personas.find((p) => p.id === id) ?? null;

  async function startFlash(body: GrowbotFlashRequest): Promise<GrowbotFlashJob | null> {
    const out = await api.startGrowbotFlash(body);
    if (out.job && typeof out.job === "object") {
      setJob(out.job);
      return out.job;
    }
    return loadJob();
  }

  /** Mint a one-time link and open it in a new tab. The tab is opened FIRST, inside the tap,
   *  because a window opened after an await is a popup the phone blocks. */
  function openTicket(what: GrowbotTicketFor, done: string) {
    let tab: Window | null = null;
    try {
      tab = window.open("", "_blank");
    } catch {
      tab = null;
    }
    api
      .growbotTicket(what)
      .then(({ url }) => {
        if (tab && !tab.closed) {
          // `noopener` would cost the handle the tab is steered with, so the link is cut by
          // hand: the vendored page on :8444 must not be able to navigate the PWA.
          tab.opener = null;
          tab.location.href = url;
          say(done);
        } else {
          say("The phone blocked the new tab — tap here to open GrowBot.", { href: url });
        }
        setTimeout(() => {
          if (mounted.current) void loadStatus();
        }, AFTER_LAUNCH_MS);
      })
      .catch((e) => {
        tab?.close();
        say(`GrowBot didn't open — ${message(e)}`);
      });
  }

  /** Open persona `pid` on this phone, alone (`code` null) or wearing robot `code`. */
  function launchHere(pid: string, code: string | null) {
    const p = persona(pid);
    if (!p) return;
    if (code && robots.find((r) => r.code === code)?.revoked_at) {
      say(`${robotName(code, pl)}'s key is revoked — reflash it before a persona can wear it.`);
      return;
    }
    if (!certNoted()) {
      setSheet({ kind: "cert", pid, code });
      return;
    }
    setSheet(null);
    go(p, code);
  }

  function go(p: GrowbotPersona, code: string | null) {
    noteLaunchHere(p.id);
    const here = !!p.phone && p.phone === thisPhone;
    let done: string;
    if (code) {
      const b = robotName(code, pl);
      const off = robots.find((r) => r.code === code && !r.connected)
        ? ` ${b} is off right now — its legs move once it's switched on and reaches the box.`
        : "";
      done =
        here && p.wearing === code
          ? `${p.name} is already wearing ${b} on this phone. Opened it again.`
          : `Opened ${p.name} wearing ${b} in a new tab.${off}`;
    } else {
      done = here
        ? `Opened ${p.name} again in a new tab.`
        : `Opened ${p.name} on this phone, alone — no body.`;
    }
    openTicket(code ? { persona: p.id, code } : { persona: p.id }, done);
  }

  /** Who wears robot `code`, picked from the body's side. */
  async function ride(code: string, pid: string | null) {
    const current = overview ? riderOf(overview, code) : null;
    if ((current?.id ?? null) === pid) return;
    const p = persona(pid);
    if (p && !p.phone) {
      launchHere(p.id, code);
      return;
    }
    try {
      const said = take(await api.setGrowbotRider(code, pid));
      if (!said) say(`Nothing moved — ${robotName(code, pl)} was already that way.`);
    } catch (e) {
      say(`${robotName(code, pl)} didn't change hands — ${message(e)}`);
    }
  }

  async function rekey(r: GrowbotRobot) {
    if (!arm(`rot:${r.code}`)) return;
    setRekeyError("");
    setRekeyLogOpen(false);
    setSheet({ kind: "rekey", code: r.code });
    try {
      await startFlash({ mode: "rekey", target: r.code });
    } catch (e) {
      setRekeyError(message(e));
    }
  }

  async function revoke(r: GrowbotRobot) {
    if (!arm(`rev:${r.code}`)) return;
    const rider = overview ? riderOf(overview, r.code) : null;
    try {
      const out = await api.revokeGrowbotRobot(r.code);
      setStatus((s) =>
        s ? { ...s, robots: s.robots.map((x) => (x.code === out.code ? out : x)) } : s,
      );
      robotsRef.current = robotsRef.current.map((x) => (x.code === out.code ? out : x));
      say(
        `${r.name || r.code}'s key is revoked. It disconnects now and can't come back until it is reflashed.${
          rider?.phone ? ` ${rider.name} carries on alone ${onPhone(rider.phone, pl)}.` : ""
        }`,
      );
    } catch (e) {
      say(`The key wasn't revoked — ${message(e)}`);
    }
  }

  async function signOutPhones() {
    if (!arm("signout")) return;
    try {
      await api.growbotSignOutPhones();
      say(
        "Every phone is signed out of GrowBot. Each persona is kept on the box — open one again from here.",
      );
    } catch (e) {
      say(`Couldn't sign the phones out — ${message(e)}`);
    }
  }

  function watch() {
    if (!job) return;
    if (job.mode === "rekey" && job.code) setSheet({ kind: "rekey", code: job.code });
    else setSheet({ kind: "add", target: "new" });
  }

  /** A persona made, duplicated or imported opens on its own page. */
  function opened(id: string) {
    setSheet(null);
    setPage(id);
    void loadStatus();
  }

  const sheetRobot =
    sheet?.kind === "rekey" ? robots.find((r) => r.code === sheet.code) : undefined;
  const revoked = robots.filter((r) => r.revoked_at);
  const pagePersona = persona(page);

  const banners = (
    <>
      {status?.service === "down" && (
        <output className="gb-banner">
          growbot is down — no robot can connect. Ops → Services has its log and Restart.
        </output>
      )}
      {revoked.map((r) => (
        <output key={r.code} className="gb-banner">
          {r.name || r.code}'s key is revoked — no persona can wear it until it's reflashed over
          USB.
        </output>
      ))}
      {banner && (
        <output key={banner.k} className="gb-banner violet">
          <span className="gb-bt">{banner.text}</span>
          <button type="button" className="gb-bx" onClick={() => setBanner(null)}>
            OK
          </button>
        </output>
      )}
    </>
  );

  function personasTab() {
    return (
      <>
        {overview === null && status !== null && (
          <p className="gb-msg info">This box hasn't got personas yet — run Ops → Update.</p>
        )}
        {overview !== null && (
          <div className="gb-list">
            {personas.length === 0 && (
              <div className="gb-li static">
                <span className="gb-tx">
                  <span className="gb-t1">No personas</span>
                  <span className="gb-t2">Make one below.</span>
                </span>
              </div>
            )}
            {personas.map((p) => (
              <div key={p.id} className="gb-prow">
                <button
                  type="button"
                  className="gb-prow-main"
                  aria-label={`${p.name} — soul, memories and checkpoints`}
                  onClick={() => setPage(p.id)}
                >
                  <GrowbotAvatar p={p} pl={pl} />
                  <span className="gb-tx">
                    <span className="gb-t1">{p.name}</span>
                    <GrowbotWhere p={p} pl={pl} />
                  </span>
                </button>
                <button
                  type="button"
                  className="gb-btn primary"
                  aria-label={`Open ${p.name}`}
                  onClick={() => setSheet({ kind: "launch", pid: p.id })}
                >
                  Open
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="gb-hint dim">
          Tap a persona for its soul, memories and checkpoints. Open asks where it goes.
        </p>
      </>
    );
  }

  function bodyCard(r: GrowbotRobot) {
    const s = bodyState(r, up);
    const name = r.name || r.code;
    const isRevoked = !!r.revoked_at;
    const rider = overview ? riderOf(overview, r.code) : null;
    return (
      <section key={r.code} className="gb-card" aria-label={name}>
        <div className="gb-card-head">
          <h3>{name}</h3>
          <span className="gb-state">
            <span className={`gb-dot ${s.level}`} aria-hidden="true" />
            <span className={`gb-${s.level}-t`}>{s.text}</span>
          </span>
        </div>
        {overview && (
          <p className="gb-hint">
            {rider ? (
              <>
                <strong>{rider.name}</strong> is wearing it
                {rider.phone ? `, ${onPhone(rider.phone, pl)}` : ""}.
              </>
            ) : (
              `Nobody is wearing ${name} — it stands still until a persona connects.`
            )}
          </p>
        )}
        <dl className="gb-facts">
          <dt>Pairing code</dt>
          <dd className="gb-mono">{r.code}</dd>
          <dt>Device key</dt>
          <dd>
            {isRevoked ? (
              <span className="gb-bad-t">revoked</span>
            ) : (
              `made ${day(keyMadeAt(r))}, only on ${name}`
            )}
          </dd>
        </dl>
        {isRevoked ? (
          <div className="gb-acts">
            <button
              type="button"
              className="gb-btn primary block"
              onClick={() => setSheet({ kind: "add", target: r.code })}
            >
              Reflash {name}
            </button>
          </div>
        ) : (
          <div className="gb-acts two">
            <button
              type="button"
              className={`gb-btn${isArmed(`rot:${r.code}`) ? " armed" : ""}`}
              onClick={() => void rekey(r)}
              disabled={running}
            >
              {isArmed(`rot:${r.code}`) ? "Tap again for a new key" : "New key"}
            </button>
            <button
              type="button"
              className={`gb-btn danger${isArmed(`rev:${r.code}`) ? " armed" : ""}`}
              onClick={() => void revoke(r)}
            >
              {isArmed(`rev:${r.code}`) ? "Tap again to revoke" : "Revoke key"}
            </button>
          </div>
        )}
      </section>
    );
  }

  function riderPick(r: GrowbotRobot) {
    if (r.revoked_at || !overview) return null;
    const name = r.name || r.code;
    const rider = riderOf(overview, r.code)?.id ?? null;
    return (
      <section key={`ride-${r.code}`} className="gb-card">
        <fieldset className="gb-choices gb-rider-pick">
          <legend>Who wears {name}</legend>
          <label className={`gb-choice${rider === null ? " sel" : ""}`}>
            <input
              type="radio"
              name={`gb-ride-${r.code}`}
              checked={rider === null}
              onChange={() => void ride(r.code, null)}
            />
            <span className="gb-tx">
              Nobody
              <em>{name} stands still.</em>
            </span>
          </label>
          {personas.map((p) => (
            <label key={p.id} className={`gb-choice${rider === p.id ? " sel" : ""}`}>
              <input
                type="radio"
                name={`gb-ride-${r.code}`}
                checked={rider === p.id}
                onChange={() => void ride(r.code, p.id)}
              />
              <GrowbotAvatar p={p} pl={pl} small />
              <span className="gb-tx">
                {p.name}
                <em>{p.phone ? onPhone(p.phone, pl) : "not open — opens on this phone"}</em>
              </span>
            </label>
          ))}
        </fieldset>
        <p className="gb-hint dim">The one it leaves carries on alone on its phone.</p>
      </section>
    );
  }

  function bodyTab() {
    if (status === null) return null;
    const flashing = running && (
      <button type="button" className="gb-btn primary block" onClick={watch}>
        Flashing — tap to watch
      </button>
    );
    if (robots.length === 0) {
      return (
        <>
          <section className="gb-card">
            <h3>No robots yet</h3>
            <p className="gb-hint">Plug the robot's Pico into the box's USB, then add it.</p>
          </section>
          {flashing || (
            <button
              type="button"
              className="gb-btn primary block"
              onClick={() => setSheet({ kind: "add", target: "new" })}
            >
              Add a robot
            </button>
          )}
        </>
      );
    }
    return (
      <>
        {robots.map((r) => (
          <div key={r.code} className="gb-stack">
            {bodyCard(r)}
            {riderPick(r)}
          </div>
        ))}
        {flashing || (
          <button
            type="button"
            className="gb-btn block"
            onClick={() => setSheet({ kind: "add", target: "new" })}
          >
            Add a robot
          </button>
        )}
      </>
    );
  }

  function phonesTab() {
    return (
      <>
        <div className="gb-list">
          {phones.length === 0 && (
            <div className="gb-li static">
              <span className="gb-tx">
                <span className="gb-t1">No phone has opened GrowBot yet</span>
                <span className="gb-t2">Open a persona and its phone shows here.</span>
              </span>
            </div>
          )}
          {phones.map((f) => {
            const p = personas.find((x) => x.phone === f.id);
            const worn = p?.wearing ? robotName(p.wearing, pl) : null;
            return (
              <div key={f.id} className="gb-ph-row">
                <span className="gb-ph-ic" aria-hidden="true" />
                <div className="gb-tx">
                  <span className="gb-t1">{phoneName(f.id, pl, true)}</span>
                  <span className="gb-t2">
                    {p ? `${p.name} · ${worn ? `wearing ${worn}` : "alone"}` : "nothing open"}
                    {` · seen ${ago(f.last_seen_at)}`}
                  </span>
                </div>
                {worn && <span className="gb-tag violet">{worn}</span>}
              </div>
            );
          })}
        </div>
        <p className="gb-hint dim">
          A phone holds one persona at a time, and a persona is open on one phone at a time —
          opening it elsewhere hands it over.
        </p>
        <button
          type="button"
          className="gb-btn primary block"
          disabled={personas.length === 0}
          onClick={() => setSheet({ kind: "other", pid: null, code: null })}
        >
          Set up another phone
        </button>
        {personas.length === 0 && overview && (
          <p className="gb-hint dim">Make a persona first: another phone opens as one.</p>
        )}
        <details className="gb-help" open>
          <summary>A phone won't open GrowBot?</summary>
          <div className="gb-help-b">
            <p className="gb-hint dim">{NEARBY}</p>
            <p className="gb-hint dim">
              A warning page instead of the creature means the phone doesn't trust the box yet.
              Install the box's certificate once and it's gone, for every page on the box.
            </p>
            {/* The first-time sheet shows once per phone; a phone that skipped the install, or
                a second phone, still needs the file, and this is the only way off the box. */}
            <a className="gb-btn block" href={CERT_URL} download="jbrain-box-root.crt">
              Download the box's certificate
            </a>
          </div>
        </details>
        {/* A phone stays signed in for 30 days so the robot's installed app keeps running;
            this is how a lost one is cut off. Armed, since every phone has to reopen after. */}
        <button
          type="button"
          className={`gb-btn block danger${isArmed("signout") ? " armed" : ""}`}
          onClick={() => void signOutPhones()}
        >
          {isArmed("signout") ? "Tap again to sign every phone out" : "Sign out every phone"}
        </button>
      </>
    );
  }

  const launchFor = sheet?.kind === "launch" ? persona(sheet.pid) : null;
  const certFor = sheet?.kind === "cert" ? persona(sheet.pid) : null;

  return (
    <>
      {banners}
      {pagePersona ? (
        <GrowbotPersonaPage
          key={pagePersona.id}
          persona={pagePersona}
          pl={pl}
          say={say}
          onBack={() => setPage(null)}
          onOpen={() => setSheet({ kind: "launch", pid: pagePersona.id })}
          onChanged={() => void loadStatus()}
          onGone={() => {
            setPage(null);
            void loadStatus();
          }}
          onDuplicated={opened}
          onSeed={(cid) => setSheet({ kind: "new", seed: cid })}
          onImport={() => setSheet({ kind: "import", over: pagePersona.id })}
        />
      ) : (
        <>
          <div className="gb-seg" role="tablist" aria-label="GrowBot">
            {(
              [
                ["personas", "Personas"],
                ["body", "Body"],
                ["phones", "Phones"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={tab === id ? "on" : ""}
                onClick={() => pickTab(id)}
              >
                {label}
                {id === "personas" && overview && <span className="gb-n">{personas.length}</span>}
              </button>
            ))}
          </div>
          <main className="screen-body gb-screen" ref={bodyRef}>
            {error && (
              <p className="gb-msg bad" role="alert">
                {error}
              </p>
            )}
            {status === null && !error && <p className="gb-hint dim">Loading GrowBot…</p>}
            {status !== null && tab === "personas" && personasTab()}
            {tab === "body" && bodyTab()}
            {status !== null && tab === "phones" && phonesTab()}
          </main>
          {tab === "personas" && overview && (
            <div className="gb-foot">
              <button
                type="button"
                className="gb-btn primary"
                onClick={() => setSheet({ kind: "new", seed: null })}
              >
                New persona
              </button>
              <button
                type="button"
                className="gb-btn"
                onClick={() => setSheet({ kind: "import", over: null })}
              >
                Import
              </button>
            </div>
          )}
        </>
      )}

      {sheet?.kind === "launch" && launchFor && (
        <GrowbotLaunchSheet
          p={launchFor}
          personas={personas}
          pl={pl}
          up={up}
          onHere={(code) => launchHere(launchFor.id, code)}
          onOther={() => setSheet({ kind: "other", pid: launchFor.id, code: null })}
          onClose={() => setSheet(null)}
        />
      )}

      {sheet?.kind === "other" && (
        <GrowbotPhoneSheet
          personas={personas}
          pl={pl}
          initialPersona={
            sheet.pid ?? personas.find((p) => thisPhone && p.phone === thisPhone)?.id ?? null
          }
          say={say}
          onClose={() => setSheet(null)}
        />
      )}

      {sheet?.kind === "new" && (
        <GrowbotNewPersonaSheet
          personas={personas}
          initialSeed={sheet.seed}
          say={say}
          onMade={opened}
          onImport={() => setSheet({ kind: "import", over: null })}
          onClose={() => setSheet(null)}
        />
      )}

      {sheet?.kind === "import" && (
        <GrowbotImportSheet
          over={persona(sheet.over)}
          say={say}
          onMade={opened}
          onOver={() => {
            setSheet(null);
            void loadStatus();
          }}
          onClose={() => setSheet(null)}
        />
      )}

      {sheet?.kind === "add" && (
        <GrowbotAddSheet
          robots={robots}
          initialTarget={sheet.target}
          job={job}
          onStart={startFlash}
          onOpen={(code) => {
            // A paired robot is worn from the Body tab, where every persona says where it is.
            setSheet(null);
            setPage(null);
            pickTab("body");
            void loadStatus();
            say(`Pick who wears ${robotName(code, pl)} under Who wears ${robotName(code, pl)}.`);
          }}
          onClose={() => setSheet(null)}
        />
      )}

      {sheet?.kind === "rekey" && (
        <Sheet title={`New key — ${sheetRobot?.name || sheet.code}`} onClose={() => setSheet(null)}>
          <p className="gb-hint">
            The box makes a new key and writes it into {sheetRobot?.name || sheet.code} over USB, so
            it has to be on the box's USB now. The old key stops working the moment this finishes.
          </p>
          {rekeyError && (
            <p className="gb-msg bad" role="alert">
              {rekeyError}
            </p>
          )}
          {job && job.mode === "rekey" && job.code === sheet.code && !rekeyError && (
            <>
              <GrowbotPhases job={job} />
              <GrowbotFail job={job} />
              {job.state === "ok" && (
                <p className="gb-msg ok">
                  Done — {sheetRobot?.name || sheet.code} is back with its new key.
                </p>
              )}
              <button
                type="button"
                className="gb-linkbtn"
                onClick={() => setRekeyLogOpen((o) => !o)}
              >
                {rekeyLogOpen || job.state === "fail" ? "Hide the log" : "Show the log"}
              </button>
              {(rekeyLogOpen || job.state === "fail") && <GrowbotLog lines={job.log} />}
            </>
          )}
          <div className="gb-sheet-foot">
            {running && !rekeyError ? (
              <button type="button" className="gb-btn block" disabled>
                Writing the new key…
              </button>
            ) : (
              <button type="button" className="gb-btn block" onClick={() => setSheet(null)}>
                Done
              </button>
            )}
          </div>
        </Sheet>
      )}

      {sheet?.kind === "cert" && certFor && (
        <Sheet title="First time on this phone" onClose={() => setSheet(null)}>
          <p className="gb-hint">
            GrowBot opens on the box's own address,{" "}
            <span className="gb-mono">jbrain.local:8444</span>, with the box's certificate. A phone
            that hasn't been told to trust the box shows a warning page first.
          </p>
          <p className="gb-hint">
            Install the box's certificate once and the warning is gone — for GrowBot and every other
            page on the box. The phone needs it for the camera and microphone.
          </p>
          <p className="gb-hint dim">
            After downloading: on iPhone, open Settings → Profile Downloaded, then General → About →
            Certificate Trust Settings. On Android, Settings → Security → Install a certificate → CA
            certificate.
          </p>
          <p className="gb-hint dim">{NEARBY}</p>
          <div className="gb-sheet-foot">
            <a className="gb-btn block" href={CERT_URL} download="jbrain-box-root.crt">
              Download the box's certificate
            </a>
            <button
              type="button"
              className="gb-btn primary block"
              onClick={() => {
                noteCert();
                const code = sheet.code;
                setSheet(null);
                go(certFor, code);
              }}
            >
              {sheet.code
                ? `Open ${certFor.name} wearing ${robotName(sheet.code, pl)}`
                : `Open ${certFor.name}`}
            </button>
          </div>
        </Sheet>
      )}

      {toast && (
        // Over an open sheet the toast moves to the top, so it never covers the sheet's buttons.
        <output key={toast.k} className={`gb-toast${sheet ? " top" : ""}`}>
          {toast.href ? (
            <a href={toast.href} target="_blank" rel="noreferrer">
              {toast.text}
            </a>
          ) : (
            <span className="gb-tt">{toast.text}</span>
          )}
          {toast.undo && (
            <button
              type="button"
              className="gb-ta"
              onClick={() => {
                const undo = toast.undo;
                setToast(null);
                undo?.();
              }}
            >
              {toast.label ?? "Undo"}
            </button>
          )}
        </output>
      )}
    </>
  );
}
