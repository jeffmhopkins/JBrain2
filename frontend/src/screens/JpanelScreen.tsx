// jpanel — the panels in the house as one surface (docs/plans/JPANEL_PLAN.md).
//
// Three tabs, because one door for "the panels in my house" beats several that each do half:
// Messages (what the twins posted, and what Dad types back), Panels (the units themselves —
// name them, retire them) and Flash (the panel flasher, MOVED here rather than rebuilt — it
// was already its own surface, so it slots in whole).
//
// Panels exists because managing a panel used to mean the LOCATION screen: panels are the same
// `Subject(kind='device')` substrate as an OwnTracks phone, so every one ever flashed was listed
// there, under a swipe rail, beside a status line (last fix, battery, speed) a panel structurally
// never produces. The owner could not find a revoke at all — "I don't see a way to revoke from
// PWA" — and there was no rename anywhere, so a unit enrolled without a name answered to "the
// other one" until somebody re-flashed it over USB. That is a terminal by another name.
//
// The messaging half is asymmetric on purpose and the asymmetry is the product: the
// panels send audio and are read to; the PWA sends TEXT and reads a transcript. A
// four-year-old cannot type, and a parent at work cannot play audio out loud.

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  type EndpointSettings,
  type JpanelMessage,
  type JpanelThread,
  PANEL_NAME_CHARS,
  PANEL_NAME_MAX,
  PET_NAME_CHARS,
  PET_NAME_MAX,
  type PanelAppearance,
  type PanelForm,
  type PanelStatusOut,
  api,
  jpanelAudioUrl,
} from "../api/client";
import { MicIcon, PlayIcon, SendIcon, StopIcon } from "../components/icons";
import { agoLabel, panelHealth } from "../panelStatus";
import { useForeground } from "../visibility";
import { MAX_MESSAGE_MS, type Recorder, startRecording } from "../voiceMessage";
import { EndpointsScreen } from "./EndpointsScreen";
import "./jpanel.css";

export type JpanelTab = "messages" | "panels" | "flash";

interface JpanelScreenProps {
  onClose: () => void;
  /** Which tab opens. The launcher's Endpoints tile lands straight on Flash: someone
   *  holding a board with a cable in it should not have to find a tab first. */
  initialTab?: JpanelTab;
}

/** Slow enough to be free on a phone, fast enough that "did they send me anything?"
 *  is answered by looking rather than by pulling. Paused while backgrounded. */
const POLL_MS = 20_000;

/** When a message arrived, for someone glancing at a phone at work. Beyond a day the
 *  clock time alone is a lie by omission — "3:14 PM" reads as today — so the date comes
 *  with it. */
export function whenText(iso: string, now: number = Date.now()): string {
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return iso;
  const diff = now - at;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  const d = new Date(at);
  const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${day}, ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

/** How long it takes to listen to, which is the question the play button raises. */
export function durationText(ms: number): string {
  const total = Math.max(1, Math.round(ms / 1000));
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** A message the owner has not heard yet: from a panel, to him, still unplayed. */
function unheard(m: JpanelMessage): boolean {
  return m.direction === "in" && m.played_at === null;
}

function MessagesTab() {
  const [threads, setThreads] = useState<JpanelThread[] | null>(null);
  const [error, setError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [sending, setSending] = useState<string | null>(null);
  const [sendError, setSendError] = useState("");
  const [playing, setPlaying] = useState<string | null>(null);
  const [playError, setPlayError] = useState("");
  /* Which panel is being recorded FOR, not a bare boolean: the screen shows every panel at
     once, and a flag would light the microphone on all of them. */
  const [recording, setRecording] = useState<string | null>(null);
  const recorder = useRef<Recorder | null>(null);
  /* The cap is enforced here as well as on the box, so a long message is stopped and SENT
     rather than truncated on arrival — the failure `audio_play` had on the panel, which must
     not be reintroduced from this end. */
  const recordTimer = useRef<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  /** Ids already reported, so a poll that re-renders the same row does not re-POST it. */
  const seenRef = useRef<Set<string>>(new Set());
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const foreground = useForeground();

  const refresh = useCallback(async () => {
    try {
      const out = await api.jpanelMessages();
      setThreads(out.panels);
      setError("");
    } catch (e) {
      // The messages already on screen stay there: a poll that failed on a train is not
      // evidence the inbox is empty, and blanking it would be the one lie this surface
      // must not tell.
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (!foreground) return;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh, foreground]);

  // A phone that navigates away mid-message must not keep talking from a screen nobody
  // is looking at.
  useEffect(() => {
    return () => {
      audioRef.current?.pause();
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
  }, []);

  /** One refetch for a burst of marks: scrolling past four unheard messages is one
   *  answer to "how many are waiting", not four. The badge always comes back from the
   *  box — nothing here does arithmetic on it. */
  const refreshSoon = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = null;
      void refresh();
    }, 400);
  }, [refresh]);

  /** He has seen it. Reading is what clears a message here — the transcript is the
   *  content, and a father who reads the words at work and never presses play has
   *  genuinely heard from his daughter. A failed report is forgotten rather than
   *  retried in place, so the next pass over the row tries again. */
  const markSeen = useCallback(
    (id: string) => {
      if (seenRef.current.has(id)) return;
      seenRef.current.add(id);
      api
        .markJpanelPlayed(id)
        .then(refreshSoon)
        .catch(() => seenRef.current.delete(id));
    },
    [refreshSoon],
  );

  // What counts as seen is the row having actually been ON SCREEN: a message below the
  // fold of a long thread has not been read, and clearing it on arrival would throw away
  // the one number this screen exists to answer. Gated on the foreground so a phone left
  // open in a pocket does not read his messages for him, and skipped entirely where there
  // is no IntersectionObserver — "cannot tell" has to mean "leave the badge alone", with
  // playing the message the other way it clears.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per fetched list; the effect reads the DOM, not the threads.
  useEffect(() => {
    const root = listRef.current;
    if (!foreground || !root || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset.unplayed;
          if (entry.isIntersecting && id) markSeen(id);
        }
      },
      // Most of the row, so a transcript half off the bottom edge does not count.
      { threshold: 0.6 },
    );
    for (const row of root.querySelectorAll("[data-unplayed]")) observer.observe(row);
    return () => observer.disconnect();
  }, [foreground, markSeen, threads]);

  function stop() {
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(null);
  }

  function play(message: JpanelMessage) {
    const id = message.id;
    const again = playing === id;
    stop();
    if (again) return;
    setPlayError("");
    // Playing is seeing, and it is the only path on a browser with no IntersectionObserver.
    if (message.direction === "in" && message.played_at === null) markSeen(id);
    const audio = new Audio(jpanelAudioUrl(id));
    audioRef.current = audio;
    setPlaying(id);
    const done = () => {
      if (audioRef.current === audio) audioRef.current = null;
      setPlaying((cur) => (cur === id ? null : cur));
    };
    audio.onended = done;
    audio.onerror = () => {
      done();
      setPlayError("Couldn't play that one — is the box reachable?");
    };
    // A blocked autoplay rejects, and an element that never got a real implementation
    // answers with undefined rather than a promise; both have to land somewhere.
    Promise.resolve(audio.play()).catch(() => {
      done();
      setPlayError("Couldn't play that one — is the box reachable?");
    });
  }

  const stopRecording = useCallback(async (deviceId: string) => {
    const active = recorder.current;
    recorder.current = null;
    if (recordTimer.current !== null) {
      window.clearTimeout(recordTimer.current);
      recordTimer.current = null;
    }
    setRecording(null);
    if (!active) return;
    setSending(deviceId);
    setSendError("");
    try {
      const pcm = await active.stop();
      if (pcm.byteLength < 2) throw new Error("nothing was recorded");
      const sent = await api.sendJpanelVoice(deviceId, pcm);
      /* From the server's own row, exactly as the typed path does: the box decides the id,
         the duration and the transcript, and a message that exists only on this phone is
         precisely the message a parent believes they sent and did not. */
      setThreads(
        (cur) =>
          cur?.map((t) =>
            t.device_id === deviceId ? { ...t, messages: [sent, ...t.messages] } : t,
          ) ?? cur,
      );
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(null);
    }
  }, []);

  const beginRecording = useCallback(
    async (deviceId: string) => {
      if (recording !== null || sending !== null) return;
      setSendError("");
      try {
        recorder.current = await startRecording();
        setRecording(deviceId);
        /* Stops and SENDS at the ceiling rather than discarding: someone who has just spoken
           for twenty seconds has said something, and throwing it away for going one second
           long is the worst thing this control could do. */
        recordTimer.current = window.setTimeout(() => {
          void stopRecording(deviceId);
        }, MAX_MESSAGE_MS);
      } catch (e) {
        /* Surfaced, never swallowed. A refused microphone makes this button do nothing, which
           is indistinguishable from a broken one — and the browser only prompts once. */
        recorder.current = null;
        setRecording(null);
        setSendError(
          e instanceof Error && e.name === "NotAllowedError"
            ? "The browser would not give this page the microphone."
            : e instanceof Error
              ? e.message
              : String(e),
        );
      }
    },
    [recording, sending, stopRecording],
  );

  /* The microphone is released when this screen goes, whatever route it left by. A recording
     abandoned by navigation would otherwise hold the mic and its indicator light open. */
  useEffect(
    () => () => {
      recorder.current?.cancel();
      recorder.current = null;
      if (recordTimer.current !== null) window.clearTimeout(recordTimer.current);
    },
    [],
  );

  const [clearing, setClearing] = useState<string | null>(null);
  const [cleared, setCleared] = useState<Record<string, string>>({});
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renamed, setRenamed] = useState<Record<string, string>>({});

  /* NAMING A PANEL, WHICH USED TO MEAN A CABLE.
     A panel's name lives on the box, not in its firmware: `/flash` writes it onto the device
     key it mints, and a unit enrolled without one calls itself "the other one" to its sibling
     forever. Correcting that meant re-flashing over USB — a terminal by another name, which is
     the thing CLAUDE.md #10 exists to stop. A prompt rather than an inline form because this
     is done once per panel and never again. */
  async function renamePanel(deviceId: string, current: string) {
    if (renaming !== null) return;
    const typed = window.prompt(
      `What should ${current} be called? The panel draws it in a 5×7 font, so letters, digits, spaces, hyphens and full stops only.`,
      current === "the other one" ? "" : current,
    );
    if (typed === null) return;
    const name = typed.trim();
    if (!name || name === current) return;
    setRenaming(deviceId);
    setSendError("");
    try {
      const done = await api.renameJpanelPanel(deviceId, name);
      /* SAID OUT LOUD WHEN IT IS NOT ONE KEY, because it usually is not: every flash mints a
         fresh device key and nothing retires the old one, so a panel flashed four times is
         four principals carrying one label. They all move together — otherwise the roster
         grows a second panel still called "the other one" that nothing can reach. */
      setRenamed((r) => ({
        ...r,
        [deviceId]:
          done.keys > 1
            ? `Now ${done.name} — ${done.keys} device keys moved (every flash mints one).`
            : `Now ${done.name}.`,
      }));
      await refresh();
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setRenaming(null);
    }
  }

  async function clearHistory(deviceId: string, name: string) {
    if (clearing !== null) return;
    /* CONFIRMED, BECAUSE IT CANNOT BE UNDONE. Everything else on this surface is recoverable
       by waiting; this is the one control that destroys a child's words. */
    if (!window.confirm(`Delete the conversation with ${name}? This cannot be undone.`)) return;
    setClearing(deviceId);
    setSendError("");
    try {
      const { deleted, kept } = await api.clearJpanelHistory(deviceId);
      setThreads(
        (cur) => cur?.map((t) => (t.device_id === deviceId ? { ...t, messages: [] } : t)) ?? cur,
      );
      /* SAID OUT LOUD WHEN SOMETHING SURVIVED. The box refuses to delete a message a child has
         not heard yet, and a clear that silently leaves rows behind is worse than one that
         refuses — the list afterwards has to match what he expects. */
      setCleared((c) => ({
        ...c,
        [deviceId]: kept
          ? `Cleared ${deleted}. Kept ${kept} ${name} hasn't heard yet — they'll stay until played.`
          : `Cleared ${deleted}.`,
      }));
      await refresh();
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setClearing(null);
    }
  }

  async function send(deviceId: string) {
    const text = (drafts[deviceId] ?? "").trim();
    if (!text || sending !== null) return;
    setSending(deviceId);
    setSendError("");
    try {
      const sent = await api.sendJpanelMessage(deviceId, text);
      setDrafts((d) => ({ ...d, [deviceId]: "" }));
      // Shown from the server's own row rather than an optimistic one: the box decides
      // the id and how long the spoken version runs, and a message that only exists on
      // this phone is exactly the message a parent thinks they sent and did not.
      setThreads(
        (cur) =>
          cur?.map((t) =>
            t.device_id === deviceId ? { ...t, messages: [sent, ...t.messages] } : t,
          ) ?? cur,
      );
    } catch (e) {
      // The draft is deliberately kept: retyping what you wanted to say to your child
      // because the network blipped is the worst outcome this form has.
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(null);
    }
  }

  if (threads === null) {
    return (
      <p className="jp-empty">
        {error ? `Couldn't reach the box — ${error}` : "Loading messages…"}
      </p>
    );
  }

  return (
    <div className="jp-threads" ref={listRef}>
      {error && (
        <p className="jp-warn" role="alert">
          Showing what was last fetched — {error}
        </p>
      )}
      {playError && (
        <p className="jp-warn" role="alert">
          {playError}
        </p>
      )}
      {sendError && (
        <p className="jp-warn" role="alert">
          Not sent — {sendError}. Your words are still in the box below.
        </p>
      )}

      {threads.length === 0 && (
        <p className="jp-empty">No panels yet. Flash one on the Flash tab and it shows up here.</p>
      )}

      {threads.map((thread) => (
        <section className="jp-panel" key={thread.device_id} aria-label={thread.name}>
          <h2 className="jp-panel-head">
            <span className="jp-panel-name">{thread.name}</span>
            {thread.unplayed > 0 && <span className="jp-unplayed">{thread.unplayed} unplayed</span>}
            <button
              type="button"
              className="jp-rename"
              aria-label={`Rename ${thread.name}`}
              disabled={renaming !== null}
              onClick={() => void renamePanel(thread.device_id, thread.name)}
            >
              {renaming === thread.device_id ? "Renaming…" : "Rename"}
            </button>
            {thread.messages.length > 0 && (
              <button
                type="button"
                className="jp-clear"
                aria-label={`Clear the conversation with ${thread.name}`}
                disabled={clearing !== null}
                onClick={() => void clearHistory(thread.device_id, thread.name)}
              >
                {clearing === thread.device_id ? "Clearing…" : "Clear history"}
              </button>
            )}
          </h2>
          {renamed[thread.device_id] && (
            <output className="jp-cleared">{renamed[thread.device_id]}</output>
          )}
          {cleared[thread.device_id] && (
            /* `<output>`, not a `<p role="status">`: it carries the same implicit role and is
               the element the rule asks for — and a screen reader should announce what a
               destructive button just did without the owner having to go looking. */
            <output className="jp-cleared">{cleared[thread.device_id]}</output>
          )}

          {thread.messages.length === 0 ? (
            <p className="jp-empty">Nothing from {thread.name} yet.</p>
          ) : (
            <ul className="jp-msgs">
              {thread.messages.map((m) => (
                <li
                  className={`jp-msg${unheard(m) ? " jp-msg-new" : ""}`}
                  key={m.id}
                  // Only a panel's own unheard message is the owner's to clear: his sent
                  // text is not his to read, and twin-to-twin post was never his at all.
                  data-unplayed={unheard(m) ? m.id : undefined}
                >
                  <div className="jp-msg-head">
                    <span className="jp-from">{m.from_name}</span>
                    <time dateTime={m.created_at}>{whenText(m.created_at)}</time>
                    <span className="jp-dur">{durationText(m.duration_ms)}</span>
                    {/* STATUS, AND ONLY ON WHAT YOU SENT. For an inbound message `played_at`
                        means "the owner has dealt with it", which is the unplayed badge's job
                        and would read as nonsense here. For an outbound one it is the only
                        live question: has the child actually heard it. */}
                    {m.direction === "out" && (
                      <span
                        className={`jp-status${
                          m.played_at ? " jp-status-heard" : m.undelivered ? " jp-status-stuck" : ""
                        }`}
                      >
                        {m.played_at
                          ? `Heard ${whenText(m.played_at)}`
                          : m.undelivered
                            ? // THE BOX GAVE UP, which is not the same as nobody having come to
                              // it yet — and `played_at` cannot tell those apart. Said plainly,
                              // because the alternative is a parent believing their child chose
                              // not to listen when the panel never managed to play it.
                              "Couldn't be delivered"
                            : "Not heard yet"}
                      </span>
                    )}
                  </div>
                  <div className="jp-msg-body">
                    {/* The transcript is the content, not a caption under a player: it is
                        what gets read at work. The audio sits beside it as the fallback
                        for when it does not make sense — which, with a four-year-old on
                        the other end, it often will not. */}
                    {m.transcript.trim() ? (
                      <p className="jp-transcript">{m.transcript}</p>
                    ) : (
                      <p className="jp-transcript jp-no-words">
                        No words came through — play it to hear what they said.
                      </p>
                    )}
                    <button
                      type="button"
                      className="jp-play"
                      aria-label={
                        playing === m.id ? "Stop playing" : `Play ${m.from_name}'s message`
                      }
                      onClick={() => play(m)}
                    >
                      {playing === m.id ? <StopIcon size={18} /> : <PlayIcon size={18} />}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {/* TWO WAYS TO SAY IT, and the microphone is the one that matters most.
              The owner: *"PWA should also be able to actually send audio, a voice message,
              that have the option to send text that gets rendered."*
              Type and the box reads it out in a voice of its own (never the pet's); or hold
              the microphone and the panel plays Dad's ACTUAL voice — which, for a child who
              cannot read, is the only version that carries who it is from. */}
          <form
            className="jp-compose"
            onSubmit={(e) => {
              e.preventDefault();
              void send(thread.device_id);
            }}
          >
            <input
              value={drafts[thread.device_id] ?? ""}
              onChange={(e) => setDrafts((d) => ({ ...d, [thread.device_id]: e.target.value }))}
              placeholder={`Say something to ${thread.name}`}
              aria-label={`Message ${thread.name}`}
              enterKeyHint="send"
            />
            {/* The microphone yields to a typed draft rather than sitting beside it armed:
                with words in the box the obvious action is to send them, and two live buttons
                is the moment a parent taps the wrong one. */}
            {(drafts[thread.device_id] ?? "").trim() ? (
              <button
                type="submit"
                className="jp-send"
                aria-label={`Send to ${thread.name}`}
                disabled={sending !== null}
              >
                <SendIcon size={18} />
              </button>
            ) : (
              <button
                type="button"
                className={`jp-mic${recording === thread.device_id ? " jp-mic-live" : ""}`}
                aria-label={
                  recording === thread.device_id
                    ? `Stop and send to ${thread.name}`
                    : `Record a message for ${thread.name}`
                }
                aria-pressed={recording === thread.device_id}
                disabled={
                  sending !== null || (recording !== null && recording !== thread.device_id)
                }
                onClick={() => {
                  if (recording === thread.device_id) void stopRecording(thread.device_id);
                  else void beginRecording(thread.device_id);
                }}
              >
                {recording === thread.device_id ? <StopIcon size={18} /> : <MicIcon size={18} />}
              </button>
            )}
          </form>
          <p className="jp-hint">
            {recording === thread.device_id
              ? "Recording — press again to send."
              : "They hear it read out on their panel — they never read it."}
          </p>
        </section>
      ))}
    </div>
  );
}

/** THE PANELS THEMSELVES — name one, retire one.
 *
 * Read from the fleet route rather than a list of its own: that route already collapses a unit's
 * flashes into ONE row (`DISTINCT ON (label)`), which is the only reading under which "this
 * panel" means a thing on a wall rather than a key in a table. A second listing would have had
 * to re-derive that and would eventually have disagreed with the one in Ops.
 */
function PanelsTab() {
  const [panels, setPanels] = useState<PanelStatusOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: tick is a re-run trigger, not read here
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await api.panelStatus();
        if (!cancelled) {
          setPanels(result.panels);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("Could not read the panels. Is the box reachable?");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tick]);

  if (error) return <p className="jp-empty">{error}</p>;
  if (panels === null) return <p className="jp-empty">Reading the panels…</p>;
  if (panels.length === 0) {
    return (
      <p className="jp-empty">No panels yet. Flash one on the Flash tab and it shows up here.</p>
    );
  }

  return (
    <div className="jp-panels">
      <PanelAudio />
      {panels.map((p) => (
        <PanelRow key={p.device_id} panel={p} onChanged={() => setTick((t) => t + 1)} />
      ))}
    </div>
  );
}

/** THE KNOBS EVERY PANEL SHARES — volume, microphone gain, and the codec's AGC.
 *
 * ONE ROW ON THE BOX, not per panel, which is why this sits above the list rather than inside a
 * row. Until now it was reachable only from the debug console, which needs a token the owner has
 * to be handed — the same no-terminal gap (CLAUDE.md #10) that hid revoke on the Location screen.
 *
 * `mic_agc` is the reason this surface exists at all. The owner, after the first real voice
 * message came off a panel: *"we need the auto gain control from panel mic too, it was way too
 * quiet."* Both panels run the same gain and their last readings were a clipping 32767 and a
 * near-silent 814 — one constant cannot serve both. */
function PanelAudio() {
  const [cfg, setCfg] = useState<EndpointSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const got = await api.endpointSettings();
        if (!cancelled) setCfg(got);
      } catch {
        if (!cancelled) setError("Could not read the panel settings.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function save(next: EndpointSettings): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // The box CLAMPS rather than rejects, so what comes back is the truth — show that rather
      // than the number just typed, or a value silently capped reads as the control ignoring you.
      setCfg(await api.setEndpointSettings(next));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save it.");
    } finally {
      setBusy(false);
    }
  }

  if (error !== null && cfg === null) return <p className="jp-panel-error">{error}</p>;
  if (cfg === null) return null;

  return (
    <div className="jp-audio">
      <h2>Every panel</h2>
      <label className="jp-slider">
        <span>
          Speaker volume <strong>{cfg.volume}</strong>
        </span>
        <input
          type="range"
          min={0}
          max={100}
          value={cfg.volume}
          disabled={busy}
          onChange={(e) => setCfg({ ...cfg, volume: Number(e.target.value) })}
          onPointerUp={() => void save(cfg)}
          onKeyUp={() => void save(cfg)}
        />
      </label>
      <label className="jp-slider">
        <span>
          Microphone gain <strong>{cfg.mic_gain_db} dB</strong>
        </span>
        <input
          type="range"
          min={0}
          max={42}
          value={cfg.mic_gain_db}
          disabled={busy}
          onChange={(e) => setCfg({ ...cfg, mic_gain_db: Number(e.target.value) })}
          onPointerUp={() => void save(cfg)}
          onKeyUp={() => void save(cfg)}
        />
      </label>
      <label className="jp-slider">
        <span>
          Screen brightness <strong>{cfg.brightness}</strong>
        </span>
        <input
          type="range"
          min={0}
          max={255}
          value={cfg.brightness}
          disabled={busy}
          onChange={(e) => setCfg({ ...cfg, brightness: Number(e.target.value) })}
          onPointerUp={() => void save(cfg)}
          onKeyUp={() => void save(cfg)}
        />
      </label>
      {/* THE ONE THE OWNER ASKED FOR BY FINDING IT BROKEN. It was a hardcoded quarter, and a
          quarter of 255 is 63 — which does not read as dim in a dark room, because a quarter of
          a register is nowhere near a quarter of perceived brightness. Shown as the resulting
          VALUE as well as the percentage, so the number being chosen is the one the panel will
          actually use rather than an abstraction over it. */}
      <label className="jp-slider">
        <span>
          Dimmed to <strong>{cfg.dim_percent}%</strong>{" "}
          {/* One interpolated string rather than several nodes: this is the number that misled
              us, and it should be greppable on screen and in a test as one piece of text. */}
          {/* FLOOR, NOT ROUND, because the panel truncates: `screen_level()` does the same sum in C
              integer arithmetic, so 255 at 25% is 63 there and rounding would show 64 here. A
              control that reports a value the device never uses is worse than one that reports
              nothing — this whole setting exists because a brightness number misled us once. */}
          <em>{`(${Math.floor((cfg.brightness * cfg.dim_percent) / 100)} of ${cfg.brightness})`}</em>
        </span>
        <input
          type="range"
          min={0}
          max={100}
          value={cfg.dim_percent}
          disabled={busy}
          onChange={(e) => setCfg({ ...cfg, dim_percent: Number(e.target.value) })}
          onPointerUp={() => void save(cfg)}
          onKeyUp={() => void save(cfg)}
        />
      </label>
      <label className="jp-check">
        <input
          type="checkbox"
          checked={cfg.mic_agc}
          disabled={busy}
          onChange={(e) => void save({ ...cfg, mic_agc: e.target.checked })}
        />
        <span>
          Automatic microphone gain
          <em>
            Lets the panel turn its own microphone up for a quiet voice and down for a loud one,
            instead of one fixed setting for every child and every room. New &mdash; worth trying if
            a panel sounds too quiet.
          </em>
        </span>
      </label>
      {saved && <p className="jp-panel-hint">Saved. Panels pick this up within 15 minutes.</p>}
      {error !== null && <p className="jp-panel-error">{error}</p>}
    </div>
  );
}

/** THE PET ITSELF: what it is called, and which body it wears.
 *
 * `pet_name` is the WAKE WORD, so this is not a caption — it changes what a four-year-old says
 * to the thing on her wall. The panel's `vocab.c` has wanted this since it was written: *"a name
 * only a rebuild can change is a name they cannot change, and the two panels will want different
 * ones."* A rebuild is a cable, and the owner has no terminal.
 *
 * Both fields save together in one PUT, because they are one question — "what is this pet" —
 * and two saves would let the owner leave the panel half-changed while walking away from it. */
function PetEditor({
  panel,
  onDone,
}: {
  panel: PanelStatusOut;
  onDone: () => void;
}) {
  const [look, setLook] = useState<PanelAppearance | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Read before editing: opening on a default would overwrite a real setting the moment the
  // owner pressed save without ever showing him what he was replacing.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const got = await api.panelAppearance(panel.device_id);
        if (!cancelled) setLook(got);
      } catch {
        if (!cancelled) setError("Could not read this panel's pet.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [panel.device_id]);

  if (error !== null && look === null) return <p className="jp-panel-error">{error}</p>;
  if (look === null) return <p className="jp-panel-hint">Reading…</p>;

  const trimmed = look.pet_name.trim();
  // Empty is ALLOWED and means "leave it as the firmware shipped" — a blank name would otherwise
  // leave a child saying something the panel cannot hear.
  const nameOk = trimmed === "" || (trimmed.length <= PET_NAME_MAX && PET_NAME_CHARS.test(trimmed));

  async function save(): Promise<void> {
    if (!nameOk || busy || look === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.setPanelAppearance(panel.device_id, { ...look, pet_name: trimmed });
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save it.");
      setBusy(false);
    }
  }

  return (
    <div className="jp-panel-rename">
      <label>
        Its name
        <input
          value={look.pet_name}
          maxLength={PET_NAME_MAX}
          placeholder="fish"
          onChange={(e) => setLook({ ...look, pet_name: e.target.value })}
        />
      </label>
      {/* SAID OUT LOUD, NOT JUST WRITTEN. Worth stating plainly on the screen where it is
          chosen: the owner is picking a word two four-year-olds have to be able to say and a
          speech model has to be able to hear, and a name that reads well can still land badly. */}
      <p className="jp-panel-hint">
        This is what they <strong>say</strong> to it — the panel listens for &ldquo;hey{" "}
        {trimmed.toLowerCase() || "fish"}&rdquo;. Letters and spaces only, {PET_NAME_MAX} at most.
        Leave it empty to keep the name it shipped with.
      </p>

      <fieldset className="jp-form-pick">
        <legend>Its body</legend>
        {(["ostrich", "robot"] as PanelForm[]).map((f) => (
          <label className="jp-radio" key={f}>
            <input
              type="radio"
              name={`form-${panel.device_id}`}
              checked={look.form === f}
              onChange={() => setLook({ ...look, form: f })}
            />
            <span>{f === "ostrich" ? "Ostrich" : "Robot"}</span>
          </label>
        ))}
      </fieldset>
      {/* The gesture is not being taken away, and saying so stops this reading as a lock. */}
      <p className="jp-panel-hint">
        What it comes back as after a restart. Four taps and a hold on the panel still swaps the
        body there and then.
      </p>

      <div className="jp-panel-actions">
        <button type="button" disabled={!nameOk || busy} onClick={() => void save()}>
          {busy ? "saving…" : "save"}
        </button>
        <button type="button" onClick={onDone}>
          cancel
        </button>
      </div>
      {error !== null && <p className="jp-panel-error">{error}</p>}
    </div>
  );
}

/** One unit: what it is, what it last said, and the things the owner can do to it. */
function PanelRow({ panel, onChanged }: { panel: PanelStatusOut; onChanged: () => void }) {
  const [renaming, setRenaming] = useState(false);
  const [editingPet, setEditingPet] = useState(false);
  const [draft, setDraft] = useState(panel.name);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = draft.trim();
  // Checked here as well as on the box, so the owner finds out while still typing rather than
  // after a round trip. The box is still the authority — this cannot be the only check.
  const nameOk =
    trimmed.length > 0 && trimmed.length <= PANEL_NAME_MAX && PANEL_NAME_CHARS.test(trimmed);

  async function rename(): Promise<void> {
    if (!nameOk || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.renamePanel(panel.device_id, trimmed);
      setRenaming(false);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not rename it.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(): Promise<void> {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.revokePanel(panel.device_id);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not revoke it.");
      setBusy(false);
      setArmed(false);
    }
  }

  const state = panelHealth(panel.age_s);

  return (
    <div className={`jp-panel${state === "ok" ? "" : " quiet"}`}>
      <div className="jp-panel-head">
        <span className="jp-panel-name">{panel.name}</span>
        {/* Only a display is badged: every panel in this house is a pet, so marking both would
            put a word on every row that answers a question nobody asked. */}
        {panel.role === "display" && <span className="jp-panel-role">display</span>}
        <span className="jp-panel-seen">
          {state === "never" ? "never reported" : agoLabel(panel.age_s)}
        </span>
      </div>
      <p className="jp-panel-meta">{panel.version || "no firmware reported yet"}</p>

      {renaming ? (
        <div className="jp-panel-rename">
          <label>
            Call it
            <input
              // biome-ignore lint/a11y/noAutofocus: the owner tapped rename; the box is the next thing they want
              autoFocus
              value={draft}
              maxLength={PANEL_NAME_MAX}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void rename();
                if (e.key === "Escape") setRenaming(false);
              }}
            />
          </label>
          {/* The constraint is the PANEL'S FONT, two packages away: `font.c` has 5x7 cells for
              A-Z, the digits, space, hyphen and full stop, and a character it does not have draws
              as nothing — so an apostrophe would reach a four-year-old as a pop-up from someone
              missing a letter. Said plainly rather than enforced silently. */}
          <p className="jp-panel-hint">
            Letters, digits, spaces, hyphens and full stops — {PANEL_NAME_MAX} at most. It is drawn
            on the other panel&rsquo;s screen, which has no other characters.
          </p>
          <div className="jp-panel-actions">
            <button type="button" disabled={!nameOk || busy} onClick={() => void rename()}>
              {busy ? "saving…" : "save"}
            </button>
            <button type="button" onClick={() => setRenaming(false)}>
              cancel
            </button>
          </div>
        </div>
      ) : editingPet ? (
        <PetEditor
          panel={panel}
          onDone={() => {
            setEditingPet(false);
            onChanged();
          }}
        />
      ) : (
        <div className="jp-panel-actions">
          <button
            type="button"
            onClick={() => {
              setDraft(panel.name);
              setArmed(false);
              setRenaming(true);
            }}
          >
            rename
          </button>
          {/* THE PANEL'S NAME AND THE PET'S NAME ARE DIFFERENT THINGS, and the labels have to
              carry that: "rename" is which unit this is — the heading on the thread, what a
              sibling's pop-up reads out — while this is the creature on the glass and the word
              the twins say to it. Two buttons a finger apart doing near-identical-sounding
              things is how the wrong one gets pressed. */}
          <button
            type="button"
            onClick={() => {
              setArmed(false);
              setEditingPet(true);
            }}
          >
            its pet
          </button>
          <button
            type="button"
            className={armed ? "jp-armed" : ""}
            disabled={busy}
            onClick={() => void revoke()}
          >
            {busy ? "revoking…" : armed ? "tap again to revoke" : "revoke"}
          </button>
          {armed && !busy && (
            <button type="button" onClick={() => setArmed(false)}>
              cancel
            </button>
          )}
        </div>
      )}
      {error && <p className="jp-panel-error">{error}</p>}
    </div>
  );
}

export function JpanelScreen({ onClose, initialTab = "messages" }: JpanelScreenProps) {
  const [tab, setTab] = useState<JpanelTab>(initialTab);

  return (
    <div className="jp-wrap">
      <header className="jp-bar">
        <button type="button" onClick={onClose}>
          Back
        </button>
        <h1>jpanel</h1>
      </header>

      <div className="jp-seg" role="tablist" aria-label="Messages, Panels or Flash">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "messages"}
          className={tab === "messages" ? "on" : ""}
          onClick={() => setTab("messages")}
        >
          Messages
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "panels"}
          className={tab === "panels" ? "on" : ""}
          onClick={() => setTab("panels")}
        >
          Panels
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "flash"}
          className={tab === "flash" ? "on" : ""}
          onClick={() => setTab("flash")}
        >
          Flash
        </button>
      </div>

      {/* Unmounted rather than hidden when another tab is up: Flash holds a USB console
          stream open, and Messages polls — neither should run behind a tab nobody is on. */}
      {tab === "messages" && <MessagesTab />}
      {tab === "panels" && <PanelsTab />}
      {tab === "flash" && <EndpointsScreen />}
    </div>
  );
}
