// The Radio launcher (docs/plans/APRS_CONTROL_PLAN.md P3; binding specs
// docs/mocks/aprs/a-launcher-shape.html shape A and c-single-dongle.html shape A).
//
// Tuner / APRS / Recordings, because they are one piece of hardware sharing one lease
// and one mental model. The APRS tab is what this wave builds.
//
// TWO THINGS HERE ARE LOAD-BEARING, not decoration:
//
// 1. "Is the receiver alive" is LAST DECODE and RATE, never a signal bar. This family
//    already deleted a meter for measuring the wrong thing — the tuner's read `peak` on
//    demodulated audio, so an empty channel full of hiss read HIGH. A quiet packet
//    frequency and a dead receiver are indistinguishable in a list of rows, so the
//    health line is the only thing that separates them.
// 2. Arming a command task and enabling logging are TWO SWITCHES. With one dongle,
//    logging means giving up listening, so it will not always be on — and a task that
//    says "armed" while nothing is receiving is the same failure as the meter. The
//    Tuner tab therefore says when APRS holds the radio, and offers the handoff back.
//
// EVERY PACKET IS UNTRUSTED TEXT: transmissions from anyone in range, with a callsign
// that forges trivially. Rows are rendered as quoted content, badged, and nothing here
// is ever put in front of a model as an instruction.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import {
  type AprsCommandState,
  type AprsLogState,
  armedLabel,
  decodeRate,
  receiverHealth,
} from "../aprsLog";
import { AprsStations } from "../components/AprsStations";
import { SdrSpectrumTab } from "../components/SdrSpectrumTab";
import { SdrTunerControls } from "../components/SdrTunerSheet";
import { type SdrListening, sessionFor, useSdrSession } from "../sdrSession";

type Tab = "tuner" | "spectrum" | "aprs" | "recordings";

const POLL_MS = 5000;

const TAB_LABEL: Record<Tab, string> = {
  tuner: "Tuner",
  // Its own tab only until the launcher's chosen shape lands — there a spectrum is one
  // of the jobs a RADIO can be given, not a place of its own (SdrSpectrumTab.tsx).
  spectrum: "Spectrum",
  aprs: "APRS",
  recordings: "Recordings",
};

export function RadioScreen({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("aprs");
  const [log, setLog] = useState<AprsLogState | null>(null);
  const [commands, setCommands] = useState<AprsCommandState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // One timer for the whole tab. The roster follows this counter rather than owning a
  // second interval, so the station list and the health line above it can never be
  // reading the channel at two different moments.
  const [tick, setTick] = useState(0);
  const [owner, setOwner] = useState<string | null>(null);
  const sdr = useSdrSession();
  // ONE reading of who holds a radio, from the 1 s lease poll — not the 5 s log poll.
  // They are the same sidecar fact arriving by two routes at two cadences, and mixing
  // them let the Tuner tab say "in use by APRS" for five seconds after the lease was
  // gone. The shared session store exists precisely so those two can never disagree.
  //
  // Asked PER JOB rather than off `listening`. `listening` is the one session the
  // omnibox draws and it prefers the tuner, so with APRS on one dongle and the tuner on
  // another it reported "listen" — and this tab then put up the one-dongle contention
  // panel, replaced "Stop APRS logging" with a button that could only no-op, and
  // printed the TUNER's elapsed time as how long APRS had held the radio.
  const logging = sessionFor(sdr, "aprs");
  const listening = sessionFor(sdr, "listen");

  // A poll in flight when the next tick fires, or when a toggle finishes, can land out
  // of order and paint a state the box has already left. The sequence number makes a
  // stale answer discardable rather than merely unlikely.
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const [next, armed] = await Promise.all([
        api.getAprsPackets(),
        // A box with no commands answers with empty lists, so this never fails on its
        // own; failing together with the log keeps the tab's two halves consistent.
        api.getAprsCommands(),
      ]);
      if (mine !== seq.current) return;
      setLog(next);
      setCommands(armed);
      setTick((t) => t + 1);
      setError(null);
    } catch (err) {
      if (mine !== seq.current) return;
      setError(err instanceof ApiError ? err.message : "Couldn't read the APRS log.");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // The owner's callsign, so their own stations pin to the top of the roster. It lives
  // in app Settings rather than on this screen: it is the operator's identity, not a
  // property of the radio. Read once — it does not change while the tab is open — and a
  // failure is silent, because not knowing which station is his costs a pin, not a list.
  useEffect(() => {
    void (async () => {
      try {
        setOwner((await api.getSettings()).owner_callsign);
      } catch {
        setOwner(null);
      }
    })();
  }, []);

  async function toggle(enabled: boolean) {
    setBusy(true);
    try {
      await api.setAprsLogging(enabled);
      await refresh();
      setError(null);
    } catch (err) {
      // The 409 names which job holds the radio, and the two need opposite answers.
      setError(err instanceof ApiError ? err.message : "Couldn't change APRS logging.");
    } finally {
      setBusy(false);
    }
  }

  return (
    // `subscreen` is what makes this a full-screen layer OVER the launcher. The
    // launcher deliberately stays open beneath a card — dismissing the card reveals it
    // again — so a screen that is merely a padded section lets the tiles show through,
    // which is what this one did.
    <section className="subscreen radio-screen">
      <div className="radio-top">
        <button type="button" className="radio-back" onClick={onClose} aria-label="Back">
          ‹
        </button>
        <h1 className="radio-title">Radio</h1>
      </div>
      <div className="radio-body">
        {/* The house segmented control — the same one the session list uses for
            Today / Older / Archived. This screen had invented an underline tab bar
            instead, which is a second answer to a question the design system had
            already settled. */}
        <div className="seg-tabs" role="tablist" aria-label="Radio">
          {(["tuner", "spectrum", "aprs", "recordings"] as Tab[]).map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={`seg-tab${tab === id ? " on" : ""}`}
              onClick={() => setTab(id)}
            >
              {TAB_LABEL[id]}
            </button>
          ))}
        </div>

        {tab === "aprs" && (
          <AprsTab
            tick={tick}
            owner={owner}
            log={log}
            commands={commands}
            error={error}
            busy={busy}
            listening={listening !== null}
            heldFor={logging?.elapsed_s ?? null}
            onToggle={toggle}
          />
        )}
        {tab === "tuner" && (
          <TunerTab
            logging={logging}
            listening={listening}
            busy={busy}
            onFree={() => toggle(false)}
            // Releasing from here leaves the tab on its idle state; the lease poll
            // publishes the change, so nothing local needs clearing.
            onReleased={() => void refresh()}
          />
        )}
        {tab === "spectrum" && <SdrSpectrumTab />}
        {tab === "recordings" && (
          <p className="radio-empty">Recordings arrive in a later wave. Nothing is stored yet.</p>
        )}
      </div>
    </section>
  );
}

function AprsTab({
  log,
  commands,
  error,
  busy,
  listening,
  heldFor,
  tick,
  owner,
  onToggle,
}: {
  log: AprsLogState | null;
  commands: AprsCommandState | null;
  error: string | null;
  busy: boolean;
  /** Whether a session is holding a radio to LISTEN — asked per job rather than off
   *  `listening`, which is whichever session the omnibox should draw. */
  listening: boolean;
  /** Seconds the APRS session has held ITS radio. Was the tuner's elapsed time, which
   *  on a two-dongle box is a different session on a different dongle. */
  heldFor: number | null;
  /** The tab's poll counter — the roster refreshes with the health line, not apart. */
  tick: number;
  /** The owner's callsign from Settings, for pinning their own stations. */
  owner: string | null;
  onToggle: (enabled: boolean) => void;
}) {
  // The error has to come BEFORE the loading return. It used to sit after it, so a
  // first load that failed left this tab on "Reading the log…" for ever with the
  // message swallowed — and that is the DEFAULT experience on a box with no radio,
  // because the launcher offers the Radio tile unconditionally.
  if (!log) {
    return error ? (
      <p className="radio-error" role="alert">
        {error}
      </p>
    ) : (
      <p className="radio-empty">Reading the log…</p>
    );
  }
  const health = receiverHealth(log);
  const freq = log.frequency_hz ? (log.frequency_hz / 1_000_000).toFixed(3) : null;

  return (
    <>
      <div className={`aprs-health aprs-health-${health.tone}`}>
        <span className="aprs-dot" aria-hidden="true" />
        <span className="aprs-who">
          {freq ? <b>{freq}</b> : <b>APRS</b>} · {health.text}
        </span>
        <span className="aprs-rate">{decodeRate(log)}</span>
      </div>

      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}

      {log.logging ? (
        // LOGGING wins over contention, and must: with a radio each, a listening session
        // exists at the same time as this one. Testing the listening session first left
        // the owner looking at the contention panel while logging ran — "Stop APRS
        // logging" replaced by a button whose only effect was a no-op 200.
        <>
          <button
            type="button"
            className="aprs-toggle aprs-toggle-on"
            disabled={busy}
            onClick={() => onToggle(false)}
          >
            Stop APRS logging
          </button>
          {heldFor !== null && (
            <p className="radio-hint">
              Holding a radio for {held(heldFor)}. Nothing else can use THAT radio until this is
              released.
            </p>
          )}
        </>
      ) : listening ? (
        // CONTENTION (docs/mocks/aprs/c-single-dongle.html, shape A). Reached only when
        // nothing is logging: a listening session may be the reason APRS cannot start,
        // and on a one-dongle box it always is. Say what holds a radio BEFORE the tap
        // rather than after a raw lowercase 409. One CTA, because round 3's own review
        // killed a duplicate here. The button is no longer a guess on a two-dongle box:
        // if another radio is free the api takes it and this simply succeeds.
        <div className="aprs-held" role="alert">
          <b>The radio is listening.</b> Logging will take a free radio if this box has one —
          otherwise release the listening session, or add a second dongle to do both.
          <button
            type="button"
            className="aprs-take"
            disabled={busy}
            onClick={() => onToggle(true)}
          >
            Release &amp; log APRS
          </button>
        </div>
      ) : (
        <>
          <button
            type="button"
            className="aprs-toggle"
            disabled={busy}
            onClick={() => onToggle(true)}
          >
            Enable APRS logging
          </button>
          <p className="radio-hint">
            Reserves the tuner until released — while it runs, the Tuner tab can't listen.
          </p>
        </>
      )}

      <CommandSummary commands={commands} logging={log.logging} />

      {/* The roster replaces the flat feed this tab shipped with. On the owner's own
          capture that feed showed 6 callsigns for 16 transmitting stations, because
          three quarters of the channel was one IGate relaying internet traffic under
          its own name — a list of frames cannot show that, and a list of stations
          keyed on the true sender shows nothing else. */}
      {log.packets.length === 0 && !log.logging ? (
        <p className="radio-empty">
          Nothing logged. Turn APRS logging on to start hearing the channel.
        </p>
      ) : (
        <AprsStations tick={tick} owner={owner} />
      )}
    </>
  );
}

/** What is armed, and what has been tried against it (the mock's "Automations · radio"
 * and c-single-dongle's "armed but deaf" block — which round 3's own review called the
 * thing most likely to be missed).
 *
 * Read-only on purpose. Editing lives in Tasks, and the point of showing commands HERE
 * is the pairing: arming a command and enabling the receiver are two switches, so a
 * task that says "armed" while nothing is receiving is the same lie a signal meter on a
 * dead channel tells. This is where those two facts finally sit next to each other. */
function CommandSummary({
  commands,
  logging,
}: {
  commands: AprsCommandState | null;
  logging: boolean;
}) {
  if (!commands || commands.commands.length === 0) return null;
  const armed = commands.commands.filter((c) => c.enabled);

  return (
    <>
      <div className="aprs-sec">Command tasks</div>
      {!logging && armed.length > 0 && (
        <div className="aprs-deaf" role="alert">
          <b>Armed, but nothing is receiving.</b> These fire on a verified command, and APRS logging
          is off — so no command can arrive. Arming a task and enabling the receiver are separate
          switches, on purpose.
        </div>
      )}
      {commands.commands.map((command) => (
        <div className="aprs-cmd" key={command.id}>
          <div className="aprs-cmd-name">{command.name || command.word}</div>
          <div className="aprs-cmd-when">
            On <span className="aprs-cmd-word">{command.word}</span>
            {command.callsign ? (
              <>
                {" "}
                from <span className="aprs-cmd-word">{command.callsign}</span>
              </>
            ) : (
              " from any station"
            )}{" "}
            ·{" "}
            <span
              className={
                command.enabled && logging && !command.locked ? "aprs-armed" : "aprs-armed bad"
              }
            >
              {/* A LOCKED command outranks "not listening": both are reasons it will not
                  fire, but only one of them is the owner's to clear, and hiding it behind
                  the receiver's state is the same kind of lie this screen is written
                  against. */}
              {command.locked || !command.enabled
                ? armedLabel(command)
                : logging
                  ? armedLabel(command)
                  : "armed — not listening"}
            </span>
          </div>
        </div>
      ))}
      {commands.attempts.length > 0 && (
        <>
          <div className="aprs-sec">Attempts</div>
          {commands.attempts.map((attempt) => (
            <div
              className={`aprs-try${attempt.accepted ? "" : " bad"}`}
              key={`${attempt.heard_at}-${attempt.source}`}
            >
              <span className="aprs-call">{attempt.source}</span>
              <div className="aprs-body">
                <div className="aprs-msg">
                  {attempt.word} — {attempt.reason}
                </div>
              </div>
              <span className="aprs-when">{clock(attempt.heard_at)}</span>
            </div>
          ))}
        </>
      )}
    </>
  );
}

/** An elapsed hold, as the mock states it: "held 1h 12m". */
function held(seconds: number): string {
  const mins = Math.max(0, Math.round(seconds / 60));
  return mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

function TunerTab({
  logging,
  listening,
  busy,
  onFree,
  onReleased,
}: {
  /** The APRS session, or null. Was a bare "is anything logging": with a radio each,
   *  that put "In use by APRS logging" over a tuner session that had its own dongle. */
  logging: SdrListening | null;
  /** The LISTENING session, asked per job. `sdr.listening` is whichever one the omnibox
   *  should draw, which is not the same question. */
  listening: SdrListening | null;
  busy: boolean;
  onFree: () => void;
  onReleased: () => void;
}) {
  // Only when APRS is the reason there is nothing to tune. With two dongles both can be
  // live at once, and the tuner controls are then the right thing to show.
  if (logging !== null && listening === null) {
    return (
      <div className="aprs-held" role="alert">
        <b>In use by APRS logging.</b> Release the logging session to listen here, or add a second
        dongle to do both.
        {/* The LOGGING session's elapsed time. It read the listening one's, which on a
            two-dongle box is a different session on a different radio. */}
        <> Held for {held(logging.elapsed_s)}.</>
        {/* Disabled while busy: without it a double tap fires two releases, and the
            second lands on a session that is already gone. */}
        <button type="button" className="aprs-take" disabled={busy} onClick={onFree}>
          Release &amp; listen
        </button>
      </div>
    );
  }
  if (listening) {
    // The REAL transport, not a description of it. This tab used to render frequency
    // and mode as text and point at the composer's icon, which meant the one screen
    // named "Radio" was the one place you could not work the radio. Same component the
    // omnibox icon opens, so the two can never drift apart.
    return <SdrTunerControls listening={listening} onReleased={onReleased} />;
  }
  return (
    <div className="radio-tuner">
      <div className="radio-tuner-freq">—</div>
      <div className="radio-tuner-sub">Idle. Ask jerv to tune something, or use the composer.</div>
    </div>
  );
}

function clock(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
