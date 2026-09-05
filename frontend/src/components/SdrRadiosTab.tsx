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
import type { BandSection, SpectrumRange } from "../sdrBands";
import { JOBS, jobAllowed, jobLabel, jobOf, sessionOn, stateLine } from "../sdrJobs";
import { type SdrRadio, type SdrRadios, labelFor, roleLabel } from "../sdrRadios";
import { type SdrListening, useSdrSession } from "../sdrSession";
import { SdrBandSheet } from "./SdrBandSheet";
import { SdrSpectrumJob } from "./SdrSpectrumJob";
import { SdrTunerControls } from "./SdrTunerSheet";

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
  const sdr = useSdrSession();
  const session = sessionOn(sdr, radio.serial);
  const job = session ? jobOf(session) : "idle";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A job that needs a band is chosen in two steps: the button arms it, the sheet
  // picks. Listening needs a frequency and a mode, and a waterfall needs a span —
  // neither is a thing this screen can invent for the owner.
  const [arming, setArming] = useState<"listen" | "spectrum" | null>(null);
  // DESIGN.md: destructive actions get an inline confirm, the button morphing to "Tap
  // again". Every job change on a BUSY radio stops what it is doing, and one of those
  // is an APRS log the owner may have armed on a schedule — the silent loss the
  // sidecar's own `_stop` is written against. Cleared whenever anything else happens,
  // so a stale arm cannot fire on the next tap.
  const [confirm, setConfirm] = useState<string | null>(null);

  async function run(what: () => Promise<unknown>, whenItFails: string) {
    setBusy(true);
    setConfirm(null);
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

  /** Free the radio before giving it another job. The api names the SERIAL on the way
   *  back in, so the re-take asks for THIS radio rather than whichever is free — which
   *  is what stops the window between the two becoming a different antenna. */
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
    // Listening is one frequency. A section gives its centre and its mode; a manual
    // entry gives the frequency itself, and inherits the mode of wherever it landed.
    const hz = range.section && section ? section.centre_hz : (range.startMhz ?? 0) * 1_000_000;
    const mode = section?.mode ?? "wbfm";
    void run(async () => {
      await free();
      await api.sdrListen(hz / 1_000_000, mode, radio.serial);
    }, "Couldn't start listening.");
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

      <ResetRadio radio={radio} busy={busy} onRun={run} />

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
  // The health line, not a signal meter: this family already deleted a meter for
  // measuring the wrong thing, and a quiet packet frequency and a dead receiver are
  // indistinguishable without it.
  const health = log ? receiverHealth(log) : null;
  return (
    <>
      {health && (
        <div className={`aprs-health aprs-health-${health.tone}`}>
          <span className="aprs-dot" aria-hidden="true" />
          <span className="aprs-who">{health.text}</span>
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
