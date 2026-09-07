// The Radios tab: what each radio is doing right now, and what to make it do instead.
//
// Binding spec: docs/mocks/sdr-launcher/shapes.html **shape A**, chosen 2026-09-04 —
// the RADIO is the object. A roster of cards; tapping one opens its control layer,
// where its job is chosen. Naming a radio and saying what it is plugged into stays in
// Settings → Radios; this screen is what each one is *doing*.
//
// **This supersedes round 3's APRS-switch placement** (docs/mocks/aprs/c-single-dongle),
// which put the switch in the APRS tab and rejected a radio-wide job selector. That was
// decided on a one-dongle box, where "which radio" was not a question. It is now. What
// moved is ONLY the switch: the APRS tab keeps its log, its roster and its command
// tasks, and the two must remain **one state, never two switches**
// (docs/plans/APRS_CONTROL_PLAN.md).
//
// A tap on a radio is honoured or refused BY NAME — `roles.named` server-side, and the
// disabled reasons here are a reading of the same rule so a button says why before it
// is pressed rather than after a 409.

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { type AprsLogState, receiverHealth } from "../aprsLog";
import { ago } from "../aprsStations";
import type { BandSection, SpectrumRange } from "../sdrBands";
import { JOBS, jobAllowed, jobLabel, jobOf, sessionOn, stateLine } from "../sdrJobs";
import { type SdrRadio, type SdrRadios, labelFor, roleLabel } from "../sdrRadios";
import { type SdrListening, useSdrSession } from "../sdrSession";
import { SdrBandSheet } from "./SdrBandSheet";
import { SdrSpectrumJob } from "./SdrSpectrumJob";
import { SdrTunerControls } from "./SdrTunerControls";

/** How many packets the job surface peeks at when nothing hands it a log. Two, because
 *  it draws ONE and the second only guards against an empty first page. */
const APRS_PEEK = 2;

/** How often that peek repeats. Far slower than the Radio screen's own poll: this is a
 *  "still working?" line behind the composer, not a log anyone is reading. */
const APRS_PEEK_MS = 20_000;

/** How much of a packet's payload the one-line summary shows. The rest is on the log
 *  screen; a position report is mostly coordinates and reads as noise at this size. */
const APRS_INFO_CHARS = 48;

export function SdrRadiosTab({
  /** The screen's poll counter. The roster follows it rather than owning a second
   *  interval, so what a radio is described as and what it is doing can never be read
   *  at two different moments. */
  tick,
  log,
  onOpenAprs,
}: {
  tick: number;
  log: AprsLogState | null;
  onOpenAprs: () => void;
}) {
  const sdr = useSdrSession();
  const [radios, setRadios] = useState<SdrRadios | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRadios(await api.getSdrRadios());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't read the radios.");
    }
  }, []);

  // `tick` is the screen's poll counter, and depending on it IS the refresh — the same
  // arrangement AprsStations uses, so the roster and the health line above it can never
  // be reading the box at two different moments.
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick is the poll signal
  useEffect(() => {
    void refresh();
  }, [refresh, tick]);

  if (error && !radios) {
    return (
      <p className="radio-error" role="alert">
        {error}
      </p>
    );
  }
  if (!radios) return <p className="radio-empty">Looking for radios…</p>;

  const chosen = open ? radios.radios.find((r) => r.serial === open) : undefined;
  if (chosen) {
    return (
      <RadioDetail
        radio={chosen}
        radios={radios}
        log={log}
        onBack={() => setOpen(null)}
        onChanged={() => void refresh()}
        onOpenAprs={onOpenAprs}
      />
    );
  }

  return (
    <>
      {!radios.scan_ok && (
        // "We cannot tell" is not "no". Read literally, every radio arrives
        // `attached: false` — and saying so under a banner admitting we cannot see is
        // the mistake `sdrRadios.outcomeFor` had to be corrected for.
        <div className="aprs-held" role="alert">
          The USB scan could not be reached, so whether each radio is attached is <b>unknown</b>{" "}
          rather than no.
        </div>
      )}
      {radios.radios.length === 0 ? (
        <p className="radio-empty">No radio on this box. Plug one in and it appears here.</p>
      ) : (
        radios.radios.map((radio) => (
          <RadioCard
            key={radio.serial}
            radio={radio}
            session={sessionOn(sdr, radio.serial)}
            scanOk={radios.scan_ok}
            onOpen={() => setOpen(radio.serial)}
          />
        ))
      )}
      <p className="radio-hint">
        Naming a radio and saying what it is plugged into lives in Settings → Radios. This screen is
        what each one is <b>doing right now</b>.
      </p>
    </>
  );
}

function RadioCard({
  radio,
  session,
  scanOk,
  onOpen,
}: {
  radio: SdrRadio;
  session: SdrListening | null;
  scanOk: boolean;
  onOpen: () => void;
}) {
  const line = stateLine(radio, session, scanOk);
  return (
    <button type="button" className="rcard" onClick={onOpen}>
      <span className="rcard-top">
        <span className="rname">{labelFor(radio)}</span>
        <span className="rser">{radio.serial}</span>
      </span>
      {radio.description && <span className="rdesc">{radio.description}</span>}
      <span className="rstate">
        <span className={`dot ${line.tone}`} aria-hidden="true" />
        <span>{line.text}</span>
        <span className="bcaret">›</span>
      </span>
    </button>
  );
}

function RadioDetail({
  radio,
  radios,
  log,
  onBack,
  onChanged,
  onOpenAprs,
}: {
  radio: SdrRadio;
  radios: SdrRadios;
  log: AprsLogState | null;
  onBack: () => void;
  onChanged: () => void;
  onOpenAprs: () => void;
}) {
  // Only what RESETTING a radio needs. Everything about its job — the release-then-take,
  // the confirm, the two-step arming — moved to `RadioJob`, which the omnibox sheet
  // shares (`docs/mocks/omnibox-radios/README.md`).
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(what: () => Promise<unknown>, whenItFails: string) {
    setBusy(true);
    try {
      await what();
      setError(null);
      onChanged();
    } catch (err) {
      // The api's own sentence: it names the radio, the job holding it, or why this
      // radio may not have this one. All three are things only the owner can act on.
      setError(err instanceof ApiError ? err.message : whenItFails);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="rdetail-top">
        <button type="button" className="radio-back" onClick={onBack} aria-label="Back">
          ‹
        </button>
        <h2 className="rdetail-title">{labelFor(radio)}</h2>
      </div>
      {radio.description && <p className="rdesc">{radio.description}</p>}
      <div className="rstate">
        <span className="rser">{radio.serial}</span>
        <span className="rused">{roleLabel(radio.role)}</span>
      </div>

      <RadioJob
        radio={radio}
        radios={radios}
        log={log}
        onChanged={onChanged}
        onOpenAprs={onOpenAprs}
      />

      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}
      <ResetRadio radio={radio} busy={busy} onRun={run} />
    </>
  );
}

/**
 * What one radio is doing, and how to change it — the whole control layer, with none of
 * the chrome around it.
 *
 * Shared by the Radios tab and the omnibox sheet (binding spec
 * `docs/mocks/omnibox-radios/d-radio-then-task.html`), because the risky part of this
 * screen is not its layout: it is the release-then-take, the confirm before a running
 * job is stopped, and the two-step arming for a job that needs a band. A second copy of
 * that for the sheet would be a second place for those to drift, and the one that drifts
 * silently takes the owner's APRS log with it.
 */
export function RadioJob({
  radio,
  radios,
  log,
  onChanged,
  onOpenAprs,
}: {
  radio: SdrRadio;
  radios: SdrRadios;
  log: AprsLogState | null;
  onChanged: () => void;
  onOpenAprs: () => void;
}) {
  const sdr = useSdrSession();
  const session = sessionOn(sdr, radio.serial);
  const job = session ? jobOf(session) : "idle";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [arming, setArming] = useState<"listen" | "spectrum" | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);

  async function run(what: () => Promise<unknown>, whenItFails: string) {
    setBusy(true);
    setConfirm(null);
    try {
      await what();
      setError(null);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : whenItFails);
    } finally {
      setBusy(false);
    }
  }

  async function free(): Promise<void> {
    if (session) await api.sdrStop(session.session_id);
  }

  function choose(next: string) {
    if (next === job) return;
    if (session && confirm !== next) {
      setConfirm(next);
      return;
    }
    if (next === "idle") {
      void run(free, "Couldn't release the radio.");
      return;
    }
    if (next === "aprs") {
      void run(async () => {
        await free();
        await api.setAprsLogging(true, undefined, radio.serial);
      }, "Couldn't start APRS logging.");
      return;
    }
    setConfirm(null);
    setArming(next as "listen" | "spectrum");
  }

  function picked(range: SpectrumRange, section: BandSection | null) {
    const want = arming;
    setArming(null);
    if (want === "spectrum") {
      void run(async () => {
        await free();
        await api.sdrSpectrumStart(range, radio.serial);
      }, "Couldn't start the spectrum.");
      return;
    }
    const hz = range.section && section ? section.centre_hz : (range.startMhz ?? 0) * 1_000_000;
    const mode = section?.mode ?? "wbfm";
    void run(async () => {
      await free();
      await api.sdrListen(hz / 1_000_000, mode, radio.serial);
    }, "Couldn't start listening.");
  }

  return (
    <>
      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}

      <fieldset className="jobs">
        <legend className="lbl">Doing</legend>
        {JOBS.map(({ id, label }) => {
          const why = jobAllowed(radios, sdr, radio, id);
          return (
            <button
              key={id}
              type="button"
              aria-pressed={job === id}
              disabled={busy || (why !== null && job !== id)}
              title={why ?? undefined}
              onClick={() => choose(id)}
            >
              {confirm === id ? "Again?" : label}
            </button>
          );
        })}
      </fieldset>
      {confirm && session && (
        // Names the job being STOPPED, not the state line: reading that line back
        // produces "that stops not attached." on the awkward cases, and what the owner
        // needs to weigh is what they are about to lose.
        <p className="why" role="alert">
          That stops {jobLabel(job).toLowerCase()} on this radio. Tap again to confirm.
        </p>
      )}
      <BlockedJobs radios={radios} radio={radio} job={job} />

      <div className="jobsurface">
        {!radio.attached && radios.scan_ok ? (
          <div className="aprs-held" role="alert">
            <b>Not attached.</b>{" "}
            {radio.role === "general"
              ? "Plug it in to use it."
              : `${jobLabel(radio.role)} is dedicated to this radio and will wait for it rather than moving to another one.`}
          </div>
        ) : job === "listen" && session ? (
          // The REAL transport, and the same component the omnibox sheet opens, so the
          // two can never drift apart.
          <SdrTunerControls listening={session} onReleased={onChanged} />
        ) : job === "aprs" ? (
          <AprsJob log={log} onOpenAprs={onOpenAprs} />
        ) : job === "spectrum" ? (
          <SdrSpectrumJob
            serial={radio.serial}
            session={session}
            onChanged={onChanged}
            onListen={(hz, mode) =>
              // The same release-then-take as the job row, and for the same reason: the
              // api names the SERIAL on the way back in, so the re-take asks for THIS
              // radio rather than whichever is free.
              void run(async () => {
                await free();
                await api.sdrListen(hz / 1_000_000, mode, radio.serial);
              }, "Couldn't listen on that signal.")
            }
          />
        ) : (
          <p className="radio-hint">Idle — nothing is holding this radio.</p>
        )}
      </div>

      {arming && <SdrBandSheet purpose={arming} onPick={picked} onClose={() => setArming(null)} />}
    </>
  );
}

/** Re-enumerate the dongle — the software equivalent of unplugging it.
 *
 *  **It is here because the owner has no terminal** (CLAUDE.md #10). An RTL-SDR left
 *  with transfers pending can stay on the bus and stop answering descriptor reads, and
 *  then every lookup by serial fails while the USB scan still lists the device. Nothing
 *  else clears it — not a container restart, not a rebuild, not an update — so before
 *  this the only answer was "go and unplug it", which is no answer when the box is
 *  somewhere else.
 *
 *  Last on the surface and quiet, because it is a repair rather than a control: the
 *  ordinary way to use a radio is the job row above. Arm-then-confirm per DESIGN.md,
 *  since it does interrupt the device. */
function ResetRadio({
  radio,
  busy,
  onRun,
}: {
  radio: SdrRadio;
  busy: boolean;
  onRun: (what: () => Promise<unknown>, whenItFails: string) => void;
}) {
  const [armed, setArmed] = useState(false);
  return (
    <>
      <hr className="hair" />
      <button
        type="button"
        className="radio-reset"
        disabled={busy}
        onClick={() => {
          if (!armed) {
            setArmed(true);
            return;
          }
          setArmed(false);
          onRun(() => api.resetSdrRadio(radio.serial), "Couldn't reset the radio.");
        }}
      >
        {armed ? "Again? This re-enumerates the dongle" : "Reset this radio"}
      </button>
      <p className="radio-hint">
        Re-plugs it in software. For a radio the box can see but cannot open — nothing else clears
        that, not even an update.
      </p>
    </>
  );
}

/** Every job this radio cannot take, and why — under the control, not only in a tooltip.
 *
 *  A disabled button on a phone has no hover, so the `title` is unreachable; without
 *  this the owner sees three greyed-out words and no reason for any of them. */
function BlockedJobs({
  radios,
  radio,
  job,
}: {
  radios: SdrRadios;
  radio: SdrRadio;
  job: string;
}) {
  const sdr = useSdrSession();
  const blocked = JOBS.map(({ id, label }) => {
    const why = id === job ? null : jobAllowed(radios, sdr, radio, id);
    return why ? `${label} — ${why}` : null;
  }).filter((line): line is string => line !== null);
  if (blocked.length === 0) return null;
  return <p className="why">{blocked.join(" · ")}</p>;
}

function AprsJob({ log, onOpenAprs }: { log: AprsLogState | null; onOpenAprs: () => void }) {
  // The log this surface can see. The Radios tab hands one down from the screen's own
  // poll; the omnibox sheet has no poll, and a job surface that can only offer a LINK
  // says nothing about whether the job is working — which is the one thing an owner
  // opens it for. So it fetches a couple of packets itself when nobody supplied any.
  const [own, setOwn] = useState<AprsLogState | null>(null);
  useEffect(() => {
    if (log) return;
    let alive = true;
    const read = () =>
      void api.getAprsPackets(APRS_PEEK).then(
        (next) => alive && setOwn(next),
        () => undefined, // the link below still works; a failed peek is not an error here
      );
    read();
    const timer = window.setInterval(read, APRS_PEEK_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [log]);

  const seen = log ?? own;
  // The health line, not a signal meter: this family already deleted a meter for
  // measuring the wrong thing, and a quiet packet frequency and a dead receiver are
  // indistinguishable without it.
  const health = seen ? receiverHealth(seen) : null;
  const newest = seen?.packets[0] ?? null;
  return (
    <>
      {health && (
        <div className={`aprs-health aprs-health-${health.tone}`}>
          <span className="aprs-dot" aria-hidden="true" />
          <span className="aprs-who">{health.text}</span>
        </div>
      )}
      {newest && (
        // The LAST PACKET, which is the proof the health line can only summarise: a
        // callsign that arrived out of the air a minute ago is what "it is working"
        // looks like. Rendered as TEXT and never as anything else — every field here
        // is a transmission from a stranger (`aprsLog.ts`).
        <div className="aprs-last">
          <span className="aprs-call">{newest.source}</span>
          <span className="aprs-when">{ago(newest.heard_at)} ago</span>
          <span className="aprs-info">{newest.info.slice(0, APRS_INFO_CHARS)}</span>
        </div>
      )}
      <button type="button" className="band" onClick={onOpenAprs}>
        <span className="bband">
          <span className="bt">Open the APRS log</span>
          <span className="bd">Heard stations, packets and command tasks</span>
        </span>
        <span className="bcaret">›</span>
      </button>
    </>
  );
}
