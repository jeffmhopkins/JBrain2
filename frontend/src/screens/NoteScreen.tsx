// Note view layer (docs/DESIGN.md "Note view"): a slide-up tree level over
// home or search with a Note / Analysis tab split. Pre-Phase-3 the header is
// domain + date only (no title), and Analysis shows the phased placeholders.

import { Fragment, type TouchEvent, useEffect, useRef, useState } from "react";
import type { SearchResult } from "../api/client";
import { attachmentUrl } from "../api/client";
import { IngestChip } from "../components/Stream";
import { TopBar } from "../components/TopBar";
import { FileIcon, ImageIcon } from "../components/icons";
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
    ingestState: null,
    attachments: null,
    attachmentCount: result.attachment_count,
    partial: true,
  };
}

function fmtBytes(n: number): string {
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

function AttachmentCardBody({ att }: { att: StreamAttachment }) {
  const Ic = att.mediaType.startsWith("image/") ? ImageIcon : FileIcon;
  return (
    <>
      <span className="attachment-icon">
        <Ic size={24} />
      </span>
      <span className="attachment-name">{att.filename}</span>
      <span className="attachment-size">{fmtBytes(att.sizeBytes)}</span>
    </>
  );
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

const ANALYSIS_SECTIONS: { header: string; phase: number }[] = [
  { header: "Tags", phase: 3 },
  { header: "Salient facts", phase: 3 },
  { header: "Entities", phase: 3 },
  { header: "Wiki backlinks", phase: 6 },
];

function AnalysisTab() {
  return (
    <div className="analysis-tab">
      {ANALYSIS_SECTIONS.map((section) => (
        <section key={section.header}>
          <h3 className="section-header">{section.header}</h3>
          <p className="empty-row">arrives in Phase {section.phase}</p>
        </section>
      ))}
      <p className="provenance-foot">
        extraction provenance — model, prompt version, analyzed-when — arrives in Phase 3
      </p>
    </div>
  );
}

const SWIPE_DOWN_PX = 56;

interface NoteScreenProps {
  source: NoteViewSource;
  /** Cache-first full-note lookup for search-result openings. */
  resolve: (id: string) => Promise<StreamItem | null>;
  syncStatus: SyncStatus;
  onClose: () => void;
  onEdit: (id: string, body: string) => void;
  onMove: (target: MoveTarget) => void;
  onDelete: (id: string) => void;
}

export function NoteScreen({
  source,
  resolve,
  syncStatus,
  onClose,
  onEdit,
  onMove,
  onDelete,
}: NoteScreenProps) {
  const [view, setView] = useState(source);
  const [tab, setTab] = useState<"note" | "analysis">("note");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
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
          <IngestChip state={view.ingestState} />
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
            aria-selected={tab === "analysis"}
            className={`seg${tab === "analysis" ? " seg-on" : ""}`}
            onClick={() => setTab("analysis")}
          >
            Analysis
          </button>
        </div>

        {tab === "note" ? (
          <>
            <BodyParagraphs body={view.body} />
            {view.partial && <p className="note-view-loading">loading the full note…</p>}
            {view.attachments?.map((att) =>
              att.id ? (
                <a
                  key={att.id}
                  className="attachment-card"
                  href={attachmentUrl(att.id)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <AttachmentCardBody att={att} />
                </a>
              ) : (
                <span key={att.filename} className="attachment-card">
                  <AttachmentCardBody att={att} />
                </span>
              ),
            )}
            {view.attachments === null && view.attachmentCount > 0 && (
              <p className="note-view-loading">
                {view.attachmentCount} attachment{view.attachmentCount > 1 ? "s" : ""} — loading…
              </p>
            )}

            {noteId !== null && (
              <div className="note-actions">
                <button
                  type="button"
                  className="action-btn action-edit"
                  onClick={() => onEdit(noteId, view.body)}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="action-btn action-move"
                  onClick={() =>
                    onMove({ id: noteId, domain: view.domain, destination: view.destination })
                  }
                >
                  Move domain
                </button>
                <button
                  type="button"
                  className="action-btn action-delete"
                  onClick={() => {
                    if (!confirmingDelete) {
                      setConfirmingDelete(true);
                      return;
                    }
                    onDelete(noteId);
                  }}
                  onBlur={() => setConfirmingDelete(false)}
                >
                  {confirmingDelete ? "Tap again to confirm" : "Delete"}
                </button>
              </div>
            )}
          </>
        ) : (
          <AnalysisTab />
        )}
      </div>
    </div>
  );
}
