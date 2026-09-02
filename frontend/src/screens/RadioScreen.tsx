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
  type AprsPacket,
  armedLabel,
  decodeRate,
  receiverHealth,
} from "../aprsLog";
import { useSdrSession } from "../sdrSession";

type Tab = "tuner" | "aprs" | "recordings";

const POLL_MS = 5000;

export function RadioScreen({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("aprs");
  const [log, setLog] = useState<AprsLogState | null>(null);
  const [commands, setCommands] = useState<AprsCommandState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sdr = useSdrSession();
  // ONE reading of who holds the tuner, from the 1 s lease poll — not the 5 s log poll.
  // They are the same sidecar field arriving by two routes at two cadences, and mixing
  // them let the Tuner tab say "in use by APRS" for five seconds after the lease was
  // gone. The shared session store exists precisely so those two can never disagree.
  const holder = sdr.listening?.purpose ?? (sdr.listening ? "listen" : null);

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
    <section className="radio-screen">
      <div className="radio-top">
        <button type="button" className="radio-back" onClick={onClose} aria-label="Back">
          ‹
        </button>
        <h1 className="radio-title">Radio</h1>
      </div>
      <div className="radio-tabs" role="tablist">
        {(["tuner", "aprs", "recordings"] as Tab[]).map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
          >
            {id === "tuner" ? "Tuner" : id === "aprs" ? "APRS" : "Recordings"}
          </button>
        ))}
      </div>

      {tab === "aprs" && (
        <AprsTab
          log={log}
          commands={commands}
          error={error}
          busy={busy}
          holder={holder}
          heldFor={sdr.listening?.elapsed_s ?? null}
          onToggle={toggle}
        />
      )}
      {tab === "tuner" && (
        <TunerTab
          logging={holder === "aprs"}
          listening={sdr.listening}
          busy={busy}
          onFree={() => toggle(false)}
        />
      )}
      {tab === "recordings" && (
        <p className="radio-empty">Recordings arrive in a later wave. Nothing is stored yet.</p>
      )}
    </section>
  );
}

function AprsTab({
  log,
  commands,
  error,
  busy,
  holder,
  heldFor,
  onToggle,
}: {
  log: AprsLogState | null;
  commands: AprsCommandState | null;
  error: string | null;
  busy: boolean;
  /** What is holding the one tuner right now: "listen", "aprs", or nothing. */
  holder: string | null;
  /** Seconds the current session has held it, for the logging state's elapsed time. */
  heldFor: number | null;
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

      {holder === "listen" ? (
        // CONTENTION (docs/mocks/aprs/c-single-dongle.html, shape A). One dongle, one
        // job: with a listening session holding it, "Enable APRS logging" is a button
        // that can only fail — and failing produced a raw lowercase 409 string. Say what
        // holds the radio BEFORE the tap, and offer the one act that resolves it. One
        // CTA, because round 3's own review killed a duplicate here.
        <div className="aprs-held" role="alert">
          <b>The radio is listening.</b> One dongle, one job — release the listening session to log
          APRS, or add a second dongle to do both.
          <button
            type="button"
            className="aprs-take"
            disabled={busy}
            onClick={() => onToggle(true)}
          >
            Release &amp; log APRS
          </button>
        </div>
      ) : log.logging ? (
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
              Holding the tuner for {held(heldFor)} — nothing else can use the radio until this is
              released.
            </p>
          )}
        </>
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

      <div className="aprs-sec">Heard</div>
      {log.packets.length === 0 ? (
        <p className="radio-empty">
          {log.logging
            ? "Nothing heard yet — a quiet channel and a dead antenna look the same here, so the line above shows the last decode rather than a signal bar."
            : "Nothing logged. Turn APRS logging on to start hearing the channel."}
        </p>
      ) : (
        log.packets.map((packet) => <PacketRow key={rowKey(packet)} packet={packet} />)
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
            <span className={command.enabled && logging ? "aprs-armed" : "aprs-armed bad"}>
              {command.enabled && !logging ? "armed — not listening" : armedLabel(command)}
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

function PacketRow({ packet }: { packet: AprsPacket }) {
  return (
    <div className="aprs-row">
      <span className="aprs-call">{packet.source}</span>
      <div className="aprs-body">
        <div className="aprs-msg">
          {/* Badged as heard rather than presented as content of ours: it is a
              stranger's transmission, and the badge is where that rule meets the eye. */}
          <span className="aprs-badge">heard</span>
          {packet.info}
        </div>
        <div className="aprs-meta">
          → {packet.destination}
          {packet.path.length > 0 ? ` via ${packet.path.join(",")}` : ""}
        </div>
      </div>
      <span className="aprs-when">{clock(packet.heard_at)}</span>
    </div>
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
}: {
  logging: boolean;
  listening: { frequency_hz: number; mode: string; elapsed_s?: number } | null;
  busy: boolean;
  onFree: () => void;
}) {
  if (logging) {
    return (
      <div className="aprs-held" role="alert">
        <b>In use by APRS logging.</b> One dongle, one job — release the logging session to listen
        here.
        {listening?.elapsed_s !== undefined && <> Held for {held(listening.elapsed_s)}.</>}
        {/* Disabled while busy: without it a double tap fires two releases, and the
            second lands on a session that is already gone. */}
        <button type="button" className="aprs-take" disabled={busy} onClick={onFree}>
          Release &amp; listen
        </button>
      </div>
    );
  }
  if (listening) {
    return (
      <div className="radio-tuner">
        <div className="radio-tuner-freq">{(listening.frequency_hz / 1_000_000).toFixed(3)}</div>
        <div className="radio-tuner-sub">
          {listening.mode.toUpperCase()} · listening — the radio icon in the composer tunes it
        </div>
      </div>
    );
  }
  return (
    <div className="radio-tuner">
      <div className="radio-tuner-freq">—</div>
      <div className="radio-tuner-sub">Idle. Ask jerv to tune something, or use the composer.</div>
    </div>
  );
}

/** Stable per row: two stations can transmit in the same second. */
function rowKey(packet: AprsPacket): string {
  return `${packet.heard_at}-${packet.source}-${packet.info.slice(0, 24)}`;
}

function clock(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
