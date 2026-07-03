// Note view layer (docs/reference/DESIGN.md "Note view"): a slide-up tree level over
// home or search with Note / Attachments / Analysis tabs. Attachments is the
// canonical manager (manifest rows + per-file sheet); pre-Phase-3 the header
// is domain + date only (no title), and Analysis shows phased placeholders.

import { Fragment, type TouchEvent, useEffect, useRef, useState } from "react";
import type { SearchResult } from "../api/client";
import { attachmentUrl } from "../api/client";
import { AnalysisTab } from "../components/AnalysisTab";
import { fmtBytes } from "../components/ImageExtracts";
import { Sheet } from "../components/Sheet";
import { IngestChip } from "../components/Stream";
import { TopBar } from "../components/TopBar";
import { FileIcon, ImageIcon, MoreIcon, PlusIcon } from "../components/icons";
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
  /** "human" or "agent"; a search-result fallback assumes "human" until the
   * full note resolves. Drives the header's "assistant" tag. */
  provenance: string;
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
    provenance: item.provenance,
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
    provenance: "human",
    attachments: null,
    attachmentCount: result.attachment_count,
    partial: true,
  };
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

const SWIPE_DOWN_PX = 112;

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

interface AttachmentsTabProps {
  view: NoteViewSource;
  onAdd: (file: File) => Promise<void>;
  onRemove: (attachmentId: string) => Promise<void>;
}

// The canonical attachment manager is a pure manifest (settled twice: the
// manifest review, then the Sources-card review moved extract viewing to
// the Analysis tab): inert rows with status chips + the per-file ⋯ sheet.
function AttachmentsTab({ view, onAdd, onRemove }: AttachmentsTabProps) {
  const [sheetFor, setSheetFor] = useState<StreamAttachment | null>(null);
  const [removeArmed, setRemoveArmed] = useState(false);
  const [uploading, setUploading] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);

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
          return (
            <div key={att.id ?? att.filename} className="att-row">
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
              <button
                type="button"
                className="att-more-btn"
                aria-label={`Actions for ${att.filename}`}
                onClick={() => {
                  setRemoveArmed(false);
                  setSheetFor(att);
                }}
              >
                ⋯
              </button>
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
  // Analysis is the most useful surface once a note exists, so it opens first;
  // the Note body is one tap away.
  const [tab, setTab] = useState<"note" | "attachments" | "analysis">("analysis");

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
          {view.provenance === "agent" && (
            <span className="note-by-assistant">prepared by assistant</span>
          )}
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
        {tab === "analysis" && (
          <AnalysisTab
            noteId={noteId}
            attachments={view.attachments}
            ingestState={view.ingestState}
            bodyChars={view.body.length}
            onOpenEntity={onOpenEntity}
          />
        )}
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
