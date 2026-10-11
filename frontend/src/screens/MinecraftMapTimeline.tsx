// The Minecraft map's Timeline tab (plan §M8a; binding mock docs/mocks/minecraft-map/
// a-full-bleed.html, session navigation "3 · Days"): a strip of the last week's days with a
// dot per player who played, that day's sessions as chips, the transport, the scrubber with
// its session bands and event glyphs, then the range presets and who's shown one drag
// further. Every session ever is in the Sessions sheet (alternative 1's list), reached from
// a player's Sessions button or the list button here.
//
// The screen owns the state and the moving parts: the scrubbed moment changes every frame
// while playing, so the thumb, the fill and the clock are moved through refs rather than
// a re-render per frame.

import { type KeyboardEvent, type PointerEvent, type Ref, useRef } from "react";
import type { MinecraftSession, MinecraftTimelineEvent } from "../api/client";
import { Sheet } from "../components/Sheet";
import {
  ListIcon,
  LocateIcon,
  LogInIcon,
  LogOutIcon,
  PauseIcon,
  PlayIcon,
  PortalIcon,
  SkullIcon,
} from "../components/icons";
import {
  PRESETS,
  type Preset,
  type RosterEntry,
  type StripDay,
  clamp,
  dayStart,
  eventWords,
  fmtClock,
  fmtDay,
  fmtDur,
  fmtKm,
  fmtShortDay,
  fmtSpan,
  initialOf,
  scrubMarks,
  sessionDistance,
  sessionEnd,
  sessionGroups,
} from "../minecraftMap";

export interface TimelineState {
  on: boolean;
  /** Pinned to now: the scrubbed moment follows the clock and the status may say Live. */
  live: boolean;
  from: number;
  to: number;
  playing: boolean;
  speed: number;
  follow: boolean;
  /** The player Follow follows: the loaded session's, or one picked on the map. */
  focus: string | null;
  /** The loaded session's id. */
  cur: string | null;
  preset: Preset | null;
  /** Players switched off under "Who's shown". */
  hidden: string[];
}

export interface ScrubRefs {
  thumb: Ref<HTMLDivElement>;
  fill: Ref<HTMLDivElement>;
  clock: Ref<HTMLElement>;
  day: Ref<HTMLSpanElement>;
  track: Ref<HTMLDivElement>;
}

const pct = (t: number, from: number, to: number) =>
  clamp(((t - from) / Math.max(1, to - from)) * 100, 0, 100);

export function Disc({ entry }: { entry: RosterEntry | undefined }) {
  return (
    <span
      className="mc-pdisc"
      style={{ background: `var(${entry?.token ?? "--steel"})` }}
      aria-hidden="true"
    >
      {initialOf(entry?.gamertag ?? "?")}
    </span>
  );
}

export function TimelinePanel({
  tl,
  t,
  now,
  days,
  sessions,
  events,
  roster,
  trackWidth,
  refs,
  scrubEl,
  onDay,
  onLoad,
  onPlay,
  onSpeed,
  onFollow,
  onNow,
  onScrub,
  onMark,
  onPreset,
  onToggle,
  onSessions,
}: {
  tl: TimelineState;
  /** The scrubbed moment as of the last render (the refs move between renders). */
  t: number;
  now: number;
  days: StripDay[];
  sessions: MinecraftSession[];
  events: MinecraftTimelineEvent[];
  roster: RosterEntry[];
  trackWidth: number;
  refs: ScrubRefs;
  scrubEl: Ref<HTMLDivElement>;
  onDay: (d: StripDay) => void;
  onLoad: (s: MinecraftSession) => void;
  onPlay: () => void;
  onSpeed: () => void;
  onFollow: () => void;
  onNow: () => void;
  onScrub: (t: number) => void;
  onMark: (e: MinecraftTimelineEvent) => void;
  onPreset: (p: Preset) => void;
  onToggle: (xuid: string) => void;
  onSessions: () => void;
}) {
  const entry = (xuid: string) => roster.find((r) => r.xuid === xuid);
  const today = dayStart(t);
  const day = days.find((d) => d.start === today) ?? null;
  const shown = (xuid: string) => !tl.hidden.includes(xuid);
  const focus = tl.focus ? entry(tl.focus) : undefined;
  const bands = sessions.filter(
    (s) => shown(s.xuid) && sessionEnd(s, now) >= tl.from && s.start <= tl.to,
  );
  const marks = scrubMarks(
    events.filter((e) => shown(e.xuid)),
    tl.from,
    tl.to,
    trackWidth,
  );
  const drag = useRef(false);

  function at(e: PointerEvent<HTMLDivElement>): number {
    const r = e.currentTarget.getBoundingClientRect();
    const f = r.width > 0 ? clamp((e.clientX - r.left) / r.width, 0, 1) : 0;
    return tl.from + f * (tl.to - tl.from);
  }

  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const span = tl.to - tl.from;
    const step: Record<string, number> = {
      ArrowRight: span / 50,
      ArrowLeft: -span / 50,
      PageUp: span / 10,
      PageDown: -span / 10,
    };
    if (e.key in step) onScrub(clamp(t + (step[e.key] ?? 0), tl.from, tl.to));
    else if (e.key === "Home") onScrub(tl.from);
    else if (e.key === "End") onScrub(tl.to);
    else return;
    e.preventDefault();
  }

  return (
    <div className="mc-tl">
      {days.length > 0 && (
        <div className="mc-dy" role="radiogroup" aria-label="Day">
          {days.map((d) => {
            const who = d.who.map((x) => entry(x)?.gamertag ?? x);
            const n = d.sessions.length;
            return (
              <button
                key={d.start}
                type="button"
                // biome-ignore lint/a11y/useSemanticElements: a styled day cell; a native radio can't carry this layout.
                role="radio"
                aria-checked={day?.start === d.start}
                className="mc-dy-d"
                disabled={!n}
                onClick={() => onDay(d)}
                aria-label={`${fmtDay(d.start, now)}${d.today ? `, ${fmtShortDay(d.start)}` : ""} — ${
                  n
                    ? `${n} ${n === 1 ? "session" : "sessions"}: ${who.join(", ")}`
                    : "nobody played"
                }`}
              >
                <span className="mc-dy-w">{d.label}</span>
                <span className="mc-dy-n">{d.date}</span>
                <span className="mc-dy-dots" aria-hidden="true">
                  {d.who.map((x) => (
                    <i key={x} style={{ background: `var(${entry(x)?.token ?? "--steel"})` }} />
                  ))}
                </span>
              </button>
            );
          })}
        </div>
      )}
      <fieldset className="mc-dc" aria-label="Sessions that day">
        {day?.sessions.length ? (
          day.sessions.map((s) => (
            <button
              key={s.id}
              type="button"
              className="mc-dc-c"
              aria-pressed={tl.cur === s.id}
              onClick={() => onLoad(s)}
              aria-label={`${s.gamertag}, ${fmtSpan(s.start, s.end)}, ${fmtDur(sessionEnd(s, now) - s.start)}${
                s.deaths ? `, ${s.deaths} ${s.deaths === 1 ? "death" : "deaths"}` : ""
              }`}
            >
              <Disc entry={entry(s.xuid)} />
              <span className="mc-dc-tx">
                <b>{fmtClock(s.start)}</b>
                {s.end === null ? (
                  <span className="on">on now</span>
                ) : (
                  <span>{fmtDur(s.end - s.start)}</span>
                )}
              </span>
            </button>
          ))
        ) : (
          <span className="mc-dc-none">
            {sessions.length ? "Nobody played that day." : "No sessions in this world yet."}
          </span>
        )}
      </fieldset>

      <div className="mc-tl-top">
        <button
          type="button"
          className="mc-tl-play"
          aria-label={tl.playing ? "Pause" : "Play"}
          onClick={onPlay}
        >
          {tl.playing ? <PauseIcon size={18} /> : <PlayIcon size={18} />}
        </button>
        <button
          type="button"
          className="mc-tl-speed"
          aria-label={`Playback speed ${tl.speed} times — tap to change`}
          onClick={onSpeed}
        >
          {tl.speed}×
        </button>
        <div className="mc-tl-when">
          <b ref={refs.clock}>{fmtClock(t)}</b>
          <span ref={refs.day}>{fmtDay(t, now)}</span>
        </div>
        <button
          type="button"
          className="mc-tl-follow"
          aria-pressed={tl.follow}
          disabled={!focus}
          aria-label={focus ? `Follow ${focus.gamertag}` : "Follow — load a session first"}
          onClick={onFollow}
        >
          <LocateIcon size={16} />
          <span className="fl">Follow</span>
        </button>
        <button type="button" className="mc-tl-now" aria-pressed={tl.live} onClick={onNow}>
          {tl.live ? (
            <>
              <span className="mc-dot mc-dot-ok" aria-hidden="true" />
              Live
            </>
          ) : (
            "Now"
          )}
        </button>
      </div>

      <div className="mc-tl-scrub" ref={scrubEl}>
        <div className="mc-tl-marks">
          {marks.map((m) => {
            const lead = m.lead;
            const cls =
              lead.kind === "death" ? " death" : lead.kind === "dimension" ? " dimension" : "";
            const what = m.events.map(eventWords).join("; ");
            return (
              <button
                key={`${lead.kind}-${lead.at}-${lead.xuid}`}
                type="button"
                className={`mc-tl-mk${cls}`}
                style={{ left: `clamp(22px, ${pct(lead.at, tl.from, tl.to)}%, calc(100% - 22px))` }}
                aria-label={`${fmtClock(lead.at)} — ${what}`}
                onClick={() => onMark(lead)}
              >
                {lead.kind === "death" ? (
                  <SkullIcon size={16} />
                ) : lead.kind === "dimension" ? (
                  <PortalIcon size={16} />
                ) : lead.kind === "join" ? (
                  <LogInIcon size={16} />
                ) : (
                  <LogOutIcon size={16} />
                )}
                {m.events.length > 1 && <span className="n">{m.events.length}</span>}
              </button>
            );
          })}
        </div>
        <div
          className="mc-tl-track"
          ref={refs.track}
          role="slider"
          tabIndex={0}
          aria-label="Time"
          aria-valuemin={Math.round(tl.from)}
          aria-valuemax={Math.round(tl.to)}
          aria-valuenow={Math.round(t)}
          aria-valuetext={`${fmtDay(t, now)} ${fmtClock(t)}`}
          onKeyDown={onKey}
          onPointerDown={(e) => {
            drag.current = true;
            e.currentTarget.setPointerCapture?.(e.pointerId);
            onScrub(at(e));
          }}
          onPointerMove={(e) => {
            if (drag.current) onScrub(at(e));
          }}
          onPointerUp={() => {
            drag.current = false;
          }}
          onPointerCancel={() => {
            drag.current = false;
          }}
        >
          <div className="mc-tl-rail" />
          {bands.map((s) => (
            <span
              key={s.id}
              className="mc-tl-band"
              style={{
                left: `${pct(s.start, tl.from, tl.to)}%`,
                width: `${Math.max(0.6, pct(sessionEnd(s, now), tl.from, tl.to) - pct(s.start, tl.from, tl.to))}%`,
                background: `var(${entry(s.xuid)?.token ?? "--steel"})`,
              }}
            />
          ))}
          <div
            className="mc-tl-fill"
            ref={refs.fill}
            style={{ width: `${pct(t, tl.from, tl.to)}%` }}
          />
          <div
            className="mc-tl-thumb"
            ref={refs.thumb}
            style={{ left: `${pct(t, tl.from, tl.to)}%` }}
          />
        </div>
      </div>

      <div className="mc-tl-more">
        <h3 className="mc-sect">Range and who&apos;s shown</h3>
        <button type="button" className="mc-link mc-tl-list" onClick={onSessions}>
          <ListIcon size={16} />
          All sessions
        </button>
      </div>
      <div className="mc-seg mc-tl-range" role="radiogroup" aria-label="Range">
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            // biome-ignore lint/a11y/useSemanticElements: a styled segment; a native radio can't carry this 44px button layout.
            role="radio"
            aria-checked={!tl.cur && tl.preset === p.id}
            aria-label={p.name}
            onClick={() => onPreset(p.id)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <fieldset className="mc-chips" aria-label="Who's shown">
        {roster.map((r) => (
          <button
            key={r.xuid}
            type="button"
            className="mc-chip"
            aria-pressed={shown(r.xuid)}
            onClick={() => onToggle(r.xuid)}
          >
            <span className="pd" style={{ background: `var(${r.token})` }} aria-hidden="true" />
            {r.gamertag}
          </button>
        ))}
      </fieldset>
    </div>
  );
}

/** Alternative 1's list: every session of the world, newest first, by day. */
export function SessionsSheet({
  world,
  sessions,
  roster,
  who,
  cur,
  now,
  onWho,
  onLoad,
  onClose,
}: {
  world: string;
  sessions: MinecraftSession[];
  roster: RosterEntry[];
  who: string | null;
  cur: string | null;
  now: number;
  onWho: (xuid: string | null) => void;
  onLoad: (s: MinecraftSession) => void;
  onClose: () => void;
}) {
  const groups = sessionGroups(sessions, who, now);
  const count = (x: string | null) => sessions.filter((s) => x === null || s.xuid === x).length;
  const entry = (xuid: string) => roster.find((r) => r.xuid === xuid);
  return (
    <Sheet title="Sessions" onClose={onClose}>
      <p className="mc-note mc-sl-note">{world} · newest first</p>
      <div className="mc-chips mc-sl-who" role="radiogroup" aria-label="Whose sessions">
        {[null, ...roster.map((r) => r.xuid)].map((x) => {
          const r = x ? entry(x) : undefined;
          return (
            <button
              key={x ?? "all"}
              type="button"
              className="mc-chip"
              // biome-ignore lint/a11y/useSemanticElements: a styled chip; a native radio can't carry this layout.
              role="radio"
              aria-checked={who === x}
              onClick={() => onWho(x)}
            >
              {r && (
                <span className="pd" style={{ background: `var(${r.token})` }} aria-hidden="true" />
              )}
              {r ? r.gamertag : "All players"} <span className="ct">{count(x)}</span>
            </button>
          );
        })}
      </div>
      {groups.length === 0 && <p className="mc-note">No sessions yet.</p>}
      {groups.map((g) => (
        <section key={g.start} className="mc-sl-group">
          <div className="mc-sl-day">
            {g.label}
            <span>
              {g.list.length} {g.list.length === 1 ? "session" : "sessions"}
            </span>
          </div>
          <div className="mc-sl-card">
            {g.list.map((s) => (
              <button
                key={s.id}
                type="button"
                className="mc-sl-row"
                aria-current={cur === s.id}
                onClick={() => onLoad(s)}
              >
                <Disc entry={entry(s.xuid)} />
                <span className="mc-sl-tx">
                  <span className="mc-sl-t">
                    {fmtSpan(s.start, s.end)}
                    {s.end === null && <span className="badge ok">on now</span>}
                  </span>
                  <span className="mc-sl-m">
                    {who === null ? `${s.gamertag} · ` : ""}
                    {fmtDur(sessionEnd(s, now) - s.start)} · {fmtKm(sessionDistance(s))}
                  </span>
                </span>
                <span className="mc-sl-ic">
                  {s.dims.includes("nether") && (
                    <span className="nth" title="Went to the Nether">
                      <PortalIcon size={16} />
                      <span className="mc-sr">Nether</span>
                    </span>
                  )}
                  {s.deaths > 0 && (
                    <span className="dth">
                      <SkullIcon size={16} />
                      {s.deaths}
                      <span className="mc-sr">{s.deaths === 1 ? " death" : " deaths"}</span>
                    </span>
                  )}
                </span>
              </button>
            ))}
          </div>
        </section>
      ))}
    </Sheet>
  );
}
