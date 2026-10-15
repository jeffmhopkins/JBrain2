// SEND TO PANEL — a photo, words on it, a question, or any of the three, from the owner's phone
// to the glass on his desk panel (docs/plans/JPANEL_C6_PLAN.md §2 "The composer"; the binding
// mocks are docs/mocks/jpanel-c6 45-56, approved 2026-10-14).
//
// Four screens over the jpanel surface: Compose (Photo / Text / Question tabs over a live
// preview of the glass), Review (the box's own rendering, chime, how long it stays, Send),
// the status of one sent notice (with Unsend while it is still open), and everything sent in the
// last week. A refusal lands on Review with the draft intact and a Try again, because the box
// never holds a refused notice to deliver later.
//
// The draft outlives the screen (`drafts`): a refusal's remedy is often a switch in Panel
// settings, and going to flip it must not cost the owner the photo he framed.

import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  type PanelNoticeIn,
  type PanelNoticeOut,
  type PanelStatusOut,
  api,
  panelNoticeImageUrl,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import { ChevronLeftIcon, ImageIcon, SendIcon } from "../components/icons";
import {
  ANSWERS_MAX,
  ANSWER_MAX,
  type Draft,
  type Layout,
  OPEN_STATES,
  type Photo,
  QUESTION_MAX,
  type RefusalCopy,
  SCREEN,
  SEND_PATH,
  SIZE_MAX,
  SIZE_MIN,
  STAY_ASK,
  STAY_PLAIN,
  TEXT_STYLES,
  type TextStyle,
  WORDS_MAX,
  ZOOM_MAX,
  clamp,
  clipBytes,
  clock,
  draftProblem,
  drawPicture,
  emptyDraft,
  groupByDay,
  noticeLayout,
  noticePayload,
  outcome,
  photoPlacement,
  pictureSize,
  refusalCopy,
  shareFresh,
  stayShort,
  utf8Bytes,
  workingSize,
} from "../panelNotice";
import { panelHealth } from "../panelStatus";
import "./panelNotice.css";

export type ComposerStart =
  | { screen: "compose"; shared?: boolean }
  | { screen: "history" }
  | { screen: "status"; notice: PanelNoticeOut };

/** One draft per panel for the life of the app. */
const drafts = new Map<string, Draft>();

/** Forget every kept draft (a test's fresh start). */
export function forgetDrafts(): void {
  drafts.clear();
}

/** Where a shared photo waits for the composer: the service worker's share target
 *  (public/panel-share-sw.js) puts it here and redirects to `/panel/send?shared=1`. */
export const SHARE_CACHE = "panel-share";

/** How long one status read holds the line on the box (its long-poll ceiling is 25 s). */
const WAIT_S = 20;
const REPOLL_MS = 2_000;

async function loadPhoto(file: Blob): Promise<Photo> {
  let src: CanvasImageSource;
  let w: number;
  let h: number;
  if (typeof createImageBitmap === "function") {
    const bmp = await createImageBitmap(file);
    src = bmp;
    w = bmp.width;
    h = bmp.height;
  } else {
    const url = URL.createObjectURL(file);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      src = img;
      w = img.naturalWidth;
      h = img.naturalHeight;
    } finally {
      URL.revokeObjectURL(url);
    }
  }
  const size = workingSize(w, h);
  const canvas = document.createElement("canvas");
  canvas.width = size.w;
  canvas.height = size.h;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no canvas");
  ctx.drawImage(src, 0, 0, size.w, size.h);
  if ("close" in src && typeof src.close === "function") src.close();
  return { source: canvas, w: size.w, h: size.h };
}

/** The picture the panel will show, drawn at its own size, as base64; null for a question
 *  alone. A photo goes as JPEG (a tenth of the PNG), words on black as PNG, whose flat colour
 *  the box's run-length stripes like. */
function exportPicture(d: Draft): string | null {
  const layout = noticeLayout(d);
  if (layout !== "full" && layout !== "banner") return null;
  const { w, h } = pictureSize(d, layout);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  drawPicture(ctx, d, w, h);
  const url = canvas.toDataURL(d.photo ? "image/jpeg" : "image/png", 0.92);
  return url.slice(url.indexOf(",") + 1);
}

async function blobBase64(blob: Blob): Promise<string> {
  const buf = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) {
    bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

/** A notice from the history as something to send again: the same words and answers, and
 *  its picture exactly as the panel drew it. */
async function resendPayload(n: PanelNoticeOut): Promise<PanelNoticeIn> {
  const out: PanelNoticeIn = {
    device_id: n.device_id,
    title: n.title,
    body: n.body,
    choices: n.choices,
    chime: n.chime,
    hide_after_s: n.hide_after_s ?? 0,
    ttl_s: n.ttl_s,
  };
  if (n.image) {
    out.image_b64 = await blobBase64(await api.panelNoticeImage(n.id, SCREEN));
    out.image_mode = n.image.mode;
  }
  return out;
}

type Refused = RefusalCopy & { reason: string };

async function sendNotice(
  body: PanelNoticeIn,
): Promise<{ sent: PanelNoticeOut } | { refused: Refused }> {
  const got = await api.sendPanelNotice(body);
  if ("refused" in got)
    return { refused: { ...refusalCopy(got.refused), reason: got.refused.reason } };
  return { sent: await api.panelNotice(got.sent.id, 0) };
}

interface SharedOffer {
  photo: Blob | null;
  words: string;
}

/** What another app shared, shown before it touches the draft. */
function SharedOfferCard({
  offer,
  onUse,
  onDiscard,
}: {
  offer: SharedOffer;
  onUse: () => void;
  onDiscard: () => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!offer.photo || typeof URL.createObjectURL !== "function") return;
    const url = URL.createObjectURL(offer.photo);
    setSrc(url);
    return () => URL.revokeObjectURL(url);
  }, [offer.photo]);
  return (
    <section className="pn-card pn-offer" aria-label="Shared to the panel">
      <div className="pn-offer-body">
        {src && <img className="pn-thumb" src={src} alt="What was shared" />}
        <span className="pn-line-text">
          <span>{offer.photo ? "Use the shared photo?" : "Use the shared words?"}</span>
          {offer.words && <span className="pn-sub">“{offer.words}”</span>}
        </span>
      </div>
      <div className="pn-row">
        <button type="button" className="pn-btn" onClick={onDiscard}>
          Discard
        </button>
        <button type="button" className="pn-btn pn-use" onClick={onUse}>
          Use it
        </button>
      </div>
    </section>
  );
}

// ---- the glass ---------------------------------------------------------------------------

function SenderChip() {
  return (
    <span className="pn-chip">
      <span className="pn-chip-dot" aria-hidden="true" />
      You
    </span>
  );
}

/** The panel's square glass at `size` CSS pixels: the picture (or the empty square), and for a
 *  question the panel's card under it — drawn in the panel's own proportions. */
function Glass({
  layout,
  size,
  picH,
  picture,
  title,
  choices,
  hint,
  caption,
}: {
  layout: Layout;
  size: number;
  picH: number;
  picture: ReactNode;
  title: string;
  choices: string[];
  hint: string;
  caption?: string;
}) {
  const k = size / SCREEN;
  return (
    <figure className="pn-glass-wrap">
      <div
        className="pn-glass"
        style={{ width: size, height: size, borderRadius: Math.round(44 * k) }}
        role="img"
        aria-label={layout === "empty" ? "Empty preview" : "Preview of the panel"}
      >
        {layout === "empty" && (
          <div className="pn-glass-empty">
            <ImageIcon size={28} />
            <span>The panel’s whole screen</span>
            <span className="pn-glass-empty-sub">Add a photo, some words, or just a question</span>
          </div>
        )}
        {(layout === "full" || layout === "banner") && (
          <div className="pn-glass-pic" style={{ height: picH * k }}>
            {picture}
          </div>
        )}
        {layout === "full" && (
          <>
            <span className="pn-glass-over top">
              <SenderChip />
            </span>
            <span className="pn-glass-over bottom">{hint}</span>
          </>
        )}
        {(layout === "banner" || layout === "plain") && (
          <div
            className="pn-glass-card"
            style={{ height: (SCREEN - (layout === "banner" ? picH : 0)) * k, fontSize: 14 * k }}
          >
            {layout === "plain" && <SenderChip />}
            <span className="pn-glass-title" style={{ fontSize: 30 * k }}>
              {title || "Your question"}
            </span>
            <div
              className="pn-glass-choices"
              style={{ gridTemplateColumns: `repeat(${Math.max(1, choices.length)}, 1fr)` }}
            >
              {choices.map((c, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: a draft's answers may repeat while typed
                <span key={`${c}-${i}`} className={i === 0 ? "first" : ""}>
                  {c}
                </span>
              ))}
            </div>
            <span className="pn-glass-hint">{hint}</span>
          </div>
        )}
      </div>
      {caption && <figcaption className="pn-caption">{caption}</figcaption>}
    </figure>
  );
}

/** The draft's picture on a canvas, redrawn on every change. With `edit`, a drag moves the
 *  words (Text tab) or pans the photo, and a pinch zooms it. */
function DraftCanvas({
  draft,
  cssW,
  edit,
  onDraft,
}: {
  draft: Draft;
  cssW: number;
  edit: "words" | "photo" | null;
  onDraft?: (fn: (d: Draft) => Draft) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const { w, h } = pictureSize(draft);
  const cssH = (h * cssW) / w;
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ dist: number; zoom: number } | null>(null);

  useLayoutEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(canvas.width / w, 0, 0, canvas.height / h, 0, 0);
    drawPicture(ctx, draft, w, h);
  }, [draft, cssW, cssH, w, h]);

  function spread(): number {
    const [a, b] = [...pointers.current.values()];
    return a && b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
  }

  function down(e: ReactPointerEvent<HTMLCanvasElement>) {
    if (!edit || !onDraft) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2) pinch.current = { dist: spread(), zoom: draft.zoom };
  }

  function move(e: ReactPointerEvent<HTMLCanvasElement>) {
    const last = pointers.current.get(e.pointerId);
    if (!last || !onDraft) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2 && pinch.current && draft.photo) {
      const p = pinch.current;
      const zoom = clamp((p.zoom * spread()) / (p.dist || 1), 1, ZOOM_MAX);
      onDraft((d) => ({ ...d, zoom }));
      return;
    }
    const k = w / cssW;
    const dx = (e.clientX - last.x) * k;
    const dy = (e.clientY - last.y) * k;
    if (edit === "words" && draft.words.trim()) {
      const key = h === SCREEN ? "full" : "banner";
      onDraft((d) => ({
        ...d,
        pos: {
          ...d.pos,
          [key]: { x: clamp(d.pos[key].x + dx, 0, w), y: clamp(d.pos[key].y + dy, 0, h) },
        },
      }));
    } else if (draft.photo) {
      onDraft((d) => {
        if (!d.photo) return d;
        // Held to the photo's edges as it moves, so a long drag past the edge does not bank
        // slack that a drag back has to unwind before anything moves.
        const p = photoPlacement(d.photo, { ...d, panX: d.panX + dx, panY: d.panY + dy }, w, h);
        return { ...d, panX: p.panX, panY: p.panY };
      });
    }
  }

  function up(e: ReactPointerEvent<HTMLCanvasElement>) {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  }

  return (
    <canvas
      ref={ref}
      className={`pn-canvas${edit ? " edit" : ""}`}
      style={{ width: cssW, height: cssH }}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
    />
  );
}

function hintFor(ask: boolean, stay: number | null): string {
  if (ask)
    return stay
      ? `Tap an answer · hides in ${stayShort(stay)}`
      : "Tap an answer · tap elsewhere to hide";
  return stay ? `Tap to hide · ${stayShort(stay)}` : "Tap to hide";
}

/** How wide the glass may be: the mock's 320 on a phone, never wider than the column. */
function useGlassSize(max: number): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState(max);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => setSize(Math.max(160, Math.min(max, el.clientWidth || max)));
    fit();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [max]);
  return [ref, size];
}

// ---- the composer --------------------------------------------------------------------------

type Tab = "photo" | "text" | "question";

function Switch({ on, label, onFlip }: { on: boolean; label: string; onFlip: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={`jp-switch${on ? " on" : ""}`}
      onClick={onFlip}
    >
      <span className="jp-knob" />
    </button>
  );
}

function PhotoPickers({ onFile, again }: { onFile: (f: File) => void; again?: boolean }) {
  const pick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) onFile(f);
  };
  return (
    <>
      <label className={again ? "pn-btn" : "pn-pick"}>
        {again ? "Retake" : "Take photo"}
        <input type="file" accept="image/*" capture="environment" onChange={pick} />
      </label>
      <label className={again ? "pn-btn" : "pn-pick"}>
        {again ? "Choose another" : "Choose photo"}
        <input type="file" accept="image/*" onChange={pick} />
      </label>
    </>
  );
}

/** iPhones have no launcher shortcuts and no share target, so the way to a one-tap icon is
 *  Safari's own Add to Home Screen on this page — said once, where it applies. */
function isIos(): boolean {
  const ua = navigator.userAgent;
  return /iPhone|iPad|iPod/.test(ua) || (ua.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

function Compose({
  draft,
  setDraft,
  tab,
  setTab,
  photoError,
  onFile,
  iosTip,
}: {
  draft: Draft;
  setDraft: (fn: (d: Draft) => Draft) => void;
  tab: Tab;
  setTab: (t: Tab) => void;
  photoError: string | null;
  onFile: (f: File) => void;
  iosTip: boolean;
}) {
  const [wrap, size] = useGlassSize(320);
  const layout = noticeLayout(draft);
  const pic = pictureSize(draft, layout);
  const captions: Record<Layout, string> = {
    empty: "Nothing yet · the panel’s glass, 480 × 480",
    full: `Fills the glass · 480 × 480${draft.words.trim() ? " · words drawn in" : ""}`,
    banner: `Picture over the question · 480 × ${pic.h} strip`,
    plain: "The panel’s own question card · no picture",
  };
  const tabs: [Tab, string, boolean][] = [
    ["photo", "Photo", draft.photo !== null],
    ["text", "Text", draft.words.trim() !== ""],
    ["question", "Question", draft.ask],
  ];
  const twoLines = pic.h < 240;
  return (
    <>
      <div className="pn-glass-slot" ref={wrap}>
        <Glass
          layout={layout}
          size={size}
          picH={pic.h}
          picture={
            <DraftCanvas
              draft={draft}
              cssW={size}
              edit={tab === "text" ? "words" : "photo"}
              onDraft={setDraft}
            />
          }
          title={draft.question}
          choices={draft.answers}
          hint={hintFor(draft.ask, draft.ask ? 0 : draft.stayPlain)}
          caption={captions[layout]}
        />
      </div>
      <div className="pn-tabs" role="tablist" aria-label="Photo, Text or Question">
        {tabs.map(([key, label, on]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "on" : ""}
            onClick={() => setTab(key)}
          >
            {label}
            {on && tab !== key && <span className="pn-tab-dot" aria-label="(added)" />}
          </button>
        ))}
      </div>

      {tab === "photo" && (
        <div className="pn-panel">
          {draft.photo === null ? (
            <>
              <div className="pn-picks">
                <PhotoPickers onFile={onFile} />
              </div>
              <span className="pn-note">
                A photo is optional. Words alone go on black; a question alone is the panel’s own
                question card.
              </span>
            </>
          ) : (
            <>
              <label className="pn-field">
                <span className="pn-field-head">
                  <span>Zoom</span>
                  <strong>{draft.zoom.toFixed(1)}×</strong>
                </span>
                <input
                  type="range"
                  min={100}
                  max={ZOOM_MAX * 100}
                  step={5}
                  value={Math.round(draft.zoom * 100)}
                  onChange={(e) => {
                    const zoom = Number(e.target.value) / 100;
                    setDraft((d) => ({ ...d, zoom }));
                  }}
                />
              </label>
              <span className="pn-note">
                Drag the photo in the square to frame it, or pinch. What is outside the square is
                not sent.
              </span>
              <div className="pn-row">
                <PhotoPickers onFile={onFile} again />
                <button
                  type="button"
                  className="pn-btn"
                  onClick={() =>
                    setDraft((d) => ({ ...d, photo: null, zoom: 1, panX: 0, panY: 0 }))
                  }
                >
                  Remove
                </button>
              </div>
            </>
          )}
          {photoError && <p className="jp-unit-error">{photoError}</p>}
        </div>
      )}

      {tab === "text" && (
        <div className="pn-panel">
          <label className="pn-field">
            <span className="pn-field-head">
              <span>Words on the picture</span>
              <span className="pn-count">
                {utf8Bytes(draft.words)} / {WORDS_MAX}
              </span>
            </span>
            <input
              type="text"
              value={draft.words}
              placeholder="One word or a short message"
              onChange={(e) => {
                const words = clipBytes(e.target.value, WORDS_MAX);
                setDraft((d) => ({ ...d, words }));
              }}
            />
          </label>
          <label className="pn-field">
            <span className="pn-field-head">
              <span>Size</span>
              <strong>{draft.size} px on the panel</strong>
            </span>
            <input
              type="range"
              min={SIZE_MIN}
              max={SIZE_MAX}
              step={2}
              value={draft.size}
              onChange={(e) => {
                const sz = Number(e.target.value);
                setDraft((d) => ({ ...d, size: sz }));
              }}
            />
          </label>
          <fieldset className="pn-styles" aria-label="Text style">
            {(Object.keys(TEXT_STYLES) as TextStyle[]).map((key) => {
              const st = TEXT_STYLES[key];
              return (
                <button
                  key={key}
                  type="button"
                  aria-pressed={draft.style === key}
                  className={draft.style === key ? "on" : ""}
                  onClick={() => setDraft((d) => ({ ...d, style: key }))}
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
          <span className="pn-note">
            Drag the words to move them. They are drawn into the picture on your phone, so the panel
            only shows a picture.
          </span>
        </div>
      )}

      {tab === "question" && (
        <div className="pn-panel">
          <div className="pn-line">
            <span className="pn-line-text">
              <span>Ask a question</span>
              <span className="pn-sub">The tapped answer comes back here</span>
            </span>
            <Switch
              on={draft.ask}
              label="Ask a question"
              onFlip={() =>
                setDraft((d) => ({
                  ...d,
                  ask: !d.ask,
                  answers: !d.ask && d.answers.length === 0 ? ["Yes", "No"] : d.answers,
                }))
              }
            />
          </div>
          {draft.ask && (
            <>
              <label className="pn-field">
                <span className="pn-field-head">
                  <span>Question</span>
                  <span className="pn-count">
                    {utf8Bytes(draft.question)} / {QUESTION_MAX}
                  </span>
                </span>
                <input
                  type="text"
                  value={draft.question}
                  placeholder="Did the package come?"
                  onChange={(e) => {
                    const question = clipBytes(e.target.value, QUESTION_MAX);
                    setDraft((d) => ({ ...d, question }));
                  }}
                />
              </label>
              <div className="pn-field">
                <span className="pn-field-head">
                  <span>
                    Answers · up to {ANSWERS_MAX}, {ANSWER_MAX} letters each
                  </span>
                </span>
                <div className="pn-answers">
                  {draft.answers.map((a, i) => (
                    // biome-ignore lint/suspicious/noArrayIndexKey: an answer being typed has no identity but its place
                    <span className="pn-answer" key={`answer-${i}`}>
                      <input
                        type="text"
                        value={a}
                        aria-label={`Answer ${i + 1}`}
                        size={Math.max(3, a.length)}
                        onChange={(e) => {
                          const v = clipBytes(e.target.value, ANSWER_MAX);
                          setDraft((d) => ({
                            ...d,
                            answers: d.answers.map((x, j) => (j === i ? v : x)),
                          }));
                        }}
                      />
                      <button
                        type="button"
                        aria-label={`Remove ${a || `answer ${i + 1}`}`}
                        onClick={() =>
                          setDraft((d) => ({ ...d, answers: d.answers.filter((_, j) => j !== i) }))
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  {draft.answers.length < ANSWERS_MAX && (
                    <button
                      type="button"
                      className="pn-btn"
                      onClick={() => setDraft((d) => ({ ...d, answers: [...d.answers, ""] }))}
                    >
                      + Answer
                    </button>
                  )}
                  {draft.answers.length === 0 && (
                    <button
                      type="button"
                      className="pn-btn"
                      onClick={() => setDraft((d) => ({ ...d, answers: ["Yes", "No"] }))}
                    >
                      Yes / No
                    </button>
                  )}
                </div>
              </div>
              <span className="pn-note">
                {layout === "banner"
                  ? twoLines
                    ? "Two lines of question: the picture strip shrinks to 480 × 200 so the answers still fit."
                    : "With a picture, the panel shows it as a strip above the question. Drag in the square to frame the strip."
                  : "No picture: the panel draws the question on its own card, answers underneath."}
              </span>
            </>
          )}
        </div>
      )}
      {iosTip && (
        <p className="pn-note pn-tip">
          For an icon that opens straight here: in Safari, on this screen, tap Share, then Add to
          Home Screen.
        </p>
      )}
    </>
  );
}

// ---- review ----------------------------------------------------------------------------------

function Review({
  draft,
  setDraft,
  panel,
  preview,
  refused,
  onSettings,
}: {
  draft: Draft;
  setDraft: (fn: (d: Draft) => Draft) => void;
  panel: PanelStatusOut;
  preview: string | null;
  refused: Refused | null;
  onSettings: () => void;
}) {
  const [wrap, size] = useGlassSize(refused ? 250 : 320);
  const layout = noticeLayout(draft);
  const pic = pictureSize(draft, layout);
  const opts = draft.ask ? STAY_ASK : STAY_PLAIN;
  const chosen = draft.ask ? draft.stayAsk : draft.stayPlain;
  return (
    <>
      {refused && (
        <div className="pn-refusal" role="alert">
          <span className="pn-refusal-title">Not sent — {refused.title}</span>
          <span>{refused.body}</span>
          {refused.settings && (
            <div>
              <button type="button" className="pn-btn" onClick={onSettings}>
                Panel settings → Notices
              </button>
            </div>
          )}
        </div>
      )}
      <div className="pn-glass-slot" ref={wrap}>
        <Glass
          layout={layout}
          size={size}
          picH={pic.h}
          picture={
            preview ? (
              <img className="pn-canvas" src={preview} alt="" />
            ) : (
              <DraftCanvas draft={draft} cssW={size} edit={null} />
            )
          }
          title={draft.question}
          choices={draft.answers}
          hint={hintFor(draft.ask, draft.ask ? 0 : draft.stayPlain)}
          caption="This is what the panel will show"
        />
      </div>
      <div className="pn-card">
        <div className="pn-line">
          <span className="pn-sub">To</span>
          <span className="pn-to">
            {panel.name}’s panel <Online panel={panel} />
          </span>
        </div>
        <div className="pn-line">
          <span className="pn-line-text">
            <span>Chime when it arrives</span>
            <span className="pn-sub">The panel’s chime switch still decides</span>
          </span>
          <Switch
            on={draft.chime}
            label="Chime when it arrives"
            onFlip={() => setDraft((d) => ({ ...d, chime: !d.chime }))}
          />
        </div>
        <div className="pn-line col">
          <span className="pn-line-text">
            <span>{draft.ask ? "Waits for an answer" : "Stays on the panel"}</span>
            <span className="pn-sub">
              {draft.ask
                ? "Until tapped, up to this long"
                : "Then it hides itself; a tap hides it sooner"}
            </span>
          </span>
          <div className="pn-seg">
            {opts.map(([v, label]) => (
              <button
                key={v}
                type="button"
                aria-pressed={chosen === v}
                className={chosen === v ? "on" : ""}
                onClick={() =>
                  setDraft((d) => (d.ask ? { ...d, stayAsk: v } : { ...d, stayPlain: v }))
                }
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>
      {!refused && (
        <span className="pn-note">
          {draft.ask
            ? "A tap off the answers hides it unanswered. On the clock face it waits as a corner badge; at night it shows in the night colour."
            : "On the clock face it waits as a corner badge; at night the picture waits behind Show picture."}
        </span>
      )}
    </>
  );
}

function Online({ panel }: { panel: PanelStatusOut }) {
  const ok = panelHealth(panel.age_s) === "ok";
  return (
    <span className={`pn-online${ok ? "" : " off"}`}>
      <span className="pn-online-dot" aria-hidden="true" />
      {ok ? "Online" : "Not heard from lately"}
    </span>
  );
}

// ---- one sent notice -----------------------------------------------------------------------

function NoticePicture({
  n,
  draft,
  size,
}: { n: PanelNoticeOut; draft: Draft | null; size: number }) {
  if (!n.image) return null;
  if (draft) return <DraftCanvas draft={draft} cssW={size} edit={null} />;
  return <img className="pn-canvas" src={panelNoticeImageUrl(n.id, SCREEN)} alt="" />;
}

function layoutOf(n: PanelNoticeOut): Layout {
  return n.kind === "picture" ? "full" : n.kind === "banner" ? "banner" : "plain";
}

const STEP_KEYS = ["sent", "panel", "end"];

/** The status card: Sent, On the panel, then how it ended — and Unsend while it can. */
function Status({
  notice,
  draft,
  onNotice,
}: {
  notice: PanelNoticeOut;
  draft: Draft | null;
  onNotice: (n: PanelNoticeOut) => void;
}) {
  const [wrap, size] = useGlassSize(220);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = OPEN_STATES.has(notice.state);
  const ask = notice.choices.length > 0;
  const id = notice.id;

  // Held open on the box while the notice is: each read returns the moment it moves.
  useEffect(() => {
    if (!open) return;
    let alive = true;
    void (async () => {
      while (alive) {
        const asked = Date.now();
        try {
          const got = await api.panelNotice(id, WAIT_S);
          if (!alive) return;
          onNotice(got);
          if (!OPEN_STATES.has(got.state)) return;
        } catch {
          if (alive)
            setError("Lost the box for a moment. Open it again from Sent to see how it ended.");
          return;
        }
        // A read that came straight back (a proxy that does not hold the line) must not turn
        // into a loop hammering the box.
        if (Date.now() - asked < REPOLL_MS) await new Promise((r) => setTimeout(r, REPOLL_MS));
      }
    })();
    return () => {
      alive = false;
    };
  }, [id, open, onNotice]);

  async function unsend(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      onNotice(await api.withdrawPanelNotice(id));
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // It ended while the owner was reaching for the button; show how.
        onNotice(await api.panelNotice(id, 0));
      } else {
        setError(err instanceof ApiError ? err.message : "Could not unsend it.");
      }
    } finally {
      setBusy(false);
    }
  }

  const end = outcome(notice);
  const at = notice.state === "queued" ? 0 : notice.state === "delivered" ? 1 : open ? 2 : 3;
  const shownOrHeld = notice.shown_at ?? notice.delivered_at;
  const steps: { label: string; sub: string; time: string }[] = [
    { label: at === 0 ? "Sending…" : "Sent", sub: "", time: clock(notice.created_at, true) },
    {
      label: "On the panel",
      sub: notice.shown_at
        ? "Shown on the glass"
        : notice.delivered_at
          ? "Waiting behind the clock face"
          : "The panel picks it up within a few seconds",
      time: clock(shownOrHeld, true),
    },
    {
      label: open ? (ask ? "Waiting for an answer" : end.text) : end.text,
      sub:
        open && ask && notice.expires_at
          ? `Answers accepted until ${clock(notice.expires_at)}`
          : "",
      time: clock(notice.finished_at, true),
    },
  ];
  const withdrawn = notice.state === "withdrawn";
  return (
    <>
      {notice.state === "answered" && (
        <div className="pn-result ok" aria-live="polite">
          <span className="pn-result-kicker">Answered on the panel</span>
          <span className="pn-result-big">{notice.answer}</span>
          <span className="pn-sub">Tapped at {clock(notice.finished_at)}</span>
        </div>
      )}
      {open && ask && notice.state === "shown" && (
        <div className="pn-result live" aria-live="polite">
          <span className="pn-result-kicker">On the panel</span>
          <span className="pn-result-big">Waiting for a tap…</span>
          <span className="pn-sub">You can leave this screen — the answer lands in Sent</span>
        </div>
      )}
      {withdrawn && (
        <div className="pn-result" aria-live="polite">
          <span className="pn-result-kicker">Unsent</span>
          <span className="pn-result-big">
            {notice.delivered_at ? "Taken off the panel" : "It never reached the panel"}
          </span>
          {notice.delivered_at && (
            <span className="pn-sub">The panel drops it on its next check, without a sound</span>
          )}
        </div>
      )}
      <div className="pn-glass-slot" ref={wrap}>
        <Glass
          layout={layoutOf(notice)}
          size={size}
          picH={notice.image?.h ?? (notice.kind === "banner" ? 240 : SCREEN)}
          picture={<NoticePicture n={notice} draft={draft} size={size} />}
          title={notice.title}
          choices={notice.choices}
          hint={hintFor(ask, notice.hide_after_s)}
        />
      </div>
      <ol className="pn-steps">
        {steps.map((s, k) => {
          const done = at > k || (k === 2 && !open);
          const live = at === k && open;
          return (
            <li key={STEP_KEYS[k]} className={done ? "done" : live ? "live" : ""}>
              <span className="pn-step-dot" aria-hidden="true" />
              <span className="pn-step-text">
                <span>{s.label}</span>
                {s.sub && <span className="pn-sub">{s.sub}</span>}
              </span>
              <span className="pn-step-time">{done || live ? s.time : ""}</span>
            </li>
          );
        })}
      </ol>
      {open && (
        <div className="pn-row">
          <button
            type="button"
            className="pn-btn danger"
            disabled={busy}
            onClick={() => void unsend()}
          >
            {busy ? "Unsending…" : "Unsend"}
          </button>
          <span className="pn-note">Takes it off the panel, quietly.</span>
        </div>
      )}
      {error && <p className="jp-unit-error">{error}</p>}
    </>
  );
}

// ---- history ---------------------------------------------------------------------------------

export function NoticeThumb({ n }: { n: PanelNoticeOut }) {
  if (n.image) {
    return <img className="pn-thumb" src={panelNoticeImageUrl(n.id, 96)} alt="" loading="lazy" />;
  }
  return (
    <span className="pn-thumb icon" aria-hidden="true">
      ?
    </span>
  );
}

function History({
  deviceId,
  onOpen,
}: {
  deviceId: string;
  onOpen: (n: PanelNoticeOut) => void;
}) {
  const [list, setList] = useState<{ notices: PanelNoticeOut[]; kept_s: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .panelNotices(deviceId)
      .then((got) => alive && setList(got))
      .catch(() => alive && setError("Could not read what was sent. Is the box reachable?"));
    return () => {
      alive = false;
    };
  }, [deviceId]);
  if (error) return <p className="jp-empty">{error}</p>;
  if (!list) return <p className="jp-empty">Reading what you sent…</p>;
  const days = Math.round(list.kept_s / 86400);
  return (
    <>
      {list.notices.length === 0 && <p className="jp-empty">Nothing sent in the last week.</p>}
      {groupByDay(list.notices).map((g) => (
        <section key={g.label} className="pn-day">
          <span className="pn-day-label">{g.label}</span>
          <div className="pn-list">
            {g.rows.map((n) => {
              const o = outcome(n);
              return (
                <button key={n.id} type="button" className="pn-hist" onClick={() => onOpen(n)}>
                  <NoticeThumb n={n} />
                  <span className="pn-hist-text">
                    <span className="pn-hist-title">{n.title}</span>
                    <span className={`pn-hist-out ${o.tone}`}>{o.text}</span>
                  </span>
                  <span className="pn-hist-time">{clock(n.created_at)}</span>
                </button>
              );
            })}
          </div>
        </section>
      ))}
      <span className="pn-note">
        What you sent from the app, as the box recorded it. The box keeps each one {days} days after
        it ends. Tap one to see it again or send it again.
      </span>
    </>
  );
}

// ---- the surface ---------------------------------------------------------------------------

type Screen = "compose" | "review" | "status" | "history";

export function PanelNoticeComposer({
  panel,
  start,
  onClose,
  onSent,
  onSettings,
}: {
  panel: PanelStatusOut;
  start: ComposerStart;
  onClose: () => void;
  /** Something was sent or unsent: the card's list is stale. */
  onSent: () => void;
  onSettings: () => void;
}) {
  const deviceId = panel.device_id;
  const [screen, setScreen] = useState<Screen>(start.screen);
  // Where Back goes from the status screen: the history if it was opened from there.
  const [fromHistory, setFromHistory] = useState(start.screen === "history");
  const [draft, setDraftState] = useState<Draft>(() => drafts.get(deviceId) ?? emptyDraft());
  const [tab, setTab] = useState<Tab>("photo");
  const [photoError, setPhotoError] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [refused, setRefused] = useState<Refused | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [notice, setNotice] = useState<PanelNoticeOut | null>(
    start.screen === "status" ? start.notice : null,
  );
  // The draft a status screen draws from: the one just sent, not one from the history.
  const [sentDraft, setSentDraft] = useState<Draft | null>(null);
  const [iosTip] = useState(
    () => isIos() && !window.matchMedia?.("(display-mode: standalone)").matches,
  );

  const setDraft = useCallback(
    (fn: (d: Draft) => Draft) =>
      setDraftState((d) => {
        const next = fn(d);
        drafts.set(deviceId, next);
        return next;
      }),
    [deviceId],
  );

  const onFile = useCallback(
    async (f: Blob) => {
      setPhotoError(null);
      try {
        const photo = await loadPhoto(f);
        setDraft((d) => ({ ...d, photo, zoom: 1, panX: 0, panY: 0 }));
      } catch {
        setPhotoError("Could not open that photo. Try another.");
      }
    },
    [setDraft],
  );

  // A photo (and words) shared from another app, left by the service worker. Offered, never
  // applied: any site can post to the share URL, so what lands in the draft is what the owner
  // says yes to, and a share left lying in the cache is forgotten (panel-share-sw.js).
  const [offer, setOffer] = useState<SharedOffer | null>(null);
  useEffect(() => {
    if (start.screen !== "compose" || !start.shared || typeof caches === "undefined") return;
    let alive = true;
    void (async () => {
      try {
        const cache = await caches.open(SHARE_CACHE);
        const photo = await cache.match("/panel/shared-photo");
        const text = await cache.match("/panel/shared-text");
        await caches.delete(SHARE_CACHE);
        const now = Date.now();
        const blob =
          photo && shareFresh(photo.headers.get("X-Shared-At"), now) ? await photo.blob() : null;
        const words =
          text && shareFresh(text.headers.get("X-Shared-At"), now)
            ? clipBytes((await text.text()).trim(), WORDS_MAX)
            : "";
        if (alive && (blob || words)) setOffer({ photo: blob, words });
      } catch {
        if (alive) setPhotoError("The shared photo could not be read. Choose it here instead.");
      }
    })();
    return () => {
      alive = false;
    };
  }, [start]);

  async function acceptOffer(o: SharedOffer): Promise<void> {
    setOffer(null);
    if (o.photo) await onFile(o.photo);
    if (o.words) setDraft((d) => ({ ...d, words: o.words }));
  }

  // Review shows the box's own rendering of the picture: a photograph posterized to fit the
  // panel's cap looks different from the phone's copy, and Review promises what lands. Only on
  // arriving at Review: chime and stay, all Review can change, do not change the picture.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the picture is fixed while on Review
  useEffect(() => {
    if (screen !== "review") return;
    setPreview(null);
    const image = exportPicture(draft);
    const layout = noticeLayout(draft);
    if (!image || (layout !== "full" && layout !== "banner")) return;
    let alive = true;
    api
      .previewPanelNotice(image, layout)
      .then((p) => alive && setPreview(`data:image/png;base64,${p.png_b64}`))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [screen]);

  // While the composer is up the address is the deep link, so an iPhone's Add to Home Screen
  // gets an icon that opens straight here — with its own picture and name.
  useEffect(() => {
    const before = window.location.pathname + window.location.search;
    if (window.location.pathname !== SEND_PATH) {
      window.history.replaceState(window.history.state, "", SEND_PATH);
    }
    const icon = document.querySelector<HTMLLinkElement>('link[rel="apple-touch-icon"]');
    const oldIcon = icon?.getAttribute("href") ?? null;
    icon?.setAttribute("href", "/send-to-panel-180.png");
    let title = document.querySelector<HTMLMetaElement>('meta[name="apple-mobile-web-app-title"]');
    const made = title === null;
    if (!title) {
      title = document.createElement("meta");
      title.name = "apple-mobile-web-app-title";
      document.head.appendChild(title);
    }
    const oldTitle = title.content;
    title.content = "Send to panel";
    return () => {
      if (oldIcon !== null) icon?.setAttribute("href", oldIcon);
      if (made) title?.remove();
      else if (title) title.content = oldTitle;
      const back = before === SEND_PATH || before.startsWith(`${SEND_PATH}?`) ? "/" : before;
      window.history.replaceState(window.history.state, "", back);
    };
  }, []);

  function back(): void {
    if (screen === "review") {
      setRefused(null);
      setScreen("compose");
    } else if (screen === "status" && fromHistory) {
      setScreen("history");
    } else {
      onClose();
    }
  }
  useBackLayer(back);

  async function send(): Promise<void> {
    setSending(true);
    setSendError(null);
    try {
      const out = await sendNotice(noticePayload(draft, deviceId, exportPicture(draft)));
      if ("refused" in out) {
        setRefused(out.refused);
        return;
      }
      setRefused(null);
      setSentDraft(draft);
      setNotice(out.sent);
      setFromHistory(false);
      setScreen("status");
      onSent();
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : "Could not reach the box.");
    } finally {
      setSending(false);
    }
  }

  async function sendAgain(n: PanelNoticeOut): Promise<void> {
    setSending(true);
    setSendError(null);
    try {
      const out = await sendNotice(await resendPayload(n));
      if ("refused" in out) {
        setSendError(`Not sent — ${out.refused.title}. ${out.refused.body}`);
        return;
      }
      setSentDraft(null);
      setNotice(out.sent);
      onSent();
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : "Could not reach the box.");
    } finally {
      setSending(false);
    }
  }

  const onNotice = useCallback(
    (n: PanelNoticeOut) => {
      setNotice(n);
      if (!OPEN_STATES.has(n.state)) onSent();
    },
    [onSent],
  );

  const titles: Record<Screen, string> = {
    compose: `Send to ${panel.name}’s panel`,
    review: "Review",
    status: `Sent to ${panel.name}’s panel`,
    history: `Sent to ${panel.name}’s panel`,
  };
  const problem = draftProblem(draft);

  let foot: ReactNode = null;
  if (screen === "compose") {
    foot = (
      <button
        type="button"
        className="pn-primary"
        aria-disabled={problem !== null}
        onClick={() => {
          if (problem === null) setScreen("review");
        }}
      >
        Review
      </button>
    );
  } else if (screen === "review") {
    foot = (
      <>
        <button
          type="button"
          className="pn-secondary"
          onClick={() => {
            setRefused(null);
            setScreen("compose");
          }}
        >
          Edit
        </button>
        <button type="button" className="pn-primary" disabled={sending} onClick={() => void send()}>
          <SendIcon size={18} />
          {sending ? "Sending…" : refused ? "Try again" : "Send to panel"}
        </button>
      </>
    );
  } else if (screen === "status" && notice) {
    foot = fromHistory ? (
      <>
        <button type="button" className="pn-secondary" onClick={() => setScreen("history")}>
          All sent
        </button>
        <button
          type="button"
          className="pn-primary"
          disabled={sending}
          onClick={() => void sendAgain(notice)}
        >
          <SendIcon size={18} />
          {sending ? "Sending…" : "Send again"}
        </button>
      </>
    ) : (
      <>
        <button
          type="button"
          className="pn-secondary"
          onClick={() => {
            const fresh = { ...emptyDraft(), chime: draft.chime };
            drafts.set(deviceId, fresh);
            setDraftState(fresh);
            setTab("photo");
            setScreen("compose");
          }}
        >
          Send another
        </button>
        <button type="button" className="pn-primary" onClick={onClose}>
          Done
        </button>
      </>
    );
  }

  return (
    <div className="pn-wrap">
      <header className="pn-bar">
        <button type="button" className="pn-back" aria-label="Back" onClick={back}>
          <ChevronLeftIcon size={22} />
        </button>
        <h1>{titles[screen]}</h1>
        {(screen === "compose" || screen === "status") && <Online panel={panel} />}
      </header>
      <main className="pn-body">
        {screen === "compose" && offer && (
          <SharedOfferCard
            offer={offer}
            onUse={() => void acceptOffer(offer)}
            onDiscard={() => setOffer(null)}
          />
        )}
        {screen === "compose" && (
          <Compose
            draft={draft}
            setDraft={setDraft}
            tab={tab}
            setTab={setTab}
            photoError={photoError}
            onFile={(f) => void onFile(f)}
            iosTip={iosTip}
          />
        )}
        {screen === "review" && (
          <Review
            draft={draft}
            setDraft={setDraft}
            panel={panel}
            preview={preview}
            refused={refused}
            onSettings={onSettings}
          />
        )}
        {screen === "status" && notice && (
          <Status notice={notice} draft={fromHistory ? null : sentDraft} onNotice={onNotice} />
        )}
        {screen === "history" && (
          <History
            deviceId={deviceId}
            onOpen={(n) => {
              setNotice(n);
              setFromHistory(true);
              setScreen("status");
            }}
          />
        )}
        {sendError && <p className="jp-unit-error">{sendError}</p>}
        {screen === "compose" && problem && noticeLayout(draft) !== "empty" && (
          <p className="pn-note">{problem}</p>
        )}
      </main>
      {foot && <footer className="pn-foot">{foot}</footer>}
    </div>
  );
}

/** The panel card's half: the button, and the last three sent with how each ended. */
export function SentToPanel({
  deviceId,
  tick,
  onCompose,
  onHistory,
  onOpen,
}: {
  deviceId: string;
  tick: number;
  onCompose: () => void;
  onHistory: () => void;
  onOpen: (n: PanelNoticeOut) => void;
}) {
  const [recent, setRecent] = useState<PanelNoticeOut[] | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick is a re-read trigger
  useEffect(() => {
    let alive = true;
    api
      .panelNotices(deviceId, 3)
      .then((got) => alive && setRecent(got.notices))
      .catch(() => alive && setRecent([]));
    return () => {
      alive = false;
    };
  }, [deviceId, tick]);
  return (
    <div className="pn-sent">
      <button type="button" className="pn-send" onClick={onCompose}>
        <SendIcon size={18} />
        Send to panel
      </button>
      {recent !== null && recent.length > 0 && (
        <div className="pn-sent-box">
          <div className="pn-sent-head">
            <span>Sent to this panel</span>
            <button type="button" className="pn-link" onClick={onHistory}>
              All sent
            </button>
          </div>
          <div className="pn-list">
            {recent.map((n) => {
              const o = outcome(n);
              return (
                <button key={n.id} type="button" className="pn-hist" onClick={() => onOpen(n)}>
                  <NoticeThumb n={n} />
                  <span className="pn-hist-text">
                    <span className="pn-hist-title">{n.title}</span>
                    <span className="pn-hist-out">
                      {clock(n.created_at)} · <span className={o.tone}>{o.text}</span>
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
