// Cast: notices on the house's TVs (docs/plans/JPANEL_CAST_PLAN.md K2; the binding mock is
// docs/mocks/jpanel-cast/index.html, "1 · PWA"). Three tabs over one box service: the TVs and
// what each is doing, Send, and Sent. The box draws the frame and re-checks every TV at the
// instant of casting, so what this screen shows before Send is a forecast and the result sheet
// is the truth. Send is a touch editor over a 16:9 canvas: Add text and Add image put a layer
// on it, and the owner drags, pinches and twists each into place; the phone draws the frame the
// TV gets, so the preview is exactly what lands.

import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { ApiError, type CastNotice, type CastTv, api, castFrameUrl } from "../api/client";
import {
  type CastDraft,
  type Layer,
  PHOTO_WORKING_MAX,
  type Rect,
  SECONDS,
  SECONDS_DEFAULT,
  TV_H,
  TV_W,
  WORDS_MAX,
  type WordsFit,
  addPicture,
  addWords,
  canAdd,
  canTakeDown,
  coverZoom,
  draftReady,
  draftTitle,
  drawFrame,
  emptyCastDraft,
  fitNote,
  handleAt,
  layerAt,
  layerById,
  layerRect,
  layerScale,
  moveLayer,
  outcomeLabel,
  outcomeSub,
  removeLayer,
  resultTitle,
  scaleLayer,
  secondsLabel,
  sentSummary,
  setStyle,
  setWords,
  skipReason,
  turnLayer,
  tvWords,
  updateLayer,
  utf8Bytes,
} from "../cast";
import { Sheet } from "../components/Sheet";
import {
  FitIcon,
  ImageIcon,
  MonitorIcon,
  PencilIcon,
  RefreshIcon,
  SendIcon,
  TrashIcon,
} from "../components/icons";
import {
  type Photo,
  type Point,
  TEXT_STYLES,
  type TextStyle,
  clipBytes,
  workingSize,
} from "../panelNotice";
import "./cast.css";
import "./panelNotice.css";

type Tab = "tvs" | "send" | "sent";

const TV_POLL_MS = 10_000;

/** The box's refusals, in words. */
const REFUSALS: Record<string, string> = {
  bad_field: "Give it a title.",
  too_long: "That's longer than a TV can show.",
  no_tvs: "Pick at least one TV.",
  bad_picture: "That picture couldn't be read.",
  picture_too_big: "That picture is too big to send.",
};

export function CastScreen() {
  const [tab, setTab] = useState<Tab>("tvs");
  const [tvs, setTvs] = useState<CastTv[] | null>(null);
  const [tvError, setTvError] = useState<string | null>(null);
  const [result, setResult] = useState<CastNotice | null>(null);
  // Held here, not in Send, so a look at the TVs or Sent doesn't lose what was composed.
  const [draft, setDraftState] = useState<CastDraft>(emptyCastDraft);
  const setDraft = useCallback((fn: (d: CastDraft) => CastDraft) => setDraftState(fn), []);

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
          draft={draft}
          setDraft={setDraft}
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

function SendTab({
  tvs,
  draft,
  setDraft,
  onSent,
}: {
  tvs: CastTv[] | null;
  draft: CastDraft;
  setDraft: (fn: (d: CastDraft) => CastDraft) => void;
  onSent: (n: CastNotice) => void;
}) {
  const [picked, setPicked] = useState<Set<string> | null>(null);
  // The layer held on the canvas, outlined with its handle, that the bar under it acts on.
  const [held, setHeld] = useState<number | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [fits, setFits] = useState<Map<number, WordsFit>>(new Map());
  const [seconds, setSeconds] = useState<number>(SECONDS_DEFAULT);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  // Whether the picture chosen next is added, or replaces the held one.
  const replacing = useRef<number | null>(null);
  const [wrap, width] = useWidth();

  const layer = layerById(draft, held);
  // Every TV that would take it is picked to start with; the owner unpicks.
  const chosen = picked ?? new Set((tvs ?? []).filter((t) => t.castable).map((t) => t.id));
  const targets = (tvs ?? []).filter((t) => chosen.has(t.id));
  const willTake = targets.filter((t) => t.castable).length;
  const skips = targets.map(skipReason).filter((r): r is string => r !== null);
  const ready = draftReady(draft) && targets.length > 0;
  const room = canAdd(draft);
  const note = layer?.kind === "words" ? fitNote(fits.get(layer.id), layer.size) : null;

  function toggle(id: string) {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPicked(next);
  }

  function choosePicture(replace: number | null) {
    replacing.current = replace;
    file.current?.click();
  }

  async function onFile(f: File) {
    try {
      const photo = await loadPhoto(f);
      const replace = replacing.current;
      if (replace !== null) {
        setDraft((d) =>
          updateLayer(d, replace, (l) => (l.kind === "picture" ? { ...l, photo } : l)),
        );
      } else {
        setHeld(draft.nextId);
        setDraft((d) => addPicture(d, photo));
      }
      setProblem(null);
    } catch {
      setProblem(REFUSALS.bad_picture ?? null);
    }
  }

  function saveWords(e: Editing) {
    if (e.id === null) {
      if (e.text.trim()) {
        setHeld(draft.nextId);
        setDraft((d) => addWords(d, e.text, e.style));
      }
    } else {
      const id = e.id;
      if (!e.text.trim()) setHeld(null);
      setDraft((d) => setStyle(setWords(d, id, e.text), id, e.style));
    }
    setEditing(null);
  }

  function editWords(id: number) {
    const l = layerById(draft, id);
    if (l?.kind === "words") setEditing({ id, text: l.text, style: l.style });
  }

  async function send() {
    setSending(true);
    setProblem(null);
    try {
      const out = await api.castSend({
        tv_ids: targets.map((t) => t.id),
        title: draftTitle(draft),
        frame_b64: exportFrame(draft),
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
      <div className="cast-stage" ref={wrap}>
        {width > 0 && (
          <FrameCanvas
            draft={draft}
            cssW={width}
            held={held}
            onHold={setHeld}
            onEdit={editWords}
            onDraft={setDraft}
            onFits={setFits}
          />
        )}
        {draft.layers.length === 0 && (
          <p className="cast-stage-empty">Add text or a picture to start</p>
        )}
      </div>

      <div className="cast-add">
        <button
          type="button"
          className="cast-add-btn"
          disabled={!room}
          onClick={() => setEditing({ id: null, text: "", style: "outline" })}
        >
          <span className="cast-add-aa" aria-hidden="true">
            Aa
          </span>
          Add text
        </button>
        <button
          type="button"
          className="cast-add-btn"
          disabled={!room}
          onClick={() => choosePicture(null)}
        >
          <ImageIcon size={20} />
          Add image
        </button>
        <input
          ref={file}
          type="file"
          accept="image/*"
          hidden
          data-testid="cast-file"
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void onFile(f);
          }}
        />
      </div>

      {layer && (
        <fieldset
          className="cast-layer-bar"
          aria-label={layer.kind === "words" ? "Selected text" : "Selected image"}
        >
          {layer.kind === "words" ? (
            <>
              <button type="button" className="cast-chip" onClick={() => editWords(layer.id)}>
                <PencilIcon size={16} /> Edit
              </button>
              {(Object.keys(TEXT_STYLES) as TextStyle[]).map((key) => {
                const st = TEXT_STYLES[key];
                return (
                  <button
                    key={key}
                    type="button"
                    className={`cast-swatch${layer.style === key ? " on" : ""}`}
                    aria-label={st.label}
                    aria-pressed={layer.style === key}
                    onClick={() => setDraft((d) => setStyle(d, layer.id, key))}
                    style={{
                      color: st.fill,
                      background: st.plate ?? "#3a4a5c",
                      WebkitTextStroke: st.stroke ? `1px ${st.stroke}` : undefined,
                    }}
                  >
                    Aa
                  </button>
                );
              })}
            </>
          ) : (
            <>
              <button type="button" className="cast-chip" onClick={() => choosePicture(layer.id)}>
                <ImageIcon size={16} /> Replace
              </button>
              <button
                type="button"
                className="cast-chip"
                onClick={() =>
                  setDraft((d) =>
                    updateLayer(d, layer.id, (l) =>
                      l.kind === "picture"
                        ? { ...l, zoom: coverZoom(l.photo), x: TV_W / 2, y: TV_H / 2, turn: 0 }
                        : l,
                    ),
                  )
                }
              >
                <FitIcon size={16} /> Fill screen
              </button>
            </>
          )}
          <button
            type="button"
            className="cast-chip cast-chip-danger"
            aria-label="Delete"
            onClick={() => {
              setDraft((d) => removeLayer(d, layer.id));
              setHeld(null);
            }}
          >
            <TrashIcon size={18} />
          </button>
        </fieldset>
      )}
      {note && <p className="cast-note">{note}</p>}
      {draft.layers.length > 0 && (
        <p className="cast-note">
          {layer
            ? "Drag to move it. Pinch to resize, twist to rotate, or pull its corner."
            : "Tap something on the screen to move, resize or rotate it."}
          {layer?.kind === "words" ? " Double-tap to change the words." : ""}
        </p>
      )}
      {!room && <p className="cast-note">That's as many as one notice holds.</p>}

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

      <div className="cast-label">On screen for</div>
      <fieldset className="cast-targets" aria-label="On screen for">
        {SECONDS.map((s) => (
          <button
            key={s}
            type="button"
            className={`cast-target${seconds === s ? " on" : ""}`}
            aria-pressed={seconds === s}
            onClick={() => setSeconds(s)}
          >
            {secondsLabel(s)}
          </button>
        ))}
      </fieldset>

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

      {editing && (
        <WordsSheet editing={editing} onSave={saveWords} onClose={() => setEditing(null)} />
      )}
    </>
  );
}

/** Words being written: new (`id` null) or a layer's, with the style they'll be drawn in. */
interface Editing {
  id: number | null;
  text: string;
  style: TextStyle;
}

/** Writing the words, in a sheet so the phone's keyboard has the room: the field takes focus at
 *  once, and Enter or Done puts them on the screen. */
function WordsSheet({
  editing,
  onSave,
  onClose,
}: {
  editing: Editing;
  onSave: (e: Editing) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState(editing.text);
  const [style, setStyle] = useState<TextStyle>(editing.style);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
  }, []);
  const save = () => onSave({ ...editing, text, style });
  return (
    <Sheet title={editing.id === null ? "Add text" : "Edit text"} onClose={onClose}>
      <form
        className="cast-words-form"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <label className="cast-field">
          <span className="pn-field-head">
            <span>Words</span>
            <span className="pn-count">
              {utf8Bytes(text)} / {WORDS_MAX}
            </span>
          </span>
          <input
            ref={input}
            type="text"
            value={text}
            placeholder="Dinner's ready"
            enterKeyHint="done"
            onChange={(e) => setText(clipBytes(e.target.value, WORDS_MAX))}
          />
        </label>
        <fieldset className="pn-styles" aria-label="Text style">
          {(Object.keys(TEXT_STYLES) as TextStyle[]).map((key) => {
            const st = TEXT_STYLES[key];
            return (
              <button
                key={key}
                type="button"
                aria-pressed={style === key}
                className={style === key ? "on" : ""}
                onClick={() => setStyle(key)}
              >
                <span
                  className="pn-style-swatch"
                  style={{
                    color: st.fill,
                    background: st.plate ?? "#3a4a5c",
                    WebkitTextStroke: st.stroke ? `1px ${st.stroke}` : undefined,
                  }}
                  aria-hidden="true"
                >
                  Aa
                </span>
                <span>{st.label}</span>
              </button>
            );
          })}
        </fieldset>
        <button type="submit" className="cast-primary">
          {editing.id === null ? "Add to screen" : "Done"}
        </button>
      </form>
    </Sheet>
  );
}

/** A gesture in progress on one layer: one finger moving it, or two fingers (or one on the
 *  corner handle) resizing and turning it from where it started. `dist` and `angle` are the
 *  fingers' spread and slant (or the handle's reach from the centre) at the start; `from` and
 *  `fromTurn` the layer's size and turn then. */
type Gesture =
  | { kind: "move"; id: number }
  | {
      kind: "pinch" | "handle";
      id: number;
      centre: Point;
      dist: number;
      angle: number;
      from: number;
      fromTurn: number;
    };

/** The handle's radius on the phone, in CSS pixels: a fingertip. */
const HANDLE_CSS = 14;
/** Two taps on the same words within this are a double-tap: open them to edit. */
const DOUBLE_TAP_MS = 350;

/** The frame on a canvas at the slot's width, redrawn on every change. A touch picks up the
 *  topmost layer under the finger (words over pictures): a drag moves it, and a pinch-and-twist
 *  anywhere on the canvas, or a pull on its corner handle, resizes and turns it. The outline and
 *  handle are drawn only here, never into the frame that is sent. */
function FrameCanvas({
  draft,
  cssW,
  held,
  onHold,
  onEdit,
  onDraft,
  onFits,
}: {
  draft: CastDraft;
  cssW: number;
  held: number | null;
  onHold: (id: number | null) => void;
  onEdit: (id: number) => void;
  onDraft: (fn: (d: CastDraft) => CastDraft) => void;
  onFits: (fits: Map<number, WordsFit>) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const cssH = (cssW * TV_H) / TV_W;
  const k = TV_W / cssW;
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef<Gesture | null>(null);
  const lastTap = useRef<{ id: number; at: number } | null>(null);

  useLayoutEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(canvas.width / TV_W, 0, 0, canvas.height / TV_H, 0, 0);
    onFits(drawFrame(ctx, draft));
    const l = layerById(draft, held);
    const r = l && layerRect(ctx, l);
    if (r) drawPicked(ctx, r, TV_W / cssW);
  }, [draft, cssW, cssH, onFits, held]);

  function ctx(): CanvasRenderingContext2D | null {
    return ref.current?.getContext("2d") ?? null;
  }

  /** A pointer's place on the screen, in TV pixels. */
  function at(e: ReactPointerEvent<HTMLCanvasElement>): Point {
    const box = e.currentTarget.getBoundingClientRect();
    return { x: (e.clientX - box.left) * k, y: (e.clientY - box.top) * k };
  }

  /** The two fingers' spread and slant. */
  function twoFingers(): { dist: number; angle: number } {
    const [a, b] = [...pointers.current.values()];
    if (!a || !b) return { dist: 0, angle: 0 };
    return { dist: Math.hypot(b.x - a.x, b.y - a.y), angle: Math.atan2(b.y - a.y, b.x - a.x) };
  }

  /** The handle's reach and slant from the layer's centre. */
  function fromCentre(p: Point, centre: Point): { dist: number; angle: number } {
    return {
      dist: Math.hypot(p.x - centre.x, p.y - centre.y),
      angle: Math.atan2(p.y - centre.y, p.x - centre.x),
    };
  }

  function grab(kind: "pinch" | "handle", l: Layer, p: Point): Gesture {
    const centre = { x: l.x, y: l.y };
    return {
      kind,
      id: l.id,
      centre,
      ...(kind === "pinch" ? twoFingers() : fromCentre(p, centre)),
      from: layerScale(l),
      fromTurn: l.turn,
    };
  }

  function down(e: ReactPointerEvent<HTMLCanvasElement>) {
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const p = at(e);
    pointers.current.set(e.pointerId, p);
    const c = ctx();
    if (pointers.current.size === 2) {
      // Two fingers anywhere work the layer in hand: a small one is hard to land both on.
      const l = layerById(draft, gesture.current?.id ?? held);
      gesture.current = l ? grab("pinch", l, p) : null;
      if (l) onHold(l.id);
      return;
    }
    if (pointers.current.size > 2 || !c) return;
    const l = layerById(draft, held);
    const r = l && layerRect(c, l);
    if (l && r) {
      const h = handleAt(r);
      if (Math.hypot(p.x - h.x, p.y - h.y) <= HANDLE_CSS * k * 1.6) {
        gesture.current = grab("handle", l, p);
        return;
      }
    }
    const hit = layerAt(c, draft, p, HANDLE_CSS * k);
    onHold(hit);
    gesture.current = hit === null ? null : { kind: "move", id: hit };
    const now = e.timeStamp || Date.now();
    const words = layerById(draft, hit)?.kind === "words";
    if (
      hit !== null &&
      words &&
      lastTap.current?.id === hit &&
      now - lastTap.current.at < DOUBLE_TAP_MS
    ) {
      lastTap.current = null;
      gesture.current = null;
      pointers.current.clear();
      onEdit(hit);
      return;
    }
    lastTap.current = hit === null ? null : { id: hit, at: now };
  }

  function move(e: ReactPointerEvent<HTMLCanvasElement>) {
    const last = pointers.current.get(e.pointerId);
    if (!last) return;
    const p = at(e);
    pointers.current.set(e.pointerId, p);
    const g = gesture.current;
    if (!g) return;
    if (g.kind !== "move") {
      if (g.kind === "pinch" && pointers.current.size < 2) return;
      const now = g.kind === "pinch" ? twoFingers() : fromCentre(p, g.centre);
      const value = (g.from * now.dist) / (g.dist || 1);
      const turn = g.fromTurn + now.angle - g.angle;
      onDraft((d) => turnLayer(scaleLayer(d, g.id, value), g.id, turn));
      return;
    }
    // One finger only moves: a second one down turned this into a pinch.
    if (pointers.current.size !== 1) return;
    const dx = p.x - last.x;
    const dy = p.y - last.y;
    if (Math.hypot(dx, dy) > 0) lastTap.current = null;
    onDraft((d) => moveLayer(d, g.id, dx, dy));
  }

  function up(e: ReactPointerEvent<HTMLCanvasElement>) {
    pointers.current.delete(e.pointerId);
    // Lifting one finger of a pinch ends it: the one left doesn't jump into a drag.
    if (pointers.current.size === 0 || gesture.current?.kind === "pinch") gesture.current = null;
  }

  return (
    <canvas
      ref={ref}
      className="pn-canvas edit"
      aria-label="What the TV will show"
      style={{ width: cssW, height: cssH }}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
    />
  );
}

/** The held layer's outline and its corner handle, turned with it, sized in phone pixels
 *  (`k` TV pixels to one) so they read the same on any screen. */
function drawPicked(ctx: CanvasRenderingContext2D, r: Rect, k: number) {
  ctx.save();
  if (r.turn) {
    ctx.translate(r.x + r.w / 2, r.y + r.h / 2);
    ctx.rotate(r.turn);
    ctx.translate(-(r.x + r.w / 2), -(r.y + r.h / 2));
  }
  ctx.lineWidth = 2 * k;
  ctx.strokeStyle = "rgba(255,255,255,0.9)";
  ctx.setLineDash?.([6 * k, 4 * k]);
  ctx.strokeRect(r.x, r.y, r.w, r.h);
  ctx.setLineDash?.([]);
  ctx.beginPath();
  ctx.arc(r.x + r.w, r.y + r.h, HANDLE_CSS * k, 0, Math.PI * 2);
  ctx.fillStyle = "#ffffff";
  ctx.fill();
  ctx.lineWidth = 2 * k;
  ctx.strokeStyle = "rgba(0,0,0,0.6)";
  ctx.stroke();
  ctx.restore();
}

/** The slot's width, kept current as the screen turns. */
function useWidth(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setW(el.clientWidth || 320);
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/** The frame as the TV gets it: 1920x1080, JPEG (a photo's PNG would be several times the
 *  size), base64. */
function exportFrame(d: CastDraft): string {
  const canvas = document.createElement("canvas");
  canvas.width = TV_W;
  canvas.height = TV_H;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no canvas");
  drawFrame(ctx, d);
  const url = canvas.toDataURL("image/jpeg", 0.9);
  return url.slice(url.indexOf(",") + 1);
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

/** A camera photo scaled once to the working size, so zooming and dragging redraw from a
 *  small copy. Never scaled up. */
async function loadPhoto(file: File): Promise<Photo> {
  const bmp = await createImageBitmap(file);
  try {
    const size = workingSize(bmp.width, bmp.height, PHOTO_WORKING_MAX);
    const canvas = document.createElement("canvas");
    canvas.width = size.w;
    canvas.height = size.h;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no canvas");
    ctx.drawImage(bmp, 0, 0, size.w, size.h);
    return { source: canvas, w: size.w, h: size.h };
  } finally {
    bmp.close();
  }
}
