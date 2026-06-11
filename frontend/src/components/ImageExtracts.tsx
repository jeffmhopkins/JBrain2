// Image-extract viewing for the Analysis tab's Sources card (settled in the
// sources-card review — variant B): OCR/description viewing and the per-image
// analyze action moved here from the Attachments tab, which is a pure
// manifest again. The expansion anatomy (mock C lineage) is unchanged:
// thumbnail strip, verbatim OCR inset, the mined description beneath.

import { Fragment, useCallback, useEffect, useState } from "react";
import type { AttachmentExtract, ImageAnalysisMode } from "../api/client";
import { api, attachmentUrl } from "../api/client";

export function fmtBytes(n: number): string {
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** ~6 lines fit the clamp; longer transcriptions grow in place. */
export const OCR_CLAMP_LINES = 6;

/** Verbatim OCR with the model's honesty marker rendered muted-italic. */
export function OcrText({ text, all }: { text: string; all: boolean }) {
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

export function microMeta(extract: AttachmentExtract): string {
  const confidence =
    extract.confidence === null ? "" : ` · ${Math.round(extract.confidence * 100)}%`;
  return `${extract.kind} · ${extract.tool}${confidence}`;
}

export type ExtractsState = AttachmentExtract[] | "loading";

export interface ImageExtractsApi {
  extractsById: Record<string, ExtractsState>;
  /** The global image-analysis mode, only to word the missing-description
   * state; load failures just suppress the "set to ocr only" line. */
  mode: ImageAnalysisMode | null;
  analyzingIds: readonly string[];
  analyze: (attachmentId: string) => void;
  /** Refetch every image's extracts — called when a fresh analysis lands. */
  refresh: () => void;
}

/** Owns the Sources card's vision-cache state: extracts are fetched eagerly
 * when the card mounts (every image row shows per-stage status up front),
 * per-image analyze runs optimistically, and the settings mode loads once. */
export function useImageExtracts(imageAttachmentIds: readonly string[]): ImageExtractsApi {
  const [extractsById, setExtractsById] = useState<Record<string, ExtractsState>>({});
  const [analyzingIds, setAnalyzingIds] = useState<readonly string[]>([]);
  const [mode, setMode] = useState<ImageAnalysisMode | null>(null);
  const idsKey = imageAttachmentIds.join("\n");

  const fetchExtracts = useCallback((ids: readonly string[]) => {
    for (const attId of ids) {
      setExtractsById((m) => (m[attId] === undefined ? { ...m, [attId]: "loading" } : m));
      api
        .attachmentExtracts(attId)
        .then((rows) => setExtractsById((m) => ({ ...m, [attId]: rows })))
        .catch(() => setExtractsById((m) => (m[attId] === "loading" ? { ...m, [attId]: [] } : m)));
    }
  }, []);

  useEffect(() => {
    const ids = idsKey === "" ? [] : idsKey.split("\n");
    fetchExtracts(ids);
    if (ids.length === 0) return;
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
  }, [idsKey, fetchExtracts]);

  const analyze = useCallback((attId: string) => {
    // Optimistic: the row shows the calm in-flight state right away; a 409
    // just means a run is already in flight, which reads the same.
    setAnalyzingIds((ids) => (ids.includes(attId) ? ids : [...ids, attId]));
    api.analyzeAttachment(attId).catch(() => {});
  }, []);

  const refresh = useCallback(() => {
    fetchExtracts(idsKey === "" ? [] : idsKey.split("\n"));
    setAnalyzingIds([]);
  }, [idsKey, fetchExtracts]);

  return { extractsById, mode, analyzingIds, analyze, refresh };
}

export type StageStatus = "done" | "running" | "queued" | "skipped" | "idle";

export interface ImageStages {
  ocr: StageStatus;
  description: StageStatus;
}

/** Per-stage status for a Sources image row. Fetched extracts are the
 * authoritative signal (they refresh when analysis lands); the manifest
 * flags only cover the moment before the eager fetch resolves. `settled`
 * means the note's analysis already landed and no re-run is in flight — a
 * still-missing stage is then terminal (analyze-anyway paths), never an
 * eternal spinner. */
export function imageStages(args: {
  attachment: { hasExtracts: boolean; hasDescription: boolean };
  extracts: ExtractsState | undefined;
  mode: ImageAnalysisMode | null;
  analyzing: boolean;
  settled: boolean;
}): ImageStages {
  const rows = Array.isArray(args.extracts) ? args.extracts : null;
  const ocrDone = rows ? rows.some((e) => e.kind === "ocr") : args.attachment.hasExtracts;
  const descDone = rows
    ? rows.some((e) => e.kind === "caption" && e.text !== "")
    : args.attachment.hasDescription;
  const ocr: StageStatus = ocrDone ? "done" : args.analyzing || !args.settled ? "running" : "idle";
  let description: StageStatus;
  if (descDone) description = "done";
  else if (args.analyzing) description = "running";
  else if (args.mode === "ocr") description = "skipped";
  else if (!ocrDone) description = "queued";
  else if (args.settled) description = "idle";
  else description = "running";
  return { ocr, description };
}

export interface ImageExpansionProps {
  attachmentId: string;
  extracts: ExtractsState | undefined;
  mode: ImageAnalysisMode | null;
  analyzing: boolean;
  /** Note analysis landed and this image isn't being re-run. */
  settled: boolean;
}

/** The unfolded image row: thumbnail strip, verbatim OCR inset, the mined
 * description beneath — with calm per-stage in-flight lines while the
 * pipeline is still working on this image. */
export function ImageExpansion({
  attachmentId,
  extracts,
  mode,
  analyzing,
  settled,
}: ImageExpansionProps) {
  const [showAll, setShowAll] = useState(false);
  if (extracts === "loading" || extracts === undefined) {
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
      ) : ocr !== null ? (
        <p className="x-quiet">no legible text in this image.</p>
      ) : analyzing || !settled ? (
        <p className="x-running">
          <span className="spin" aria-hidden="true" />
          reading image text…
        </p>
      ) : (
        <p className="x-quiet">no text extracted.</p>
      )}

      <div className="x-desc-block">
        <span className="x-label">description</span>
        {description !== null ? (
          <>
            <p className="x-desc">{description.text}</p>
            <span className="x-micro">{microMeta(description)} · mined for facts in analysis</span>
          </>
        ) : analyzing ? (
          <p className="x-running">
            <span className="spin" aria-hidden="true" />
            analyzing image…
          </p>
        ) : mode === "ocr" ? (
          <p className="x-quiet">no description — image analysis is set to ocr only.</p>
        ) : ocr === null ? (
          <p className="x-quiet">queued — runs after ocr.</p>
        ) : settled ? (
          <p className="x-quiet">no description cached — re-run image analysis to add one.</p>
        ) : (
          <p className="x-running">
            <span className="spin" aria-hidden="true" />
            describing image… facts wait for this.
          </p>
        )}
      </div>
    </div>
  );
}
