// Note view Analysis tab (docs/reference/DESIGN.md "Analysis tab + entity pages" —
// graph-forward): facts render as literal property-graph edges grouped by
// subject node; subject headers double as entity navigation; tapping a fact
// expands its citation back to the highlighted source words. The Sources
// card at the bottom (settled review — variant B) frames analysis as a
// pipeline: the note text plus every image's extract stages, with the
// provenance footer owning the note-level re-run.

import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { EdgeValue, FactCitation, FactTenure, KindBadge, StatusChip } from "../analysis/bits";
import { dedupeTokens, edgePath, fmtConfidence, fmtTemporal } from "../analysis/format";
import {
  type AnalysisEntity,
  type AttachmentExtract,
  type FactOut,
  type NoteAnalysis,
  api,
  attachmentUrl,
} from "../api/client";
import { awaitingImageCount } from "../notes/lifecycle";
import type { StreamAttachment } from "../notes/useNotes";
import { useForegroundRef } from "../visibility";
import { AudioTranscript, transcriptWords } from "./AudioTranscript";
import {
  ImageExpansion,
  type ImageExtractsApi,
  type StageStatus,
  fmtBytes,
  imageStages,
  useImageExtracts,
} from "./ImageExtracts";
import { Sheet } from "./Sheet";
import { ChevronRightIcon, FileIcon, ImageIcon } from "./icons";

type AnalysisState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; analysis: NoteAnalysis };

interface SubjectGroup {
  entity: AnalysisEntity;
  facts: FactOut[];
}

// The note's analysis surface shows what the note currently asserts —
// active + pending_review. Retracted/superseded facts stay reachable in the
// entity pages' history rails, which exist for exactly that.
const VISIBLE_STATUSES = new Set(["active", "pending_review"]);

/** Group visible facts by subject, in order of first appearance. */
function groupBySubject(analysis: NoteAnalysis): SubjectGroup[] {
  const byId = new Map(analysis.entities.map((e) => [e.id, e]));
  const groups: SubjectGroup[] = [];
  for (const fact of analysis.facts) {
    if (!VISIBLE_STATUSES.has(fact.status)) continue;
    const last = groups.find((g) => g.entity.id === fact.entity_id);
    if (last) {
      last.facts.push(fact);
      continue;
    }
    const entity = byId.get(fact.entity_id) ?? {
      id: fact.entity_id,
      kind: "",
      name: fact.entity_name,
      status: "active",
    };
    groups.push({ entity, facts: [fact] });
  }
  return groups;
}

interface FactRowProps {
  fact: FactOut;
  extractor: string | null;
  onOpenEntity: (entityId: string) => void;
}

function FactRow({ fact, extractor, onOpenEntity }: FactRowProps) {
  const [open, setOpen] = useState(false);
  const toggle = () => setOpen((o) => !o);
  const former = fact.valid_to !== null; // closed interval = FORMER (not current)
  return (
    <div className="fact-row-wrap">
      <div
        className={former ? "fact-row is-former" : "fact-row"}
        // biome-ignore lint/a11y/useSemanticElements: the row hosts a nested object-entity link, which a real <button> cannot wrap.
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <span className="fact-edge">
          <span className="edge-path">{edgePath(fact.predicate, fact.qualifier)}</span>
          <span className="edge-arrow"> → </span>
          <span className="edge-value">
            <EdgeValue fact={fact} onOpenEntity={onOpenEntity} />
          </span>
        </span>
        <FactTenure fact={fact} />
        <span className="fact-meta">
          <KindBadge kind={fact.kind} />
          <StatusChip status={fact.status} pinned={fact.pinned} />
          <span className="fact-conf">{fmtConfidence(fact.confidence)}</span>
        </span>
      </div>
      {open && <FactCitation fact={fact} extractor={extractor} />}
    </div>
  );
}

// ===== The Sources card (settled review — variant B) =====

/** A synced image attachment — the only kind that gets a pipeline row. */
interface ImageSource {
  id: string;
  filename: string;
  mediaType: string;
  sizeBytes: number;
  hasExtracts: boolean;
  hasDescription: boolean;
}

function imageSources(attachments: StreamAttachment[] | null): ImageSource[] {
  return (attachments ?? []).flatMap((a) =>
    a.id !== null && a.mediaType.startsWith("image/") ? [{ ...a, id: a.id }] : [],
  );
}

interface AudioSource {
  id: string;
  filename: string;
}

function audioSources(attachments: StreamAttachment[] | null): AudioSource[] {
  return (attachments ?? []).flatMap((a) =>
    a.id !== null && a.mediaType.startsWith("audio/") ? [{ id: a.id, filename: a.filename }] : [],
  );
}

/** The audio attachments' transcripts, each as a playable karaoke card. Lazily
 * loads each attachment's transcript extract (with its per-word data) and renders
 * the reused AudioTranscript component over the note's audio download URL. */
function AudioTranscriptSources({ audio }: { audio: AudioSource[] }) {
  const [extracts, setExtracts] = useState<Record<string, AttachmentExtract | null>>({});
  const loaded = useRef<Set<string>>(new Set());
  const ids = audio.map((a) => a.id).join(",");

  // Keyed on the id list (a new `audio` array every render would loop); the ref
  // tracks which ids were already fetched.
  // biome-ignore lint/correctness/useExhaustiveDependencies: id-list keyed, ref-guarded
  useEffect(() => {
    let cancelled = false;
    for (const a of audio) {
      if (loaded.current.has(a.id)) continue;
      loaded.current.add(a.id);
      api
        .attachmentExtracts(a.id)
        .then((rows) => {
          if (cancelled) return;
          const transcript = rows.find((r) => r.kind === "transcript") ?? null;
          setExtracts((prev) => ({ ...prev, [a.id]: transcript }));
        })
        .catch(() => {
          if (!cancelled) setExtracts((prev) => ({ ...prev, [a.id]: null }));
        });
    }
    return () => {
      cancelled = true;
    };
  }, [ids]);

  return (
    <>
      {audio.map((a) => {
        const extract = extracts[a.id];
        if (extract === undefined) {
          return (
            <p key={a.id} className="analysis-sources-meta">
              {a.filename} · loading transcript…
            </p>
          );
        }
        if (extract === null || !extract.text) {
          return (
            <p key={a.id} className="analysis-sources-meta">
              {a.filename} · no transcript yet
            </p>
          );
        }
        const words = transcriptWords(extract.words);
        const lastWord = words[words.length - 1];
        return (
          <div key={a.id} className="analysis-audio-card">
            <AudioTranscript
              audioUrl={attachmentUrl(a.id)}
              filename={a.filename}
              model={extract.tool}
              words={words}
              text={extract.text}
              durationMs={lastWord ? lastWord.endMs : null}
            />
          </div>
        );
      })}
    </>
  );
}

function StageMark({ status }: { status: StageStatus }) {
  if (status === "done") return <span className="stage-done">✓</span>;
  if (status === "running") {
    return (
      <span className="stage-running">
        <span className="spin" aria-hidden="true" />
      </span>
    );
  }
  if (status === "skipped") return <span className="stage-queued">skipped</span>;
  if (status === "queued") return <span className="stage-queued">queued</span>;
  return <span className="stage-queued">—</span>;
}

interface SourceImageRowProps {
  source: ImageSource;
  extractsApi: ImageExtractsApi;
  settled: boolean;
  onMore: () => void;
}

function SourceImageRow({ source, extractsApi, settled, onMore }: SourceImageRowProps) {
  const [open, setOpen] = useState(false);
  const analyzing = extractsApi.analyzingIds.includes(source.id);
  const stages = imageStages({
    attachment: source,
    extracts: extractsApi.extractsById[source.id],
    mode: extractsApi.mode,
    analyzing,
    settled: settled && !analyzing,
  });

  function onKeyDown(e: KeyboardEvent) {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    setOpen((o) => !o);
  }

  return (
    <div className={`analysis-sources-item${open ? " open" : ""}`}>
      <div
        className="analysis-sources-row"
        // biome-ignore lint/a11y/useSemanticElements: the row hosts the nested ⋯ button, which a real <button> cannot.
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onKeyDown}
      >
        <span className="analysis-sources-icon">
          <ImageIcon size={20} />
        </span>
        <span className="analysis-sources-main">
          <span className="analysis-sources-name">{source.filename}</span>
          <span className="analysis-sources-meta">
            {fmtBytes(source.sizeBytes)} · {source.mediaType}
          </span>
          <span className="analysis-sources-stages">
            <span>ocr</span> <StageMark status={stages.ocr} />
            <span className="stage-dot">·</span>
            <span>description</span> <StageMark status={stages.description} />
          </span>
        </span>
        <span className="analysis-sources-caret" aria-hidden="true">
          <ChevronRightIcon size={16} />
        </span>
        <button
          type="button"
          className="analysis-sources-more"
          aria-label={`Actions for ${source.filename}`}
          onClick={(e) => {
            e.stopPropagation();
            onMore();
          }}
        >
          ⋯
        </button>
      </div>
      {open && (
        <ImageExpansion
          attachmentId={source.id}
          extracts={extractsApi.extractsById[source.id]}
          mode={extractsApi.mode}
          analyzing={analyzing}
          settled={settled && !analyzing}
        />
      )}
    </div>
  );
}

interface SourcesCardProps {
  bodyChars: number;
  images: ImageSource[];
  audio: AudioSource[];
  extractsApi: ImageExtractsApi;
  /** null = not yet analyzed (gated/waiting) — the footer re-run disables. */
  analysis: NoteAnalysis | null;
  rerunning: boolean;
  onRerun: () => void;
  onRerunImage: (attachmentId: string) => void;
}

function SourcesCard({
  bodyChars,
  images,
  audio,
  extractsApi,
  analysis,
  rerunning,
  onRerun,
  onRerunImage,
}: SourcesCardProps) {
  const [sheetFor, setSheetFor] = useState<ImageSource | null>(null);
  const settled = analysis !== null && !rerunning;
  return (
    <section>
      <h3 className="section-header">Sources</h3>
      <div className="analysis-sources-card">
        <div className="analysis-sources-item">
          <div className="analysis-sources-row">
            <span className="analysis-sources-icon">
              <FileIcon size={20} />
            </span>
            <span className="analysis-sources-main">
              <span className="analysis-sources-name">note text</span>
              <span className="analysis-sources-meta">
                {bodyChars} chars · the note body itself
              </span>
            </span>
            <span className="analysis-sources-check">✓</span>
          </div>
        </div>
        {images.map((source) => (
          <SourceImageRow
            key={source.id}
            source={source}
            extractsApi={extractsApi}
            settled={settled}
            onMore={() => setSheetFor(source)}
          />
        ))}
        {audio.length > 0 && <AudioTranscriptSources audio={audio} />}
        <div className="analysis-sources-foot">
          {analysis !== null ? (
            <p className="analysis-sources-provenance">
              {/* analyzed_at is a real instant, not a calendar date: keep it local */}
              analyzed {fmtTemporal(analysis.analyzed_at, "instant")}
              {analysis.extractor !== null && ` · ${analysis.extractor}`}
            </p>
          ) : (
            <p className="analysis-sources-provenance">
              analysis waits here — runs automatically when every source is in.
            </p>
          )}
          <button
            type="button"
            className="analysis-sources-rerun"
            disabled={analysis === null || rerunning}
            onClick={onRerun}
          >
            {rerunning && <span className="spin" aria-hidden="true" />}
            {rerunning ? "re-running…" : "re-run analysis"}
          </button>
        </div>
      </div>

      {sheetFor !== null && (
        <Sheet title={sheetFor.filename} onClose={() => setSheetFor(null)}>
          <button
            type="button"
            className="sheet-action"
            onClick={() => {
              const attId = sheetFor.id;
              setSheetFor(null);
              onRerunImage(attId);
            }}
          >
            re-run image analysis
          </button>
          <p className="sheet-hint">
            runs ocr and description again for this image — facts re-extract after.
          </p>
        </Sheet>
      )}
    </section>
  );
}

const POLL_MS = 3000;

interface AnalysisTabProps {
  /** Server note id; null for unsynced outbox rows (nothing to analyze yet). */
  noteId: string | null;
  /** null = unknown (search-result fallback until the full note resolves). */
  attachments: StreamAttachment[] | null;
  ingestState: string | null;
  /** Note body length, for the Sources card's note-text row. */
  bodyChars: number;
  onOpenEntity: (entityId: string) => void;
}

export function AnalysisTab({
  noteId,
  attachments,
  ingestState,
  bodyChars,
  onOpenEntity,
}: AnalysisTabProps) {
  const [state, setState] = useState<AnalysisState>({ phase: "loading" });
  const [rerunning, setRerunning] = useState(false);
  const images = imageSources(attachments);
  const audio = audioSources(attachments);
  const extractsApi = useImageExtracts(images.map((a) => a.id));
  const { refresh: refreshExtracts } = extractsApi;

  useEffect(() => {
    if (noteId === null) return;
    let stale = false;
    api
      .noteAnalysis(noteId)
      .then((analysis) => {
        if (!stale) setState({ phase: "done", analysis });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [noteId]);

  // One poller serves the re-run flow, the gated waiting state, and the
  // per-image re-run: tick noteAnalysis until analyzed_at moves past the
  // pre-run value, swap the fresh analysis in, and refetch the extracts so
  // the stage marks settle too. Unmount (incl. tab switch) clears it.
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // A backgrounded app holds its place but sends nothing: the tick below skips
  // the request while hidden and resumes once the app is foreground again.
  const foregroundRef = useForegroundRef();
  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) clearInterval(pollTimer.current);
    pollTimer.current = null;
  }, []);
  useEffect(() => stopPolling, [stopPolling]);

  const startPolling = useCallback(
    (prevAnalyzedAt: string | null) => {
      if (noteId === null) return;
      stopPolling();
      pollTimer.current = setInterval(() => {
        if (!foregroundRef.current) return;
        api
          .noteAnalysis(noteId)
          .then((fresh) => {
            if (fresh.analyzed_at === null || fresh.analyzed_at === prevAnalyzedAt) return;
            stopPolling();
            setState({ phase: "done", analysis: fresh });
            setRerunning(false);
            refreshExtracts();
          })
          .catch(() => {}); // transient failure — the next tick retries
      }, POLL_MS);
    },
    [noteId, stopPolling, refreshExtracts, foregroundRef],
  );

  const analysis = state.phase === "done" ? state.analysis : null;
  // The gated empty state: indexed, analysis pending, and at least one image
  // still missing its extracts — the backend won't analyze until they land.
  const gated =
    analysis !== null &&
    analysis.analyzed_at === null &&
    ingestState === "indexed" &&
    attachments !== null &&
    awaitingImageCount(attachments) > 0;

  useEffect(() => {
    if (gated) startPolling(null);
  }, [gated, startPolling]);

  function rerunNote() {
    if (noteId === null || analysis === null) return;
    setRerunning(true);
    // A 409 means a run is already in flight, which reads the same; the
    // poller picks up whichever run finishes.
    api.analyzeNote(noteId).catch(() => {});
    startPolling(analysis.analyzed_at);
  }

  function rerunImage(attachmentId: string) {
    extractsApi.analyze(attachmentId);
    // Image extracts gate analysis, so a fresh analysis follows; poll it in
    // so the result fills without reopening the note.
    startPolling(analysis?.analyzed_at ?? null);
  }

  if (noteId === null) {
    return <p className="analysis-quiet">analysis runs after indexing — nothing here yet.</p>;
  }
  if (state.phase === "loading") {
    return <p className="analysis-quiet">loading analysis…</p>;
  }
  if (state.phase === "error") {
    return <p className="analysis-quiet">couldn't load analysis — reopen to retry.</p>;
  }
  if (analysis === null || analysis.analyzed_at === null) {
    if (!gated) {
      return <p className="analysis-quiet">analysis runs after indexing — nothing here yet.</p>;
    }
    return (
      <div className="analysis-tab">
        <p className="analysis-quiet">
          waiting on image analysis — facts extract once every source below is in.
        </p>
        <SourcesCard
          bodyChars={bodyChars}
          images={images}
          audio={audio}
          extractsApi={extractsApi}
          analysis={null}
          rerunning={false}
          onRerun={() => {}}
          onRerunImage={rerunImage}
        />
      </div>
    );
  }

  const groups = groupBySubject(analysis);

  return (
    <div className="analysis-tab">
      {analysis.title !== null && <h2 className="analysis-title">{analysis.title}</h2>}
      {analysis.tags.length > 0 && (
        <div className="tag-row">
          {analysis.tags.map((tag) => (
            <span key={tag} className="tag-pill">
              {tag}
            </span>
          ))}
        </div>
      )}

      {groups.map((group) => (
        <section key={group.entity.id} className="subject-group">
          <button
            type="button"
            className="entity-chip"
            onClick={() => onOpenEntity(group.entity.id)}
          >
            <span className="entity-chip-name">{group.entity.name}</span>
            {group.entity.kind !== "" && (
              <span className="entity-chip-kind">{group.entity.kind.toLowerCase()}</span>
            )}
            {group.entity.status === "provisional" && (
              <span className="fact-chip fact-chip-muted">provisional</span>
            )}
          </button>
          <div className="fact-card">
            {group.facts.map((fact) => (
              <FactRow
                key={fact.id}
                fact={fact}
                extractor={analysis.extractor}
                onOpenEntity={onOpenEntity}
              />
            ))}
          </div>
        </section>
      ))}

      {analysis.temporal_tokens.length > 0 && (
        <section>
          <h3 className="section-header">Dates</h3>
          <div className="token-row">
            {dedupeTokens(analysis.temporal_tokens).map((token) => (
              <span key={token.id} className="token-chip">
                {fmtTemporal(token.resolved_start, token.temporal_precision)}
                {token.resolved_end !== null &&
                  ` → ${fmtTemporal(token.resolved_end, token.temporal_precision)}`}
                <span className="token-phrase">“{token.surface_phrase}”</span>
              </span>
            ))}
          </div>
        </section>
      )}

      <SourcesCard
        bodyChars={bodyChars}
        images={images}
        audio={audio}
        extractsApi={extractsApi}
        analysis={analysis}
        rerunning={rerunning}
        onRerun={rerunNote}
        onRerunImage={rerunImage}
      />
    </div>
  );
}
