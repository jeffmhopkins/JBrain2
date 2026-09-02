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

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { type AprsLogState, type AprsPacket, decodeRate, receiverHealth } from "../aprsLog";
import { useSdrSession } from "../sdrSession";

type Tab = "tuner" | "aprs" | "recordings";

const POLL_MS = 5000;

export function RadioScreen({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("aprs");
  const [log, setLog] = useState<AprsLogState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sdr = useSdrSession();

  const refresh = useCallback(async () => {
    try {
      setLog(await api.getAprsPackets());
      setError(null);
    } catch (err) {
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

      {tab === "aprs" && <AprsTab log={log} error={error} busy={busy} onToggle={toggle} />}
      {tab === "tuner" && (
        <TunerTab
          logging={log?.logging === true}
          listening={sdr.listening}
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
  error,
  busy,
  onToggle,
}: {
  log: AprsLogState | null;
  error: string | null;
  busy: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  if (!log) return <p className="radio-empty">Reading the log…</p>;
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

      {error && <p className="radio-error">{error}</p>}

      {log.logging ? (
        <button
          type="button"
          className="aprs-toggle aprs-toggle-on"
          disabled={busy}
          onClick={() => onToggle(false)}
        >
          Stop APRS logging
        </button>
      ) : (
        <button
          type="button"
          className="aprs-toggle"
          disabled={busy}
          onClick={() => onToggle(true)}
        >
          Enable APRS logging
        </button>
      )}
      {!log.logging && (
        <p className="radio-hint">
          Reserves the tuner until released — while it runs, the Tuner tab can't listen.
        </p>
      )}

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

function TunerTab({
  logging,
  listening,
  onFree,
}: {
  logging: boolean;
  listening: { frequency_hz: number; mode: string } | null;
  onFree: () => void;
}) {
  if (logging) {
    return (
      <div className="aprs-held">
        <b>In use by APRS logging.</b> One dongle, one job — release the logging session to listen
        here.
        <button type="button" className="aprs-take" onClick={onFree}>
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
