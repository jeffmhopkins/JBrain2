// Note view layer (docs/DESIGN.md "Note view"): a slide-up tree level over
// home or search with Note / Attachments / Analysis tabs. Attachments is the
// canonical manager (manifest rows + per-file sheet); pre-Phase-3 the header
// is domain + date only (no title), and Analysis shows phased placeholders.

import { Fragment, type TouchEvent, useEffect, useRef, useState } from "react";
import type { AttachmentExtract, ImageAnalysisMode, SearchResult } from "../api/client";
import { api, attachmentUrl } from "../api/client";
import { AnalysisTab } from "../components/AnalysisTab";
import { Sheet } from "../components/Sheet";
import { IngestChip } from "../components/Stream";
import { TopBar } from "../components/TopBar";
import { ChevronRightIcon, FileIcon, ImageIcon, MoreIcon, PlusIcon } from "../components/icons";
import { awaitingImageCount } from "../notes/lifecycle";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import type { MoveTarget } from "../notes/useNoteActions";
import type { StreamAttachment, StreamItem, SyncStatus } from "../notes/useNotes";

export interface NoteViewSource {
  /** Server note id; null only for outbox rows that haven't synced yet. */
  id: string | null;
  domain: string;
  destination: string | null;
  body: string;
  createdAt: Date;
  ingestState: string | null;
  /** True once analysis finished — ends the header's lifecycle chip. */
  analyzed: boolean;
  /** null = unknown (search-result fallback until the full note resolves). */
  attachments: StreamAttachment[] | null;
  attachmentCount: number;
  /** True when built from a search result; the body is only a preview. */
  partial: boolean;
}

export function noteViewFromItem(item: StreamItem): NoteViewSource {
  return {
    id: item.id,
    domain: item.domain,
    destination: item.destination,
    body: item.body,
    createdAt: item.createdAt,
    ingestState: item.ingestState,
    analyzed: item.analyzed,
    attachments: item.attachments,
    attachmentCount: item.attachments.length,
    partial: false,
  };
}

export function noteViewFromSearch(result: SearchResult): NoteViewSource {
  return {
    id: result.note_id,
    domain: result.domain,
    destination: result.destination,
    body: result.body_preview,
    createdAt: new Date(result.created_at),
    // Unknown until the full note resolves; the null ingestState already
    // suppresses the lifecycle chip, so analyzed=false is inert here.
    ingestState: null,
    analyzed: false,
    attachments: null,
    attachmentCount: result.attachment_count,
    partial: true,
  };
}

function fmtBytes(n: number): string {
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** Minimal markdown: blank lines split paragraphs, single newlines break. */
function BodyParagraphs({ body }: { body: string }) {
  return (
    <div className="note-view-body">
      {body.split(/\n{2,}/).map((para, pi) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: paragraphs are static per body.
        <p key={pi}>
          {para.split("\n").map((line, li) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: lines are static per body.
            <Fragment key={li}>
              {li > 0 && <br />}
              {line}
            </Fragment>
          ))}
        </p>
      ))}
    </div>
  );
}

const SWIPE_DOWN_PX = 56;

/** Pipeline status, derived client-side: PDFs/text are searchable once the
 * note is indexed; images become searchable once the async OCR job has
 * filled the vision cache (hasExtracts), and the chip says when full
 * analysis also cached a description. */
function attachmentStatus(att: StreamAttachment, ingestState: string | null) {
  if (ingestState === "pending" || ingestState === "processing") {
    return { label: "indexing…", tone: "warn" as const };
  }
  if (att.mediaType.startsWith("image/")) {
    if (!att.hasExtracts) return { label: "ocr queued…", tone: "warn" as const };
    return att.hasDescription
      ? { label: "text + description", tone: "ok" as const }
      : { label: "text extracted (ocr)", tone: "ok" as const };
  }
  return { label: "text extracted", tone: "ok" as const };
}

/** ~6 lines fit the clamp; longer transcriptions grow in place. */
const OCR_CLAMP_LINES = 6;
const PDF_HINT_MS = 2600;

/** Verbatim OCR with the model's honesty marker rendered muted-italic. */
function OcrText({ text, all }: { text: string; all: boolean }) {
  return (
    <pre className={`x-text${all ? " all" : ""}`}>
      {text.split(/(\[illegible\])/).map((part, i) =>
        part === "[illegible]" ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: parts are static per text.
          <span key={i} className="x-illegible">
            [illegible]
          </span>
        ) : (
          // biome-ignore lint/suspicious/noArrayIndexKey: parts are static per text.
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </pre>
  );
}

function microMeta(extract: AttachmentExtract): string {
  const confidence =
    extract.confidence === null ? "" : ` · ${Math.round(extract.confidence * 100)}%`;
  return `${extract.kind} · ${extract.tool}${confidence}`;
}

interface ExpansionProps {
  attachmentId: string;
  extracts: AttachmentExtract[] | "loading" | null;
  mode: ImageAnalysisMode | null;
  analyzing: boolean;
  onAnalyze: () => void;
}

/** The unfolded image row (mock C): thumbnail strip, verbatim OCR inset,
 * the mined description beneath, and the on-demand analyze action. */
function ImageExpansion({ attachmentId, extracts, mode, analyzing, onAnalyze }: ExpansionProps) {
  const [showAll, setShowAll] = useState(false);
  if (extracts === "loading" || extracts === null) {
    return (
      <div className="x-inner">
        <p className="x-quiet">loading extraction…</p>
      </div>
    );
  }
  const ocr = extracts.find((e) => e.kind === "ocr") ?? null;
  const description = extracts.find((e) => e.kind === "caption" && e.text !== "") ?? null;
  const ocrLines = ocr ? ocr.text.split("\n").length : 0;
  return (
    <div className="x-inner">
      <div className="x-strip">
        <span className="x-thumb">
          <img src={attachmentUrl(attachmentId)} alt="" loading="lazy" />
        </span>
        <span className="x-strip-meta">
          <span className="x-label">extracted text</span>
          {ocr !== null && <span className="x-micro">{microMeta(ocr)}</span>}
          <a
            className="x-open-link"
            href={attachmentUrl(attachmentId)}
            target="_blank"
            rel="noreferrer"
          >
            open full image →
          </a>
        </span>
      </div>

      {ocr !== null && ocr.text !== "" ? (
        <>
          <OcrText text={ocr.text} all={showAll} />
          {ocrLines > OCR_CLAMP_LINES && (
            <button type="button" className="x-showall" onClick={() => setShowAll((v) => !v)}>
              {showAll ? "show less" : `show all ${ocrLines} lines`}
            </button>
          )}
        </>
      ) : (
        <p className="x-quiet">no legible text in this image.</p>
      )}

      {description !== null ? (
        <div className="x-desc-block">
          <span className="x-label">description</span>
          <p className="x-desc">{description.text}</p>
          <span className="x-micro">{microMeta(description)} · mined for facts in analysis</span>
        </div>
      ) : analyzing ? (
        <p className="x-quiet">analyzing image…</p>
      ) : (
        <div className="x-desc-block">
          {mode === "ocr" && (
            <span className="x-quiet">no description — image analysis is set to ocr only.</span>
          )}
          <button type="button" className="x-analyze" onClick={onAnalyze}>
            analyze image
          </button>
        </div>
      )}
    </div>
  );
}

interface AttachmentsTabProps {
  view: NoteViewSource;
  onAdd: (file: File) => Promise<void>;
  onRemove: (attachmentId: string) => Promise<void>;
}

function AttachmentsTab({ view, onAdd, onRemove }: AttachmentsTabProps) {
  const [sheetFor, setSheetFor] = useState<StreamAttachment | null>(null);
  const [removeArmed, setRemoveArmed] = useState(false);
  const [uploading, setUploading] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  // Inline expansion (settled in a three-way review — rows unfold in place):
  // extracts are fetched lazily on first expand, never inlined into notes.
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [extractsById, setExtractsById] = useState<Record<string, AttachmentExtract[] | "loading">>(
    {},
  );
  const [analyzingIds, setAnalyzingIds] = useState<readonly string[]>([]);
  const [pdfHintFor, setPdfHintFor] = useState<string | null>(null);
  const pdfHintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The global image-analysis mode, only to word the missing-description
  // state; load failures just suppress the "set to ocr only" line.
  const [mode, setMode] = useState<ImageAnalysisMode | null>(null);
  useEffect(() => {
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (!stale) setMode(s.image_analysis_mode);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);
  useEffect(
    () => () => {
      if (pdfHintTimer.current !== null) clearTimeout(pdfHintTimer.current);
    },
    [],
  );

  function toggleExpand(att: StreamAttachment) {
    if (att.id === null) return;
    const id = att.id;
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);
    if (extractsById[id] === undefined) {
      setExtractsById((m) => ({ ...m, [id]: "loading" }));
      api
        .attachmentExtracts(id)
        .then((rows) => setExtractsById((m) => ({ ...m, [id]: rows })))
        .catch(() => setExtractsById((m) => ({ ...m, [id]: [] })));
    }
  }

  function showPdfHint(att: StreamAttachment) {
    if (att.id === null) return;
    setPdfHintFor(att.id);
    if (pdfHintTimer.current !== null) clearTimeout(pdfHintTimer.current);
    pdfHintTimer.current = setTimeout(() => setPdfHintFor(null), PDF_HINT_MS);
  }

  function analyze(id: string) {
    // Optimistic: the row shows the calm in-flight line right away; a 409
    // just means a run is already in flight, which reads the same.
    setAnalyzingIds((ids) => [...ids, id]);
    api.analyzeAttachment(id).catch(() => {});
  }

  const attachments = view.attachments ?? [];
  const totalBytes = attachments.reduce((sum, a) => sum + a.sizeBytes, 0);
  const indexing = view.ingestState === "pending" || view.ingestState === "processing";
  const searchable = indexing
    ? 0
    : attachments.filter((a) => !a.mediaType.startsWith("image/") || a.hasExtracts).length;
  const awaitingOcr = awaitingImageCount(attachments);

  async function addFiles(list: FileList | null) {
    if (!list) return;
    for (const file of Array.from(list)) {
      setUploading((n) => n + 1);
      try {
        await onAdd(file);
      } catch {
        // The sync dot reports trouble; the row simply doesn't appear.
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  const summary = [
    `${attachments.length} file${attachments.length === 1 ? "" : "s"}`,
    fmtBytes(totalBytes),
    ...(indexing ? ["indexing…"] : []),
    ...(searchable > 0 ? [`${searchable} searchable`] : []),
    ...(awaitingOcr > 0 ? [`${awaitingOcr} awaiting ocr`] : []),
  ].join(" · ");

  return (
    <>
      {attachments.length > 0 && <p className="att-summary">{summary}</p>}
      {view.attachments === null && view.attachmentCount > 0 && (
        <p className="note-view-loading">loading attachments…</p>
      )}

      <div className="att-card">
        {attachments.map((att) => {
          const status = attachmentStatus(att, view.ingestState);
          const isImage = att.mediaType.startsWith("image/");
          const expandable = isImage && att.id !== null;
          const open = expandable && expandedId === att.id;
          return (
            <div key={att.id ?? att.filename} className={`att-item${open ? " open" : ""}`}>
              <div
                className="att-row"
                // biome-ignore lint/a11y/useSemanticElements: the row hosts the nested ⋯ button, which a real <button> cannot.
                role="button"
                tabIndex={0}
                aria-expanded={expandable ? open : undefined}
                onClick={() => (expandable ? toggleExpand(att) : showPdfHint(att))}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  e.preventDefault();
                  if (expandable) toggleExpand(att);
                  else showPdfHint(att);
                }}
              >
                <span className="att-icon">
                  {isImage ? <ImageIcon size={20} /> : <FileIcon size={20} />}
                </span>
                <span className="att-main">
                  <span className="att-name">{att.filename}</span>
                  <span className="att-meta">
                    {fmtBytes(att.sizeBytes)} · {att.mediaType}
                  </span>
                  <span className={`att-chip att-chip-${status.tone}`}>{status.label}</span>
                </span>
                {expandable && (
                  <span className="att-caret" aria-hidden="true">
                    <ChevronRightIcon size={16} />
                  </span>
                )}
                <button
                  type="button"
                  className="att-more-btn"
                  aria-label={`Actions for ${att.filename}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setRemoveArmed(false);
                    setSheetFor(att);
                  }}
                >
                  ⋯
                </button>
              </div>
              {open && att.id !== null && (
                <ImageExpansion
                  attachmentId={att.id}
                  extracts={extractsById[att.id] ?? null}
                  mode={mode}
                  analyzing={analyzingIds.includes(att.id)}
                  onAnalyze={() => att.id !== null && analyze(att.id)}
                />
              )}
              {!expandable && pdfHintFor !== null && pdfHintFor === att.id && (
                <p className="pdf-hint">
                  pdfs carry their own text layer — open the file to read it; nothing was ocr'd.
                </p>
              )}
            </div>
          );
        })}
        {uploading > 0 && (
          <div className="att-row">
            <span className="att-icon">
              <FileIcon size={20} />
            </span>
            <span className="att-main">
              <span className="att-name">uploading…</span>
              <span className="att-chip att-chip-warn">
                {uploading} file{uploading === 1 ? "" : "s"} in flight
              </span>
            </span>
          </div>
        )}
        {attachments.length === 0 && uploading === 0 && (
          <p className="att-empty">nothing attached — add a file below.</p>
        )}

        {view.id !== null && (
          <button type="button" className="att-add-row" onClick={() => fileRef.current?.click()}>
            <PlusIcon size={18} />
            <span>
              add files
              <span className="att-add-hint"> — pdfs and images become searchable</span>
            </span>
          </button>
        )}
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            void addFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {sheetFor !== null && (
        <Sheet title={sheetFor.filename} onClose={() => setSheetFor(null)}>
          {sheetFor.id !== null && (
            <a
              className="sheet-action"
              href={attachmentUrl(sheetFor.id)}
              target="_blank"
              rel="noreferrer"
              onClick={() => setSheetFor(null)}
            >
              open
            </a>
          )}
          <button
            type="button"
            className={`sheet-action sheet-action-danger${removeArmed ? " armed" : ""}`}
            onClick={() => {
              if (!removeArmed) {
                setRemoveArmed(true);
                return;
              }
              const id = sheetFor.id;
              setSheetFor(null);
              if (id !== null) void onRemove(id);
            }}
            onBlur={() => setRemoveArmed(false)}
          >
            {removeArmed ? "tap again — removes file + its extracted text" : "remove"}
          </button>
        </Sheet>
      )}
    </>
  );
}

interface NoteScreenProps {
  source: NoteViewSource;
  /** Cache-first full-note lookup for search-result openings. */
  resolve: (id: string) => Promise<StreamItem | null>;
  syncStatus: SyncStatus;
  onClose: () => void;
  onEdit: (
    id: string,
    body: string,
    domain: string,
    createdAt: Date,
    attachments: StreamAttachment[],
  ) => void;
  onMove: (target: MoveTarget) => void;
  onDelete: (id: string) => void;
  onAddAttachment: (noteId: string, file: File) => Promise<StreamAttachment>;
  onRemoveAttachment: (attachmentId: string) => Promise<void>;
  /** Analysis-tab entity chips open the entity layer above this one. */
  onOpenEntity: (entityId: string) => void;
}

export function NoteScreen({
  source,
  resolve,
  syncStatus,
  onClose,
  onEdit,
  onMove,
  onDelete,
  onAddAttachment,
  onRemoveAttachment,
  onOpenEntity,
}: NoteScreenProps) {
  const [view, setView] = useState(source);
  const [tab, setTab] = useState<"note" | "attachments" | "analysis">("note");

  // Keep the local view in step when App refreshes the source (saved edits,
  // attachment changes from the editor layer).
  useEffect(() => setView(source), [source]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const swipeStart = useRef<{ x: number; y: number } | null>(null);

  // A search opening only carries the preview; swap in the full note (body,
  // attachments, ingest state) once the cache/page-walk finds it.
  useEffect(() => {
    if (!source.partial || source.id === null) return;
    let stale = false;
    void resolve(source.id).then((item) => {
      if (!stale && item) setView({ ...noteViewFromItem(item), partial: false });
    });
    return () => {
      stale = true;
    };
  }, [source, resolve]);

  // Swipe-down at scroll-top climbs back, same as every card layer.
  function onTouchStart(event: TouchEvent) {
    if ((scrollerRef.current?.scrollTop ?? 0) > 4) {
      swipeStart.current = null;
      return;
    }
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }

  function onTouchMove(event: TouchEvent) {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dy = t.clientY - start.y;
    const dx = Math.abs(t.clientX - start.x);
    if (dy > SWIPE_DOWN_PX && dy > dx * 2) {
      swipeStart.current = null;
      onClose();
    }
  }

  const noteId = view.id;

  return (
    <div className="subscreen subscreen-note" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <TopBar title="Note" onBack={onClose} syncStatus={syncStatus} onBolt={onClose} />
      <div className="screen-body note-view" ref={scrollerRef}>
        <div className="note-view-head">
          <span
            className="domain-pill"
            style={{ color: DOMAIN_COLOR[view.domain] ?? "var(--steel)" }}
          >
            <span
              className="domain-dot"
              style={{ background: DOMAIN_COLOR[view.domain] ?? "var(--steel)" }}
            />
            {DOMAIN_TITLE[view.domain] ?? view.domain}
            {view.destination ? ` → ${view.destination}` : ""}
          </span>
          <span className="note-view-date">
            {view.createdAt.toLocaleDateString(undefined, {
              weekday: "short",
              month: "short",
              day: "numeric",
              year: "numeric",
            })}{" "}
            · {view.createdAt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}
          </span>
          <IngestChip item={{ ...view, attachments: view.attachments ?? [] }} />
          {noteId !== null && (
            <button
              type="button"
              className="note-menu-btn"
              aria-label="Note actions"
              onClick={() => {
                setDeleteArmed(false);
                setMenuOpen(true);
              }}
            >
              <MoreIcon size={20} />
            </button>
          )}
        </div>

        <div className="note-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "note"}
            className={`seg${tab === "note" ? " seg-on" : ""}`}
            onClick={() => setTab("note")}
          >
            Note
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "attachments"}
            className={`seg${tab === "attachments" ? " seg-on" : ""}`}
            onClick={() => setTab("attachments")}
          >
            Attachments
            {(view.attachments?.length ?? view.attachmentCount) > 0 && (
              <span className="tab-count">{view.attachments?.length ?? view.attachmentCount}</span>
            )}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "analysis"}
            className={`seg${tab === "analysis" ? " seg-on" : ""}`}
            onClick={() => setTab("analysis")}
          >
            Analysis
          </button>
        </div>

        {tab === "note" && (
          <>
            <BodyParagraphs body={view.body} />
            {view.partial && <p className="note-view-loading">loading the full note…</p>}
          </>
        )}
        {tab === "attachments" && (
          <AttachmentsTab
            view={view}
            onAdd={async (file) => {
              if (view.id === null) return;
              const added = await onAddAttachment(view.id, file);
              setView((v) => ({
                ...v,
                attachments: [...(v.attachments ?? []), added],
                attachmentCount: v.attachmentCount + 1,
              }));
            }}
            onRemove={async (attachmentId) => {
              await onRemoveAttachment(attachmentId);
              setView((v) => ({
                ...v,
                attachments: (v.attachments ?? []).filter((a) => a.id !== attachmentId),
                attachmentCount: Math.max(0, v.attachmentCount - 1),
              }));
            }}
          />
        )}
        {tab === "analysis" && <AnalysisTab noteId={noteId} onOpenEntity={onOpenEntity} />}
      </div>

      {menuOpen && noteId !== null && (
        <Sheet title="Note actions" onClose={() => setMenuOpen(false)}>
          <button
            type="button"
            className="sheet-action sheet-action-edit"
            onClick={() => {
              setMenuOpen(false);
              onEdit(noteId, view.body, view.domain, view.createdAt, view.attachments ?? []);
            }}
          >
            edit
          </button>
          <button
            type="button"
            className="sheet-action"
            onClick={() => {
              setMenuOpen(false);
              onMove({ id: noteId, domain: view.domain, destination: view.destination });
            }}
          >
            move domain
          </button>
          <button
            type="button"
            className={`sheet-action sheet-action-danger${deleteArmed ? " armed" : ""}`}
            onClick={() => {
              if (!deleteArmed) {
                setDeleteArmed(true);
                return;
              }
              setMenuOpen(false);
              onDelete(noteId);
            }}
            onBlur={() => setDeleteArmed(false)}
          >
            {deleteArmed ? "tap again — deletes this note" : "delete"}
          </button>
        </Sheet>
      )}
    </div>
  );
}
