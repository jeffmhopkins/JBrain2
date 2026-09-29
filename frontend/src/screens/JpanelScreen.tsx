// jpanel — the panels in the house as one surface (docs/plans/JPANEL_PLAN.md).
//
// Three tabs, because one door for "the panels in my house" beats several that each do half:
// Messages (one child's whole conversation — see `ConversationTab`), Panels (the units
// themselves — name them, retire them) and Flash (the panel flasher, MOVED here rather than
// rebuilt — it was already its own surface, so it slots in whole).
//
// THERE WAS A FOURTH, and folding it back in is the point of the current shape. `Chats` held
// what the children said to the pet, which is half of a child's afternoon; `Messages` held the
// other half. Two tabs, one child, and no way to read either in the order it happened. They are
// one thread now, picked by the panel's name.
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

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  type EndpointSettings,
  type JpanelMessage,
  type JpanelPetChat,
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
import { agoLabel, panelHealth, panelStateWords } from "../panelStatus";
import { useForeground } from "../visibility";
import { MAX_MESSAGE_MS, type Recorder, startRecording } from "../voiceMessage";
import { EndpointsScreen } from "./EndpointsScreen";
import { atLiveEnd, threadItems, threadPanels } from "./jpanelThread";
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

/* ── ONE CHILD, ONE CONVERSATION ───────────────────────────────────────────────────────────
 *
 * The owner: *"I want there to be a separate selection underneath the top where you can select
 * ... that'll be the panel's names. So if I select lydian or Elora up there it should show
 * those two as conversations ... kind of like a normal conversation does with jerv or the other
 * ones in the pwa where there's an omnibox at the bottom and a left and right conversation
 * bubble."*
 *
 * WHAT THIS REPLACED was every panel stacked down one page, each with its own list and its own
 * composer — a shape that gets worse with every panel added, and reads as a report rather than
 * as a conversation. Beside it sat a second tab holding the OTHER half of the same child's
 * afternoon: what she had been saying to the pet on her wall. She does not experience those as
 * two things. She asks the pet why fish sleep and then records something for her father about
 * it; split across two tabs, the second sentence has no first half.
 *
 * So the picker is the panel's name and the thread is everything that happened on it, merged by
 * time (`jpanelThread.ts`) — a message lands BETWEEN the question she asked the pet and the
 * answer it gave her, because that is where it happened.
 *
 * LEFT IS THE PANEL, RIGHT IS THE OWNER. The pet's replies are on the left too, tinted rather
 * than sided differently: the pet is not him, it is the other voice in her room, and putting a
 * machine's answer where his own words go would make it read as something he said.
 */
function ConversationTab() {
  const [threads, setThreads] = useState<JpanelThread[] | null>(null);
  const [chats, setChats] = useState<JpanelPetChat[] | null>(null);
  const [error, setError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [sending, setSending] = useState<string | null>(null);
  const [sendError, setSendError] = useState("");
  const [playing, setPlaying] = useState<string | null>(null);
  const [playError, setPlayError] = useState("");
  /* Which panel is being recorded FOR, not a bare boolean: a flag would light the microphone
     on a panel the owner had since switched away from. */
  const [recording, setRecording] = useState<string | null>(null);
  /** Which chip is chosen. `null` means "whichever is first" rather than a panel — a real
   *  device id here would go stale the moment a panel is renamed or retired. */
  const [picked, setPicked] = useState<string | null>(null);
  const recorder = useRef<Recorder | null>(null);
  const recordTimer = useRef<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  /** Whether to follow the conversation down when a new line arrives — see `atLiveEnd`. */
  const stick = useRef(true);
  /** Ids already reported, so a poll that re-renders the same row does not re-POST it. */
  const seenRef = useRef<Set<string>>(new Set());
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const foreground = useForeground();

  const refresh = useCallback(async () => {
    try {
      /* BOTH, TOGETHER. Two awaits in series would paint a thread whose pet half was one
         round trip older than its message half — and the merge is by time, so the seam would
         show as lines appearing above ones already on screen. */
      const [msgs, pet] = await Promise.all([api.jpanelMessages(), api.jpanelChats()]);
      setThreads(msgs.panels);
      setChats(pet.panels);
      setError("");
    } catch (e) {
      // What is on screen stays: a poll that failed on a train is not evidence the inbox is
      // empty, and blanking it would be the one lie this surface must not tell.
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

  const panels = useMemo(() => threadPanels(threads ?? [], chats ?? []), [threads, chats]);
  /* Resolved on every render rather than synced into state by an effect: a `picked` that no
     longer names a live panel falls back to the first one instead of rendering nothing. */
  const active = panels.find((p) => p.device_id === picked) ?? panels[0];
  const items = useMemo(
    () =>
      active
        ? threadItems(
            threads?.find((t) => t.device_id === active.device_id),
            chats?.find((c) => c.device_id === active.device_id),
          )
        : [],
    [active, threads, chats],
  );

  /* THE NEWEST LINE IS THE ONE ABOVE THE COMPOSER, so the thread opens at its live end —
     and stays there as messages arrive, unless the owner has scrolled up to read back
     through this morning, in which case a poll every twenty seconds must not take the page
     away from him. */
  // biome-ignore lint/correctness/useExhaustiveDependencies: `items` is the TRIGGER, not a read — the effect measures the DOM the new items just produced.
  useLayoutEffect(() => {
    const el = listRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  /* Switching child is always a jump to the live end: the last thing said is what the chip
     was pressed to see. */
  // biome-ignore lint/correctness/useExhaustiveDependencies: the point is the panel change, not the items.
  useLayoutEffect(() => {
    stick.current = true;
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [active?.device_id]);

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

  // What counts as seen is the row having actually been ON SCREEN: a message above the fold of
  // a long thread has not been read, and clearing it on arrival would throw away the one number
  // this screen exists to answer. Gated on the foreground so a phone left open in a pocket does
  // not read his messages for him, and skipped entirely where there is no IntersectionObserver
  // — "cannot tell" has to mean "leave the badge alone", with playing the message the other way
  // it clears.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per rendered list; the effect reads the DOM, not the items.
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
      // Most of the bubble, so a transcript half off the edge does not count.
      { threshold: 0.6 },
    );
    for (const row of root.querySelectorAll("[data-unplayed]")) observer.observe(row);
    return () => observer.disconnect();
  }, [foreground, markSeen, items]);

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
      // Saying something is always a jump to the bottom, wherever he had scrolled to.
      stick.current = true;
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
    if (!window.confirm(`Delete the messages with ${name}? This cannot be undone.`)) return;
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

  /* A SEPARATE CONTROL FROM THE ONE ABOVE, and deliberately so although they now clear two
     halves of one visible thread. The messages are post between two people and the box refuses
     to destroy one a child has not heard; the pet transcript is a record of what she said to a
     machine. Merging the buttons would mean one press deleting both, and the owner asked for
     the transcript to be clearable precisely so it could be cleared on its own. */
  async function clearPetChat(deviceId: string, name: string) {
    if (clearing !== null) return;
    if (!window.confirm(`Delete what ${name} said to the pet? This cannot be undone.`)) return;
    setClearing(deviceId);
    setSendError("");
    try {
      const { deleted } = await api.jpanelClearChats(deviceId);
      setChats(
        (cur) => cur?.map((c) => (c.device_id === deviceId ? { ...c, turns: [] } : c)) ?? cur,
      );
      setCleared((c) => ({ ...c, [deviceId]: `Cleared ${deleted} pet conversations.` }));
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
      stick.current = true;
    } catch (e) {
      // The draft is deliberately kept: retyping what you wanted to say to your child
      // because the network blipped is the worst outcome this form has.
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(null);
    }
  }

  if (threads === null || chats === null) {
    return (
      <p className="jp-empty">
        {error ? `Couldn't reach the box — ${error}` : "Loading messages…"}
      </p>
    );
  }

  if (active === undefined) {
    return (
      <p className="jp-empty">No panels yet. Flash one on the Flash tab and it shows up here.</p>
    );
  }

  const draft = drafts[active.device_id] ?? "";
  const isRecording = recording === active.device_id;

  return (
    <div className="jp-convo">
      {/* WHICH CHILD, and it is the first thing under the tabs because it is the first
          decision: everything below answers a question about one of them. The unplayed count
          rides the chip so "who is waiting on me?" is answered without opening either.

          `jp-seg` AS WELL AS `jp-who`, so this is the same control as the tabs above rather than
          something that resembles them. The owner, on the row of pills this first shipped as:
          *"it should be in the same kind of radio selection as the top selector."* */}
      <div className="jp-seg jp-who" role="tablist" aria-label="Which panel">
        {panels.map((p) => (
          <button
            type="button"
            role="tab"
            key={p.device_id}
            aria-selected={p.device_id === active.device_id}
            className={p.device_id === active.device_id ? "on" : ""}
            onClick={() => setPicked(p.device_id)}
          >
            {/* Wrapped, so a long name truncates inside its own share of the row instead of
                shoving the count off the end of it. */}
            <span>{p.name}</span>
            {p.unplayed > 0 && <span className="jp-who-badge">{p.unplayed}</span>}
          </button>
        ))}
      </div>

      <div className="jp-convo-head">
        <button
          type="button"
          className="jp-rename"
          disabled={renaming !== null}
          onClick={() => void renamePanel(active.device_id, active.name)}
        >
          {renaming === active.device_id ? "Renaming…" : "Rename"}
        </button>
        <button
          type="button"
          className="jp-clear"
          disabled={clearing !== null}
          onClick={() => void clearHistory(active.device_id, active.name)}
        >
          Clear messages
        </button>
        <button
          type="button"
          className="jp-clear"
          disabled={clearing !== null}
          onClick={() => void clearPetChat(active.device_id, active.name)}
        >
          Clear pet chat
        </button>
      </div>

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
      {renamed[active.device_id] && (
        <output className="jp-cleared">{renamed[active.device_id]}</output>
      )}
      {cleared[active.device_id] && (
        /* `<output>`, not a `<p role="status">`: it carries the same implicit role and is the
           element the rule asks for — and a screen reader should announce what a destructive
           button just did without the owner having to go looking. */
        <output className="jp-cleared">{cleared[active.device_id]}</output>
      )}

      <div
        className="jp-stream"
        ref={listRef}
        onScroll={() => {
          const el = listRef.current;
          if (el) stick.current = atLiveEnd(el);
        }}
      >
        {items.length === 0 ? (
          <p className="jp-empty">
            Nothing from {active.name} yet — say something and it plays out on her panel.
          </p>
        ) : (
          items.map((item) =>
            item.kind === "message" ? (
              <MessageBubble
                key={item.key}
                message={item.message}
                playing={playing === item.message.id}
                onPlay={() => play(item.message)}
              />
            ) : item.kind === "asked" ? (
              <div className="jp-b jp-b-ask" key={item.key}>
                {/* SAID TO THE PET, NOT TO HIM, and the thread has to say which: without the
                    label these are her words arriving in his conversation, and a parent would
                    reasonably read them as addressed to him. */}
                <p className="jp-b-who">to the pet</p>
                <p className="jp-b-text">{item.turn.heard || "(nothing heard)"}</p>
                <p className="jp-b-meta">
                  <time dateTime={item.turn.created_at}>{whenText(item.turn.created_at)}</time>
                </p>
              </div>
            ) : (
              <div className="jp-b jp-b-reply" key={item.key}>
                <p className="jp-b-text">{item.turn.reply}</p>
                {/* The one number worth surfacing: how long she waited for an answer. */}
                <p className="jp-b-meta">{(item.turn.total_ms / 1000).toFixed(1)}s</p>
              </div>
            ),
          )
        )}
      </div>

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
          void send(active.device_id);
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDrafts((d) => ({ ...d, [active.device_id]: e.target.value }))}
          placeholder={`Say something to ${active.name}`}
          aria-label={`Message ${active.name}`}
          enterKeyHint="send"
        />
        {/* The microphone yields to a typed draft rather than sitting beside it armed:
            with words in the box the obvious action is to send them, and two live buttons
            is the moment a parent taps the wrong one. */}
        {draft.trim() ? (
          <button
            type="submit"
            className="jp-send"
            aria-label={`Send to ${active.name}`}
            disabled={sending !== null}
          >
            <SendIcon size={18} />
          </button>
        ) : (
          <button
            type="button"
            className={`jp-mic${isRecording ? " jp-mic-live" : ""}`}
            aria-label={
              isRecording
                ? `Stop and send to ${active.name}`
                : `Record a message for ${active.name}`
            }
            aria-pressed={isRecording}
            disabled={sending !== null || (recording !== null && !isRecording)}
            onClick={() => {
              if (isRecording) void stopRecording(active.device_id);
              else void beginRecording(active.device_id);
            }}
          >
            {isRecording ? <StopIcon size={18} /> : <MicIcon size={18} />}
          </button>
        )}
      </form>
      <p className="jp-hint">
        {isRecording
          ? "Recording — press again to send."
          : "They hear it read out on their panel — they never read it."}
      </p>
    </div>
  );
}

/** One message, as a bubble. His own on the right, hers on the left, and twin-to-twin post on
 *  the left with both names — it was never addressed to him and must not read as though it
 *  was. */
function MessageBubble({
  message,
  playing,
  onPlay,
}: {
  message: JpanelMessage;
  playing: boolean;
  onPlay: () => void;
}) {
  const mine = message.direction === "out";
  return (
    <div
      className={`jp-b ${mine ? "jp-b-me" : "jp-b-them"}${unheard(message) ? " jp-b-new" : ""}`}
      // Only a panel's own unheard message is the owner's to clear: his sent text is not his
      // to read, and twin-to-twin post was never his at all.
      data-unplayed={unheard(message) ? message.id : undefined}
    >
      {/* WHO IT WAS ACTUALLY BETWEEN. Drawn only when the owner is not one end of it — his own
          thread does not need telling that a message to him was to him — but a message between
          the two girls named only its sender, so it read as though it had come to him. */}
      {message.direction === "between" && (
        <p className="jp-b-who">
          {message.from_name} → {message.to_name}
        </p>
      )}
      <div className="jp-b-row">
        {/* The transcript is the content, not a caption under a player: it is what gets read
            at work. The audio sits beside it as the fallback for when it does not make sense
            — which, with a four-year-old on the other end, it often will not. */}
        {message.transcript.trim() ? (
          <p className="jp-b-text">{message.transcript}</p>
        ) : (
          <p className="jp-b-text jp-no-words">
            No words came through — play it to hear what they said.
          </p>
        )}
        <button
          type="button"
          className="jp-play"
          aria-label={playing ? "Stop playing" : `Play ${message.from_name}'s message`}
          onClick={onPlay}
        >
          {playing ? <StopIcon size={18} /> : <PlayIcon size={18} />}
        </button>
      </div>
      <p className="jp-b-meta">
        <time dateTime={message.created_at}>{whenText(message.created_at)}</time>
        <span className="jp-dur">{durationText(message.duration_ms)}</span>
        {/* STATUS, AND ONLY ON WHAT YOU SENT. For an inbound message `played_at` means "the
            owner has dealt with it", which is the unplayed badge's job and would read as
            nonsense here. For an outbound one it is the only live question: has the child
            actually heard it. */}
        {mine && (
          <span
            className={`jp-status${
              message.played_at ? " jp-status-heard" : message.undelivered ? " jp-status-stuck" : ""
            }`}
          >
            {message.played_at
              ? `Heard ${whenText(message.played_at)}`
              : message.undelivered
                ? // THE BOX GAVE UP, which is not the same as nobody having come to it yet —
                  // and `played_at` cannot tell those apart. Said plainly, because the
                  // alternative is a parent believing their child chose not to listen when the
                  // panel never managed to play it.
                  "Couldn't be delivered"
                : "Not heard yet"}
          </span>
        )}
      </p>
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
  const foreground = useForeground();

  // biome-ignore lint/correctness/useExhaustiveDependencies: tick is a re-run trigger, not read here
  useEffect(() => {
    let cancelled = false;
    const read = async (): Promise<void> => {
      try {
        const result = await api.panelStatus();
        if (!cancelled) {
          setPanels(result.panels);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("Could not read the panels. Is the box reachable?");
      }
    };
    // RE-READ RATHER THAN COUNT UP LOCALLY. "12 min ago" was computed once, at mount, and then
    // sat there: leave the tab open and a panel that went silent an hour ago still reads as
    // twelve minutes, which is the exact failure this screen exists to catch. Re-asking the box
    // is also the only honest fix — `age_s` is computed THERE (see panelStatus.ts), because a
    // phone that has been asleep disagrees with the box by minutes, and a locally-ticked clock
    // would drift back into "last seen 4 minutes in the future".
    //
    // Foreground-gated and immediate-on-resume, the same shape `ConversationTab` above uses and for
    // the same reason: a backgrounded phone polling a box on a home network is battery spent to
    // refresh a screen nobody is looking at.
    if (!foreground) return;
    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [tick, foreground]);

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

/** How long "Saved." stays up. Long enough to be read by someone who was looking at the panel
 *  rather than the phone; short enough that it is gone before the next knob is touched, so it
 *  can never be read as confirming THAT one. It used to stay for the life of the screen. */
const SAVED_MS = 4_000;

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

  // Cleared on a timer rather than left up, and the timer is restarted by each save — so the
  // line always refers to the knob just turned. Cleared on unmount too: a `setSaved` after the
  // tab is switched away is a React warning and a leak, for a message nobody will see.
  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => setSaved(false), SAVED_MS);
    return () => clearTimeout(t);
  }, [saved]);

  async function save(next: EndpointSettings): Promise<void> {
    setBusy(true);
    setError(null);
    setSaved(false);
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

  if (error !== null && cfg === null) return <p className="jp-unit-error">{error}</p>;
  // A CARD-SHAPED HOLE, NOT NOTHING. This returned `null` while the settings were in flight, so
  // the knobs appeared a beat after the panel list had already painted and shoved every row down
  // the screen — under a thumb that was, by then, already moving towards one of them.
  if (cfg === null) {
    return (
      <div className="jp-audio jp-audio-wait" aria-busy="true">
        <h2>Every panel</h2>
        <p className="jp-unit-hint">Reading the settings…</p>
      </div>
    );
  }

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
      {saved && <p className="jp-unit-saved">Saved. Panels pick this up within 15 minutes.</p>}
      {error !== null && <p className="jp-unit-error">{error}</p>}
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

  if (error !== null && look === null) return <p className="jp-unit-error">{error}</p>;
  if (look === null) return <p className="jp-unit-hint">Reading…</p>;

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
    <div className="jp-unit-edit">
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
      <p className="jp-unit-hint">
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
      <p className="jp-unit-hint">
        What it comes back as after a restart. Four taps and a hold on the panel still swaps the
        body there and then.
      </p>

      <div className="jp-unit-actions">
        <button
          type="button"
          className="primary"
          disabled={!nameOk || busy}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
      {error !== null && <p className="jp-unit-error">{error}</p>}
    </div>
  );
}

/** How long a revoke stays armed. It never disarmed at all: an owner who tapped "revoke",
 *  thought better of it and put the phone down left a panel one stray tap from being retired —
 *  and the next tap on that card, minutes later, would not have looked like a confirmation of
 *  anything. Long enough to read the label and mean it, short enough that walking away is a
 *  cancellation, which is what walking away ought to mean. */
const ARMED_MS = 3_000;

/** One unit: what it is, what it last said, and the things the owner can do to it. */
function PanelRow({ panel, onChanged }: { panel: PanelStatusOut; onChanged: () => void }) {
  const [renaming, setRenaming] = useState(false);
  const [editingPet, setEditingPet] = useState(false);
  const [draft, setDraft] = useState(panel.name);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), ARMED_MS);
    return () => clearTimeout(t);
  }, [armed]);

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
  // `late` earns amber and the two worse states earn rose, which is the fleet card's own reading
  // of the same number — the two surfaces must not disagree about whether a panel is in trouble.
  const tone = state === "ok" ? "" : state === "late" ? " late" : " bad";
  const words = panelStateWords(state);

  return (
    <div className="jp-unit">
      <div className="jp-unit-head">
        <span className="jp-unit-name">{panel.name}</span>
        {/* Only a display is badged: every panel in this house is a pet, so marking both would
            put a word on every row that answers a question nobody asked. */}
        {panel.role === "display" && <span className="jp-unit-role">display</span>}
        {/* "never reported" rather than `agoLabel`'s bare "never": a panel flashed and never
            heard from is a DIFFERENT fault from one that reported and went quiet — check it was
            provisioned against this box at all — and the slot is wide enough to say so. */}
        <span className={`jp-unit-seen${tone}`}>
          {state === "never" ? "never reported" : agoLabel(panel.age_s)}
        </span>
      </div>
      {/* THE STATE IN WORDS, beside the facts rather than instead of them. The card used to say
          all of this by fading to 75% opacity — indistinguishable from a healthy panel, only
          greyer, and invisible to anyone reading it in sunlight. */}
      <p className="jp-unit-meta">
        <span className="jp-unit-version">{panel.version || "no firmware reported yet"}</span>
        {panel.report.screen ? ` · ${panel.report.screen}` : ""}
        {words ? (
          <>
            {" · "}
            <span className={`jp-unit-state${tone}`}>{words}</span>
          </>
        ) : null}
      </p>

      {renaming ? (
        <div className="jp-unit-edit">
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
          <p className="jp-unit-hint">
            Letters, digits, spaces, hyphens and full stops — {PANEL_NAME_MAX} at most. It is drawn
            on the other panel&rsquo;s screen, which has no other characters.
          </p>
          <div className="jp-unit-actions">
            <button
              type="button"
              className="primary"
              disabled={!nameOk || busy}
              onClick={() => void rename()}
            >
              {busy ? "Saving…" : "Save"}
            </button>
            <button type="button" onClick={() => setRenaming(false)}>
              Cancel
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
        <div className="jp-unit-actions">
          <button
            type="button"
            onClick={() => {
              setDraft(panel.name);
              setArmed(false);
              setRenaming(true);
            }}
          >
            Rename
          </button>
          {/* THE PANEL'S NAME AND THE PET'S NAME ARE DIFFERENT THINGS, and the labels have to
              carry that: "Rename" is which unit this is — the heading on the thread, what a
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
            Its pet
          </button>
          {/* ROSE FROM THE START, not only once armed. It was styled exactly like the two beside
              it, so the one irreversible control on the screen was the least distinguishable —
              arming it changes the weight and the label, which is what the second tap needs. */}
          <button
            type="button"
            className={armed ? "danger armed" : "danger"}
            disabled={busy}
            onClick={() => void revoke()}
          >
            {busy ? "Revoking…" : armed ? "Tap again to revoke" : "Revoke"}
          </button>
          {armed && !busy && (
            <button type="button" onClick={() => setArmed(false)}>
              Cancel
            </button>
          )}
        </div>
      )}
      {error && <p className="jp-unit-error">{error}</p>}
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
      {tab === "messages" && <ConversationTab />}
      {tab === "panels" && <PanelsTab />}
      {tab === "flash" && <EndpointsScreen />}
    </div>
  );
}
