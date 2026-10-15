// Cast: notices on the house's TVs (docs/plans/JPANEL_CAST_PLAN.md K2; the binding mock is
// docs/mocks/jpanel-cast/index.html, "1 · PWA"). Three tabs over one box service: the TVs and
// what each is doing, Send, and Sent. The box draws the frame and re-checks every TV at the
// instant of casting, so what this screen shows before Send is a forecast and the result sheet
// is the truth.

import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type CastNotice, type CastTv, api, castFrameUrl } from "../api/client";
import {
  BODY_MAX,
  SECONDS,
  SECONDS_DEFAULT,
  TITLE_MAX,
  canTakeDown,
  fitSize,
  outcomeLabel,
  outcomeSub,
  resultTitle,
  secondsLabel,
  sentSummary,
  skipReason,
  tvWords,
  utf8Bytes,
} from "../cast";
import { Sheet } from "../components/Sheet";
import { ImageIcon, MonitorIcon, RefreshIcon, SendIcon, XIcon } from "../components/icons";
import "./cast.css";

type Tab = "tvs" | "send" | "sent";

const TV_POLL_MS = 10_000;
const PREVIEW_DEBOUNCE_MS = 700;

/** The box's refusals, in words. */
const REFUSALS: Record<string, string> = {
  bad_field: "Give it a title.",
  too_long: "That's longer than a TV can show.",
  no_tvs: "Pick at least one TV.",
  bad_picture: "That picture couldn't be read.",
  picture_too_big: "That picture is too big to send.",
};

interface Picture {
  b64: string;
  name: string;
  thumb: string;
}

export function CastScreen() {
  const [tab, setTab] = useState<Tab>("tvs");
  const [tvs, setTvs] = useState<CastTv[] | null>(null);
  const [tvError, setTvError] = useState<string | null>(null);
  const [result, setResult] = useState<CastNotice | null>(null);

  const loadTvs = useCallback(async () => {
    try {
      const out = await api.castTvs();
      setTvs(out.tvs);
      setTvError(null);
    } catch (err) {
      setTvError(
        err instanceof ApiError && (err.status === 502 || err.status === 503)
          ? "The box can't reach its TV finder right now."
          : "Couldn't read the TVs.",
      );
    }
  }, []);

  useEffect(() => {
    void loadTvs();
    const t = setInterval(() => void loadTvs(), TV_POLL_MS);
    return () => clearInterval(t);
  }, [loadTvs]);

  return (
    <main className="screen-body cast-screen">
      <fieldset
        className="seg-row cast-seg"
        aria-label="Cast"
        style={
          { "--mode": "var(--steel)", "--mode-tint": "var(--steel-tint)" } as React.CSSProperties
        }
      >
        {(["tvs", "send", "sent"] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            className={`seg${tab === t ? " seg-on" : ""}`}
            aria-pressed={tab === t}
            onClick={() => setTab(t)}
          >
            {t === "tvs" ? "TVs" : t === "send" ? "Send" : "Sent"}
          </button>
        ))}
      </fieldset>

      {tab === "tvs" && (
        <TvsTab
          tvs={tvs}
          error={tvError}
          onRefresh={() => void loadTvs()}
          onSend={() => setTab("send")}
        />
      )}
      {tab === "send" && (
        <SendTab
          tvs={tvs}
          onSent={(n) => {
            setResult(n);
            void loadTvs();
          }}
        />
      )}
      {tab === "sent" && <SentTab onOpen={setResult} />}

      {result && (
        <ResultSheet notice={result} onChange={setResult} onClose={() => setResult(null)} />
      )}
    </main>
  );
}

function TvsTab({
  tvs,
  error,
  onRefresh,
  onSend,
}: {
  tvs: CastTv[] | null;
  error: string | null;
  onRefresh: () => void;
  onSend: () => void;
}) {
  return (
    <>
      <div className="cast-head">
        <span className="muted">
          {tvs === null ? (error ? "" : "Looking for TVs…") : `${tvs.length} found on the network`}
        </span>
        <button type="button" className="cast-ghost" onClick={onRefresh}>
          <RefreshIcon size={16} /> Refresh
        </button>
      </div>
      {error && <p className="cast-error">{error}</p>}
      {tvs && tvs.length > 0 && (
        <section className="cast-card">
          {tvs.map((tv) => {
            const w = tvWords(tv);
            return (
              <div key={tv.id} className="cast-tv">
                <span className="cast-tv-ic" aria-hidden="true">
                  <MonitorIcon size={20} />
                </span>
                <span className="cast-tv-meta">
                  <span className="cast-tv-name">{tv.name}</span>
                  <span className="cast-tv-sub">{w.sub}</span>
                </span>
                <span className={`cast-state cast-${w.tone}`}>
                  <span className="cast-dot" aria-hidden="true" />
                  {w.label}
                </span>
              </div>
            );
          })}
        </section>
      )}
      {tvs && tvs.length === 0 && (
        <p className="muted cast-empty">
          No TVs found yet. A TV shows up here once it is on the same network as the box.
        </p>
      )}
      <div className="cast-label">Rules</div>
      <section className="cast-card cast-rules">
        <p>
          <b>Only an idle TV.</b> Ambient, screensaver or a home screen with nothing playing.
          Anything else is skipped, never covered.
        </p>
        <p>
          <b>Off is fine.</b> Casting turns it on and shows the notice. It stays on afterwards,
          because a cast can't turn a TV off.
        </p>
      </section>
      <button type="button" className="cast-primary cast-fab" onClick={onSend}>
        <SendIcon size={18} /> Send to TVs
      </button>
    </>
  );
}

function SendTab({ tvs, onSent }: { tvs: CastTv[] | null; onSent: (n: CastNotice) => void }) {
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [picture, setPicture] = useState<Picture | null>(null);
  const [seconds, setSeconds] = useState<number>(SECONDS_DEFAULT);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Every TV that would take it is picked to start with; the owner unpicks.
  const chosen = picked ?? new Set((tvs ?? []).filter((t) => t.castable).map((t) => t.id));
  const targets = (tvs ?? []).filter((t) => chosen.has(t.id));
  const willTake = targets.filter((t) => t.castable).length;
  const skips = targets.map(skipReason).filter((r): r is string => r !== null);
  const titleOver = utf8Bytes(title.trim()) > TITLE_MAX;
  const bodyOver = utf8Bytes(body.trim()) > BODY_MAX;
  const ready = title.trim() !== "" && !titleOver && !bodyOver && targets.length > 0;

  useEffect(() => {
    if (!title.trim() || titleOver || bodyOver) {
      setPreview(null);
      return;
    }
    let stale = false;
    let url: string | null = null;
    const t = setTimeout(() => {
      api
        .castPreview({ title, body, picture_b64: picture?.b64 ?? null })
        .then((blob) => {
          if (stale) return;
          url = URL.createObjectURL(blob);
          setPreview(url);
          setPreviewFailed(false);
        })
        .catch(() => {
          if (stale) return;
          setPreview(null);
          setPreviewFailed(true);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      stale = true;
      clearTimeout(t);
      if (url) URL.revokeObjectURL(url);
    };
  }, [title, body, picture, titleOver, bodyOver]);

  function toggle(id: string) {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPicked(next);
  }

  async function onFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      setPicture(await loadPicture(file));
      setProblem(null);
    } catch {
      setProblem(REFUSALS.bad_picture ?? null);
    }
  }

  async function send() {
    setSending(true);
    setProblem(null);
    try {
      const out = await api.castSend({
        tv_ids: targets.map((t) => t.id),
        title,
        body,
        picture_b64: picture?.b64 ?? null,
        seconds,
      });
      if ("sent" in out) onSent(out.sent);
      else setProblem(REFUSALS[out.refused.reason] ?? `The box said no (${out.refused.reason}).`);
    } catch (err) {
      setProblem(
        err instanceof ApiError && (err.status === 502 || err.status === 503)
          ? "The box can't reach the TVs right now."
          : "That didn't send. Try again.",
      );
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <div className="cast-label">To</div>
      {tvs === null ? (
        <p className="muted">Looking for TVs…</p>
      ) : (
        <fieldset className="cast-targets" aria-label="TVs">
          {tvs.map((tv) => (
            <button
              key={tv.id}
              type="button"
              className={`cast-target${chosen.has(tv.id) ? " on" : ""}${tv.castable ? "" : " busy"}`}
              aria-pressed={chosen.has(tv.id)}
              onClick={() => toggle(tv.id)}
            >
              <span className={`cast-dot cast-${tvWords(tv).tone}`} aria-hidden="true" />
              {tv.name}
            </button>
          ))}
        </fieldset>
      )}
      {skips.map((s) => (
        <p key={s} className="cast-note">
          {s}
        </p>
      ))}

      <div className="cast-label">Message</div>
      <label className="cast-field">
        <span>Title</span>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Dinner's ready"
          enterKeyHint="next"
        />
      </label>
      {titleOver && <p className="cast-error">That title is too long for the screen.</p>}
      <label className="cast-field">
        <span>Text</span>
        <textarea
          value={body}
          rows={3}
          onChange={(e) => setBody(e.target.value)}
          placeholder="Optional"
        />
      </label>
      {bodyOver && <p className="cast-error">That's more text than a TV can show.</p>}
      <div className="cast-attach">
        {picture ? (
          <span className="cast-thumb">
            <img src={picture.thumb} alt="" />
            <span className="cast-thumb-name">{picture.name}</span>
            <button
              type="button"
              className="cast-icon-btn"
              aria-label="Remove the picture"
              onClick={() => setPicture(null)}
            >
              <XIcon size={16} />
            </button>
          </span>
        ) : (
          <button type="button" className="cast-ghost" onClick={() => fileRef.current?.click()}>
            <ImageIcon size={16} /> Add a picture
          </button>
        )}
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          hidden
          data-testid="cast-file"
          onChange={(e) => void onFile(e)}
        />
        <label className="cast-seconds">
          <span>On screen</span>
          <select value={seconds} onChange={(e) => setSeconds(Number(e.target.value))}>
            {SECONDS.map((s) => (
              <option key={s} value={s}>
                {secondsLabel(s)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="cast-label">On the TV · {secondsLabel(seconds)}</div>
      <div className="cast-screen-16x9">
        {preview ? (
          <img src={preview} alt="What the TV will show" />
        ) : (
          <span className="muted">
            {!title.trim()
              ? "Type a title to see it"
              : previewFailed
                ? "Couldn't draw the preview"
                : "Drawing…"}
          </span>
        )}
      </div>

      {problem && <p className="cast-error">{problem}</p>}
      <button
        type="button"
        className="cast-primary cast-fab"
        disabled={!ready || sending}
        onClick={() => void send()}
      >
        <SendIcon size={18} />
        {sending
          ? "Casting…"
          : willTake === targets.length
            ? `Cast to ${tvCount(targets.length)}`
            : `Cast to ${willTake} of ${tvCount(targets.length)}`}
      </button>
    </>
  );
}

function tvCount(n: number): string {
  return n === 1 ? "1 TV" : `${n} TVs`;
}

function SentTab({ onOpen }: { onOpen: (n: CastNotice) => void }) {
  const [notices, setNotices] = useState<CastNotice[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let stale = false;
    api
      .castSent()
      .then((out) => {
        if (!stale) setNotices(out.notices);
      })
      .catch(() => {
        if (!stale) setError(true);
      });
    return () => {
      stale = true;
    };
  }, []);

  if (error) return <p className="cast-error">Couldn't read what was sent.</p>;
  if (notices === null) return <p className="muted">Loading…</p>;
  if (notices.length === 0) {
    return <p className="muted cast-empty">Nothing cast this week.</p>;
  }
  return (
    <>
      <section className="cast-card">
        {notices.map((n) => (
          <button key={n.id} type="button" className="cast-sent" onClick={() => onOpen(n)}>
            <img className="cast-mini" src={castFrameUrl(n.id)} alt="" loading="lazy" />
            <span className="cast-tv-meta">
              <span className="cast-tv-name">{n.title}</span>
              <span className="cast-tv-sub">{sentSummary(n)}</span>
            </span>
          </button>
        ))}
      </section>
      <p className="muted cast-hint">Kept for a week.</p>
    </>
  );
}

function ResultSheet({
  notice,
  onChange,
  onClose,
}: {
  notice: CastNotice;
  onChange: (n: CastNotice) => void;
  onClose: () => void;
}) {
  const [stopping, setStopping] = useState(false);
  const [stopped, setStopped] = useState<string | null>(null);

  async function takeDown() {
    setStopping(true);
    try {
      const out = await api.castStop(notice.id);
      const down = Object.values(out.stopped).filter(Boolean).length;
      setStopped(
        down > 0
          ? `Taken down on ${tvCount(down)}.`
          : "It had already gone, or someone took the TV over.",
      );
      onChange({ ...notice, stopped_at: new Date().toISOString() });
    } catch {
      setStopped("Couldn't reach the TVs to take it down.");
    } finally {
      setStopping(false);
    }
  }

  return (
    <Sheet title={resultTitle(notice)} onClose={onClose}>
      <p className="cast-lead">
        "{notice.title}" · on screen for {secondsLabel(notice.seconds)}
      </p>
      <div className="cast-results">
        {notice.tvs.map((t) => (
          <div key={t.tv_id} className="cast-res">
            <span className={`cast-res-tag cast-res-${t.outcome}`}>{outcomeLabel(t.outcome)}</span>
            <span className="cast-tv-meta">
              <span className="cast-tv-name">{t.name}</span>
              <span className="cast-tv-sub">{outcomeSub(t)}</span>
            </span>
          </div>
        ))}
      </div>
      {stopped && <p className="cast-note">{stopped}</p>}
      {canTakeDown(notice) && (
        <button
          type="button"
          className="cast-danger"
          disabled={stopping}
          onClick={() => void takeDown()}
        >
          {stopping ? "Taking it down…" : "Take it down"}
        </button>
      )}
    </Sheet>
  );
}

/** The photo as the box wants it: no bigger than the TV draws it, as JPEG, base64. */
async function loadPicture(file: File): Promise<Picture> {
  const bmp = await createImageBitmap(file);
  try {
    const size = fitSize(bmp.width, bmp.height);
    const canvas = document.createElement("canvas");
    canvas.width = size.w;
    canvas.height = size.h;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no canvas");
    ctx.drawImage(bmp, 0, 0, size.w, size.h);
    const url = canvas.toDataURL("image/jpeg", 0.88);
    return { b64: url.slice(url.indexOf(",") + 1), name: file.name || "picture", thumb: url };
  } finally {
    bmp.close();
  }
}
