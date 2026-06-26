// The image launcher (docs/IMAGE_LAUNCHER_PLAN.md "Wave L1", reference mock
// docs/mocks/image-launcher/launcher-b-gallery.html). A card-launcher destination
// that drives on-box renders DIRECTLY — the headline property is that the language
// models stay UNLOADED (the honest residency line). Not a chat surface: only a
// one-line "ask jerv in chat" note, never a chat affordance. Violet image accent.
//
// Mock-mode only here (Wave L1): renders go through api.generateImage/editImage,
// backed by api/mock.ts fixtures; the by-id <img> srcs come from the client URL
// builders (invariant #9: never a hand-authored URL). The L3 wave wires the real
// direct-render API behind the same client methods.

import {
  type ChangeEvent,
  type ReactNode,
  type TouchEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { Lightbox } from "../agent/views/Lightbox";
import { EditCompare, ImageFrame } from "../agent/views/registry";
import {
  type EditImageRequest,
  type GenerateImageRequest,
  type GeneratedImageOut,
  type ImageAspect,
  type ImageResolution,
  type ImageSpeed,
  api,
  generatedImageUrl,
} from "../api/client";
import { ChevronLeftIcon, ImageIcon, PlusIcon, XIcon } from "../components/icons";
import { useArmed } from "../review/useArmed";

// speed implies the model + a fixed step count off the quality path (the mock's
// SPEED table). Only the quality path exposes the 20–40 steps slider.
const SPEED: Record<ImageSpeed, { label: string; model: string; steps: number; note: string }> = {
  dreamshaper: {
    label: "dreamshaper",
    model: "dreamshaper",
    steps: 8,
    note: "tiny SDXL · seconds",
  },
  fast: {
    label: "fast",
    model: "qwen-image-lightning",
    steps: 4,
    note: "4-step Lightning · quick",
  },
  quality: {
    label: "quality",
    model: "qwen-image",
    steps: 30,
    note: "full model · 20–40 steps · ~1min+",
  },
};
const ASPECT: Record<ImageAspect, { label: string; dims: [number, number] }> = {
  square: { label: "1:1", dims: [1024, 1024] },
  portrait: { label: "3:4", dims: [896, 1152] },
  landscape: { label: "4:3", dims: [1152, 896] },
  tall: { label: "9:16", dims: [768, 1344] },
  wide: { label: "16:9", dims: [1344, 768] },
};
const RESMUL: Record<ImageResolution, number> = { small: 0.75, medium: 1, large: 1.4 };
const SPEEDS = Object.keys(SPEED) as ImageSpeed[];
const ASPECTS = Object.keys(ASPECT) as ImageAspect[];
const RESOLUTIONS: ImageResolution[] = ["small", "medium", "large"];

interface GenConfig {
  speed: ImageSpeed;
  aspect: ImageAspect;
  resolution: ImageResolution;
  steps: number;
  negative: string;
  seed: string;
}
interface EditConfig {
  speed: ImageSpeed;
  resolution: ImageResolution;
  steps: number;
  negative: string;
  seed: string;
}

// The chosen edit source: a prior render (id known, dims/model carried for the
// summary) or an uploaded file (objectURL for the before-preview).
interface EditSource {
  kind: "render" | "upload";
  id: string | null;
  file: File | null;
  previewUrl: string;
  name: string;
  width: number;
  height: number;
  model: string | null;
  seed: number | null;
}

// queued → rendering, then the reveal — an honest phased sequence (no fake
// progress bar). reduced-motion shows the same text without the shimmer/spinner.
const PHASES = [
  "queued — ComfyUI is free, starting…",
  "rendering… the language models stay unloaded",
];
const PHASE_MS = 700;

type RenderState =
  | { phase: "idle" }
  | { phase: "queued" | "rendering"; step: number }
  | { phase: "done"; result: GeneratedImageOut; beforeSrc: string | null };

function effSteps(speed: ImageSpeed, steps: number): number {
  return speed === "quality" ? steps : SPEED[speed].steps;
}
function genDims(c: GenConfig): [number, number] {
  const [w, h] = ASPECT[c.aspect].dims;
  const m = RESMUL[c.resolution];
  return [Math.round((w * m) / 8) * 8, Math.round((h * m) / 8) * 8];
}
function seedLabel(seed: string): string {
  return seed.trim() ? seed.trim() : "random";
}
function parseSeed(seed: string): number | null {
  const n = Number(seed.trim());
  return seed.trim() && Number.isFinite(n) ? Math.trunc(n) : null;
}

function reducedMotion(): boolean {
  return (
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

const SWIPE_DOWN_PX = 56;

export function ImageScreen({ onClose }: { onClose: () => void }): ReactNode {
  const [tab, setTab] = useState<"generate" | "edit">("generate");
  const [gallery, setGallery] = useState<GeneratedImageOut[]>([]);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [pickMode, setPickMode] = useState(false);
  const [lightbox, setLightbox] = useState<GeneratedImageOut | null>(null);
  const [zoom, setZoom] = useState<string | null>(null);

  const [genCfg, setGenCfg] = useState<GenConfig>({
    speed: "quality",
    aspect: "square",
    resolution: "medium",
    steps: 30,
    negative: "",
    seed: "",
  });
  const [editCfg, setEditCfg] = useState<EditConfig>({
    speed: "quality",
    resolution: "medium",
    steps: 30,
    negative: "",
    seed: "",
  });
  const [genCardOpen, setGenCardOpen] = useState(true);
  const [editCardOpen, setEditCardOpen] = useState(true);
  const [genPrompt, setGenPrompt] = useState("");
  const [editPrompt, setEditPrompt] = useState("");
  const [editSource, setEditSource] = useState<EditSource | null>(null);
  const [refs, setRefs] = useState<(File | null)[]>([null, null]);

  const [genRender, setGenRender] = useState<RenderState>({ phase: "idle" });
  const [editRender, setEditRender] = useState<RenderState>({ phase: "idle" });
  const [seedCopied, setSeedCopied] = useState(false);
  const touchStart = useRef<number | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .listGeneratedImages()
      .then((list) => {
        if (!stale) setGallery(list);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  // Revoke an uploaded source's objectURL when it's replaced/cleared.
  useEffect(() => {
    const url = editSource?.kind === "upload" ? editSource.previewUrl : null;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [editSource]);

  function useRenderAsSource(g: GeneratedImageOut): void {
    setEditSource({
      kind: "render",
      id: g.id,
      file: null,
      previewUrl: generatedImageUrl(g.id),
      name: "from gallery",
      width: g.width,
      height: g.height,
      model: g.model,
      seed: g.seed,
    });
    setTab("edit");
    setGalleryOpen(false);
    setPickMode(false);
    setLightbox(null);
  }

  // Destructive (DESIGN "Buttons"): the lightbox arms the delete; confirm drops
  // the row (blobs are keep-all), prunes the tile + live count, and closes the
  // lightbox. The render bytes are unaffected — only this gallery row goes.
  async function deleteRender(g: GeneratedImageOut): Promise<void> {
    try {
      await api.deleteGeneratedImage(g.id);
    } catch {
      return; // leave the tile in place if the delete didn't land
    }
    setGallery((prev) => prev.filter((x) => x.id !== g.id));
    setLightbox(null);
  }

  function onUploadSource(e: ChangeEvent<HTMLInputElement>): void {
    const file = e.target.files?.[0];
    if (!file) return;
    setEditSource({
      kind: "upload",
      id: null,
      file,
      previewUrl: URL.createObjectURL(file),
      name: file.name,
      width: 1024,
      height: 1024,
      model: null,
      seed: null,
    });
  }

  function onUploadRef(slot: number, e: ChangeEvent<HTMLInputElement>): void {
    const file = e.target.files?.[0];
    if (!file) return;
    setRefs((prev) => prev.map((r, i) => (i === slot ? file : r)));
  }

  // The honest sequence: flip queued → rendering on a timer (or instantly under
  // reduced motion), await the render, then reveal. The await resolves quickly in
  // mock mode; the phase text is the truthful "what's happening", not a bar.
  async function runRender(mode: "generate" | "edit"): Promise<void> {
    const setRender = mode === "generate" ? setGenRender : setEditRender;
    const reduce = reducedMotion();
    setRender({ phase: "queued", step: 0 });

    let request: Promise<GeneratedImageOut>;
    let beforeSrc: string | null = null;
    if (mode === "generate") {
      const req: GenerateImageRequest = {
        prompt: genPrompt,
        speed: genCfg.speed,
        aspect: genCfg.aspect,
        resolution: genCfg.resolution,
        steps: effSteps(genCfg.speed, genCfg.steps),
        seed: parseSeed(genCfg.seed),
        negativePrompt: genCfg.negative,
      };
      request = api.generateImage(req);
    } else {
      if (!editSource) return;
      beforeSrc = editSource.previewUrl;
      const req: EditImageRequest = {
        prompt: editPrompt,
        speed: editCfg.speed,
        resolution: editCfg.resolution,
        steps: effSteps(editCfg.speed, editCfg.steps),
        seed: parseSeed(editCfg.seed),
        negativePrompt: editCfg.negative,
        sourceImageId: editSource.id,
      };
      const files = refs.filter((r): r is File => r !== null);
      request = api.editImage(req, editSource.file, files);
    }

    if (!reduce) {
      await new Promise((r) => setTimeout(r, PHASE_MS));
      setRender({ phase: "rendering", step: 1 });
      await new Promise((r) => setTimeout(r, PHASE_MS));
    }
    const result = await request;
    setGallery((prev) => [result, ...prev]);
    setRender({ phase: "done", result, beforeSrc });
  }

  function copySeed(seed: number | null): void {
    if (seed === null) return;
    try {
      navigator.clipboard?.writeText(String(seed));
    } catch {
      // best-effort; the seed is also shown in the meta line
    }
    setSeedCopied(true);
    setTimeout(() => setSeedCopied(false), 1600);
  }

  function onTouchStart(event: TouchEvent): void {
    // Down-swipe from the top dismisses, like the other sub-screens.
    const scroller = event.currentTarget.querySelector(".screen-body");
    if ((scroller?.scrollTop ?? 0) > 4) {
      touchStart.current = null;
      return;
    }
    touchStart.current = event.touches[0]?.clientY ?? null;
  }
  function onTouchMove(event: TouchEvent): void {
    const y = event.touches[0]?.clientY;
    if (touchStart.current !== null && y !== undefined && y - touchStart.current > SWIPE_DOWN_PX) {
      touchStart.current = null;
      onClose();
    }
  }

  return (
    <div className="subscreen imgscreen" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <header className="imgscreen-nav">
        <button type="button" className="back-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">Image</span>
        </button>
        <div className="imgscreen-nav-right">
          <span
            className="imgscreen-dot"
            role="status"
            aria-label="ComfyUI ready, language models unloaded"
            title="ComfyUI ready · LLMs unloaded"
          />
          <button
            type="button"
            className="icon-btn imgscreen-gallery-btn"
            onClick={() => {
              setPickMode(false);
              setGalleryOpen(true);
            }}
            aria-label={`Open gallery (${gallery.length} renders)`}
          >
            <ImageIcon size={20} />
            {gallery.length > 0 && <span className="imgscreen-badge">{gallery.length}</span>}
          </button>
        </div>
      </header>

      <div className="screen-body imgscreen-body">
        <div className="imgscreen-residency">
          <span>
            <b>renders on-box</b> · language models stay unloaded
          </span>
        </div>

        <div className="imgseg" role="tablist" aria-label="Generate or edit">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "generate"}
            className={tab === "generate" ? "on" : ""}
            onClick={() => setTab("generate")}
          >
            Generate
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "edit"}
            className={tab === "edit" ? "on" : ""}
            onClick={() => setTab("edit")}
          >
            Edit
          </button>
        </div>

        {tab === "generate" ? (
          <section className="imgpanel" aria-label="Generate">
            <div className="imgfield">
              <label htmlFor="genPrompt">prompt</label>
              <textarea
                id="genPrompt"
                value={genPrompt}
                onChange={(e) => setGenPrompt(e.target.value)}
                placeholder="describe the image — a calm subject, a setting, a light…"
              />
            </div>

            <ConfigCard
              open={genCardOpen}
              onToggle={() => setGenCardOpen((v) => !v)}
              summary={`${SPEED[genCfg.speed].label} · ${genDims(genCfg)[0]}×${genDims(genCfg)[1]} · ${effSteps(genCfg.speed, genCfg.steps)} steps · seed ${seedLabel(genCfg.seed)}`}
            >
              <Knob label="speed" hint={SPEED[genCfg.speed].note}>
                <MiniSeg
                  options={SPEEDS.map((k) => ({ value: k, label: SPEED[k].label }))}
                  value={genCfg.speed}
                  onChange={(v) => setGenCfg((c) => ({ ...c, speed: v }))}
                />
              </Knob>
              <Knob label="aspect" hint="ratio of the frame">
                <MiniSeg
                  five
                  options={ASPECTS.map((k) => ({ value: k, label: ASPECT[k].label }))}
                  value={genCfg.aspect}
                  onChange={(v) => setGenCfg((c) => ({ ...c, aspect: v }))}
                />
              </Knob>
              <Knob label="resolution" hint="medium = native ~1MP">
                <MiniSeg
                  options={RESOLUTIONS.map((k) => ({ value: k, label: k }))}
                  value={genCfg.resolution}
                  onChange={(v) => setGenCfg((c) => ({ ...c, resolution: v }))}
                />
              </Knob>
              <StepsRow
                speed={genCfg.speed}
                steps={genCfg.steps}
                onChange={(v) => setGenCfg((c) => ({ ...c, steps: v }))}
              />
              <Knob label="negative prompt" hint="keep out">
                <input
                  type="text"
                  placeholder="things to avoid — blur, text, extra fingers"
                  value={genCfg.negative}
                  onChange={(e) => setGenCfg((c) => ({ ...c, negative: e.target.value }))}
                />
              </Knob>
              <Knob label="seed" hint="blank = random">
                <input
                  type="number"
                  placeholder="random"
                  value={genCfg.seed}
                  onChange={(e) => setGenCfg((c) => ({ ...c, seed: e.target.value }))}
                />
              </Knob>
            </ConfigCard>

            <button
              type="button"
              className="imgbtn"
              disabled={isRunning(genRender)}
              onClick={() => void runRender("generate")}
            >
              Generate
            </button>
            <p className="imgnote">
              runs ComfyUI on-box · same can be asked of jerv in chat instead
            </p>

            <RenderResult
              state={genRender}
              onUseAsSource={useRenderAsSource}
              onCopySeed={copySeed}
              onZoom={setZoom}
              seedCopied={seedCopied}
            />
          </section>
        ) : (
          <section className="imgpanel" aria-label="Edit">
            <div className="imgfield">
              {/* biome-ignore lint/a11y/noLabelWithoutControl: the dropzone/upload is the control beneath this group label */}
              <label>source image</label>
              {editSource ? (
                <div className="imgsrc">
                  <span className="imgsrc-thumb">
                    <img src={editSource.previewUrl} alt="source" />
                  </span>
                  <span className="imgsrc-meta">
                    <span className="imgsrc-name">{editSource.name}</span>
                    <span className="imgsrc-dim">
                      {editSource.width} × {editSource.height}
                      {editSource.model ? ` · ${editSource.model}` : ""}
                      {editSource.seed !== null ? ` · seed ${editSource.seed}` : ""}
                    </span>
                  </span>
                  <button
                    type="button"
                    className="imgsrc-x"
                    onClick={() => setEditSource(null)}
                    aria-label="Remove source"
                  >
                    <XIcon size={16} />
                  </button>
                </div>
              ) : (
                <label className="imgdrop">
                  <ImageIcon size={26} />
                  <span className="imgdrop-t">upload an image, or pick from the gallery</span>
                  <span className="imgdrop-s">
                    tap to load · or <b>choose a render</b> below
                  </span>
                  <input type="file" accept="image/*" onChange={onUploadSource} hidden />
                </label>
              )}
              <button
                type="button"
                className="imgpick"
                onClick={() => {
                  setPickMode(true);
                  setGalleryOpen(true);
                }}
              >
                <ImageIcon size={14} /> pick from gallery
              </button>
            </div>

            <div className="imgfield">
              {/* biome-ignore lint/a11y/noLabelWithoutControl: labels the two reference upload slots below */}
              <label>
                references <span className="imgmuted">— up to 2, for style or compositing</span>
              </label>
              <div className="imgrefs">
                {[0, 1].map((slot) => (
                  <RefSlot
                    key={slot}
                    file={refs[slot] ?? null}
                    onUpload={(e) => onUploadRef(slot, e)}
                    onClear={() => setRefs((prev) => prev.map((r, i) => (i === slot ? null : r)))}
                  />
                ))}
              </div>
            </div>

            <div className="imgfield">
              <label htmlFor="editPrompt">edit instruction</label>
              <textarea
                id="editPrompt"
                className="imgedit-prompt"
                value={editPrompt}
                onChange={(e) => setEditPrompt(e.target.value)}
                placeholder='what to change — "make the background a warm sunset"'
              />
            </div>

            <ConfigCard
              open={editCardOpen}
              onToggle={() => setEditCardOpen((v) => !v)}
              summary={`${SPEED[editCfg.speed].label} · ${effSteps(editCfg.speed, editCfg.steps)} steps · seed ${seedLabel(editCfg.seed)}`}
            >
              <Knob label="speed" hint={SPEED[editCfg.speed].note}>
                <MiniSeg
                  options={SPEEDS.map((k) => ({ value: k, label: SPEED[k].label }))}
                  value={editCfg.speed}
                  onChange={(v) => setEditCfg((c) => ({ ...c, speed: v }))}
                />
              </Knob>
              <Knob label="aspect" hint="inherited from source">
                <div className="imgmseg">
                  <button type="button" className="on" disabled>
                    matches source image
                  </button>
                </div>
              </Knob>
              <Knob label="resolution" hint="medium = native ~1MP">
                <MiniSeg
                  options={RESOLUTIONS.map((k) => ({ value: k, label: k }))}
                  value={editCfg.resolution}
                  onChange={(v) => setEditCfg((c) => ({ ...c, resolution: v }))}
                />
              </Knob>
              <StepsRow
                speed={editCfg.speed}
                steps={editCfg.steps}
                onChange={(v) => setEditCfg((c) => ({ ...c, steps: v }))}
              />
              <Knob label="negative prompt" hint="keep out">
                <input
                  type="text"
                  placeholder="things to avoid — blur, text, extra fingers"
                  value={editCfg.negative}
                  onChange={(e) => setEditCfg((c) => ({ ...c, negative: e.target.value }))}
                />
              </Knob>
              <Knob label="seed" hint="blank = random">
                <input
                  type="number"
                  placeholder="random"
                  value={editCfg.seed}
                  onChange={(e) => setEditCfg((c) => ({ ...c, seed: e.target.value }))}
                />
              </Knob>
            </ConfigCard>

            <button
              type="button"
              className="imgbtn"
              disabled={!editSource || isRunning(editRender)}
              onClick={() => void runRender("edit")}
            >
              Apply edit
            </button>
            <p className="imgnote">
              edits the source on-box · same can be asked of jerv in chat instead
            </p>

            <RenderResult
              state={editRender}
              onUseAsSource={useRenderAsSource}
              onCopySeed={copySeed}
              onZoom={setZoom}
              seedCopied={seedCopied}
            />
          </section>
        )}
      </div>

      {galleryOpen && (
        <GalleryOverlay
          gallery={gallery}
          pickMode={pickMode}
          onClose={() => {
            setGalleryOpen(false);
            setPickMode(false);
          }}
          onPick={useRenderAsSource}
          onOpen={setLightbox}
        />
      )}

      {lightbox && (
        <LightboxCard
          image={lightbox}
          onClose={() => setLightbox(null)}
          onUseAsSource={useRenderAsSource}
          onCopySeed={copySeed}
          onDelete={deleteRender}
          seedCopied={seedCopied}
        />
      )}

      {zoom && <Lightbox src={zoom} alt="render" onClose={() => setZoom(null)} />}
    </div>
  );
}

function isRunning(state: RenderState): boolean {
  return state.phase === "queued" || state.phase === "rendering";
}

function ConfigCard({
  open,
  summary,
  onToggle,
  children,
}: {
  open: boolean;
  summary: string;
  onToggle: () => void;
  children: ReactNode;
}): ReactNode {
  return (
    <div className={`imgcard${open ? " open" : ""}`}>
      <button type="button" className="imgcard-head" onClick={onToggle} aria-expanded={open}>
        <span className="imgcard-title">configuration</span>
        <span className="imgcard-summary">{summary}</span>
        <span className="imgcard-caret" aria-hidden="true">
          <ChevronLeftIcon size={16} />
        </span>
      </button>
      {open && <div className="imgcard-body">{children}</div>}
    </div>
  );
}

function Knob({
  label,
  hint,
  children,
}: { label: string; hint: string; children: ReactNode }): ReactNode {
  return (
    <div className="imgrow">
      <div className="imgrow-head">
        <span className="imgrow-label">{label}</span>
        <span className="imgrow-hint">{hint}</span>
      </div>
      {children}
    </div>
  );
}

function MiniSeg<T extends string>({
  options,
  value,
  onChange,
  five = false,
}: {
  options: Array<{ value: T; label: string }>;
  value: T;
  onChange: (value: T) => void;
  five?: boolean;
}): ReactNode {
  return (
    <div className={`imgmseg${five ? " five" : ""}`}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? "on" : ""}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// The steps slider VISIBLY LOCKS off the quality path: the row dims, the slider
// is inert, and a "fixed N steps" hint appears (the mock's lock behavior).
function StepsRow({
  speed,
  steps,
  onChange,
}: {
  speed: ImageSpeed;
  steps: number;
  onChange: (value: number) => void;
}): ReactNode {
  const locked = speed !== "quality";
  const shown = locked ? SPEED[speed].steps : steps;
  return (
    <div className={`imgrow imgsteps${locked ? " locked" : ""}`}>
      <div className="imgrow-head">
        <span className="imgrow-label">steps</span>
        <span className="imgrow-hint">20–40 · quality path only</span>
      </div>
      <div className="imgslider">
        <input
          type="range"
          min={20}
          max={40}
          value={shown}
          disabled={locked}
          aria-label="steps"
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <span className="imgstepval">{shown}</span>
      </div>
      {locked && (
        <div className="imglockhint">
          fixed {SPEED[speed].steps} steps on the {speed} path
        </div>
      )}
    </div>
  );
}

function RefSlot({
  file,
  onUpload,
  onClear,
}: {
  file: File | null;
  onUpload: (e: ChangeEvent<HTMLInputElement>) => void;
  onClear: () => void;
}): ReactNode {
  const url = file ? URL.createObjectURL(file) : null;
  useEffect(() => {
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [url]);
  if (file && url) {
    return (
      <div className="imgrefslot filled">
        <img src={url} alt="reference" />
        <button
          type="button"
          className="imgrefslot-x"
          onClick={onClear}
          aria-label="Remove reference"
        >
          <XIcon size={12} />
        </button>
      </div>
    );
  }
  return (
    <label className="imgrefslot">
      <PlusIcon size={20} />
      <input type="file" accept="image/*" onChange={onUpload} hidden aria-label="Add reference" />
    </label>
  );
}

function metaLine(g: GeneratedImageOut): string {
  return `${g.width}×${g.height} · ${g.model}${g.seed !== null ? ` · seed ${g.seed}` : ""}`;
}

function RenderResult({
  state,
  onUseAsSource,
  onCopySeed,
  onZoom,
  seedCopied,
}: {
  state: RenderState;
  onUseAsSource: (g: GeneratedImageOut) => void;
  onCopySeed: (seed: number | null) => void;
  onZoom: (src: string) => void;
  seedCopied: boolean;
}): ReactNode {
  if (state.phase === "idle") return null;

  if (state.phase !== "done") {
    return (
      <div className="imgresult" aria-live="polite">
        <div className="imgstage imgstage-pending">
          <span className="imgspin" aria-hidden="true" />
          <span className="imgphase">{PHASES[state.step]}</span>
        </div>
      </div>
    );
  }

  const g = state.result;
  const src = generatedImageUrl(g.id);
  return (
    <div className="imgresult">
      {g.kind === "edit" && state.beforeSrc ? (
        <EditCompare
          beforeSrc={state.beforeSrc}
          afterSrc={src}
          width={g.width}
          height={g.height}
          alt={g.prompt || "edited image"}
        />
      ) : (
        <ImageFrame
          src={src}
          alt={g.prompt || "generated image"}
          width={g.width}
          height={g.height}
        />
      )}
      <div className="imgmeta">{metaLine(g)}</div>
      <div className="imgactions">
        <button type="button" className="imgchip" onClick={() => onUseAsSource(g)}>
          use as edit source
        </button>
        <button
          type="button"
          className={`imgchip${seedCopied ? " done" : ""}`}
          disabled={g.seed === null}
          onClick={() => onCopySeed(g.seed)}
        >
          {seedCopied ? "copied" : "copy seed"}
        </button>
        {g.kind === "generate" && (
          <button type="button" className="imgchip" onClick={() => onZoom(src)}>
            expand
          </button>
        )}
      </div>
    </div>
  );
}

// The gallery shortcut: a full-screen scrollable IMAGE-ONLY 2-col masonry, newest
// first, a kind badge per tile, a live count. In pick mode a tap sets the edit
// source directly; otherwise it opens the lightbox. Empty = one sentence with the
// action inline.
function GalleryOverlay({
  gallery,
  pickMode,
  onClose,
  onPick,
  onOpen,
}: {
  gallery: GeneratedImageOut[];
  pickMode: boolean;
  onClose: () => void;
  onPick: (g: GeneratedImageOut) => void;
  onOpen: (g: GeneratedImageOut) => void;
}): ReactNode {
  return (
    <div className="imggallery" aria-label="Gallery">
      <header className="imggallery-nav">
        <button
          type="button"
          className="icon-btn"
          onClick={onClose}
          aria-label="Back to image screen"
        >
          <ChevronLeftIcon size={22} />
        </button>
        <span className="screen-title">Gallery</span>
        <span className="imggallery-count">{gallery.length}</span>
        <span className="imggallery-sp" />
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close gallery">
          <XIcon size={20} />
        </button>
      </header>
      <p className="imggallery-hint">
        {pickMode
          ? "tap a render to use it as the edit source"
          : "every render on this device · owner-only, never a note · tap to view"}
      </p>
      {gallery.length === 0 ? (
        <p className="imggallery-empty">
          nothing rendered yet — generate an image and it lands here.
        </p>
      ) : (
        <div className="imggallery-grid">
          {gallery.map((g) => (
            <button
              key={g.id}
              type="button"
              className="imgtile"
              onClick={() => (pickMode ? onPick(g) : onOpen(g))}
              aria-label={`${g.kind} render ${g.width}×${g.height}`}
            >
              <span className="imgtile-kind">{g.kind}</span>
              <img src={generatedImageUrl(g.id)} alt={g.prompt || `${g.kind} render`} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function LightboxCard({
  image,
  onClose,
  onUseAsSource,
  onCopySeed,
  onDelete,
  seedCopied,
}: {
  image: GeneratedImageOut;
  onClose: () => void;
  onUseAsSource: (g: GeneratedImageOut) => void;
  onCopySeed: (seed: number | null) => void;
  onDelete: (g: GeneratedImageOut) => void;
  seedCopied: boolean;
}): ReactNode {
  // Tap-again confirm for the destructive delete (DESIGN "Buttons"): first tap
  // arms to a filled-rose "tap again…" that auto-disarms in ~3s; second confirms.
  const [armed, tap] = useArmed();
  const deleteArmed = armed === "delete";
  return (
    <div className="imglb" aria-label="Render detail">
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: the close + card buttons cover keyboard; the backdrop tap is a pointer convenience */}
      <div className="imglb-top" onClick={onClose}>
        <img src={generatedImageUrl(image.id)} alt={image.prompt || "render"} />
      </div>
      <div className="imglb-card">
        <span className="imglb-grip" aria-hidden="true" />
        <div className="imglb-meta">
          <span className="imglb-kind">{image.kind}</span>
          <span>{metaLine(image)}</span>
        </div>
        <div className="imgactions">
          <button type="button" className="imgchip" onClick={() => onUseAsSource(image)}>
            use as edit source
          </button>
          <button
            type="button"
            className={`imgchip${seedCopied ? " done" : ""}`}
            disabled={image.seed === null}
            onClick={() => onCopySeed(image.seed)}
          >
            {seedCopied ? "copied" : "copy seed"}
          </button>
          <button
            type="button"
            className={`imgchip imgchip-danger${deleteArmed ? " armed" : ""}`}
            onClick={() => {
              if (tap("delete")) onDelete(image);
            }}
            onBlur={() => deleteArmed && tap("delete")}
          >
            {deleteArmed ? "tap again — deletes this render" : "delete"}
          </button>
          <button type="button" className="imgchip" onClick={onClose}>
            close
          </button>
        </div>
      </div>
    </div>
  );
}
