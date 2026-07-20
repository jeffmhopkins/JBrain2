// The Research Library (docs/reference/DESIGN.md "Research Library"; binding mock
// docs/mocks/research-library/b-segmented-tabs.html). The owner's browse door to the two
// external-corpus artifacts jerv produces — deep-research reports + analysed videos. A
// Reports/Videos segmented control switches purpose-built per-type lists; an as-you-type
// filter narrows the active tab. Reports show their short LLM title (falling back to the
// question); videos are grouped into collapsible per-channel sections with a thumbnail.
// A per-row ⋯ opens the ONE consolidated action sheet (view / open-in-jerv / copy /
// download / open-source / delete) — the detail layer is now pure reading. Delete is
// owner-initiated with a deferred-commit undo: the row leaves the list immediately and the
// server DELETE fires only when the undo window closes. Copy/download fetch the full item
// on demand (the listing carries no body). Amber research accent (read-only domain).
// Reachable from the launcher's Research tile; App hosts the detail layer (onOpen) and the
// jerv handoff (onOpenInJerv).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  type ReportListItem,
  type VideoDetail,
  type VideoListItem,
  api,
} from "../api/client";
import { Sheet } from "../components/Sheet";
import {
  ChevronRightIcon,
  ClipIcon,
  FileIcon,
  GlobeIcon,
  MessageIcon,
  MoreIcon,
  SearchIcon,
  TrashIcon,
  VideoIcon,
} from "../components/icons";

export type ResearchKind = "report" | "video";
type Item = { kind: "report"; row: ReportListItem } | { kind: "video"; row: VideoListItem };

/** The undo window (ms) before a soft-deleted row's server DELETE actually commits. */
const UNDO_MS = 4500;
/** How long the copy/download/open feedback toast lingers. */
const FLASH_MS = 3000;

function errMsg(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

function fmtDuration(s: number | null): string {
  if (s === null) return "";
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  return h > 0
    ? `${h}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${m}:${String(s % 60).padStart(2, "0")}`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function itemId(item: Item): string {
  return item.kind === "report" ? item.row.id : item.row.video_id;
}
/** The report's short display title (question fallback), or the video's title. */
function itemTitle(item: Item): string {
  return item.kind === "report" ? item.row.title || item.row.question : item.row.title;
}

/** The YouTube thumbnail for a video row, or null for a non-YouTube / id-less source. The
 * <VideoThumb> falls back to the placeholder icon when the image itself fails to load. */
function thumbUrl(row: VideoListItem): string | null {
  return row.provider === "youtube" && row.video_id
    ? `https://i.ytimg.com/vi/${encodeURIComponent(row.video_id)}/mqdefault.jpg`
    : null;
}

/** Copy text to the clipboard, resolving false when the clipboard API is unavailable
 * (an insecure context or a headless test) so the caller can report honestly. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Trigger a client-side download of `text` as `name` (a Blob + a transient <a>). */
function downloadText(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** The video's transcript as plain text — the word-level cues when present (what the card
 * renders), else the ordered passage windows — so Copy transcript matches what's shown. */
function videoTranscript(video: VideoDetail): string {
  const cued = video.cued_transcript?.words;
  if (cued && cued.length > 0) return cued.map((w) => w.text).join(" ");
  return video.windows.map((w) => w.text).join("\n");
}

/** A filesystem-safe short slug from a title, for the download filename. */
function slug(s: string): string {
  return (
    s
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48) || "report"
  );
}

/** The prose that seeds a jerv chat when "Open in jerv conversation" fires — a reference to
 * the item (its title + the video's URL), never its body. */
function jervSeed(item: Item): string {
  if (item.kind === "report") {
    return `Let's continue from my research report: "${item.row.title || item.row.question}".`;
  }
  return `Let's discuss the analysed video "${item.row.title}" (${item.row.url}).`;
}

interface ResearchScreenProps {
  /** Open the full-screen detail layer (App owns it). */
  onOpen: (kind: ResearchKind, id: string) => void;
  /** Seed the owner's current Research (jerv) conversation with a reference to this item. */
  onOpenInJerv: (text: string) => void;
  /** The undo window before a delete commits; injectable so tests don't wait 4.5s. */
  undoMs?: number;
}

export function ResearchScreen({ onOpen, onOpenInJerv, undoMs = UNDO_MS }: ResearchScreenProps) {
  const [tab, setTab] = useState<ResearchKind>("report");
  const [reports, setReports] = useState<ReportListItem[] | null>(null);
  const [videos, setVideos] = useState<VideoListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [menuFor, setMenuFor] = useState<Item | null>(null);
  // Which channel sections are collapsed (by channel name); default every section open.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // The undo snackbar is stamped with the pending delete's id so its lifetime tracks the
  // undo window (dismissed exactly when that delete commits) and overlapping deletes never
  // clear each other's toast.
  const [toast, setToast] = useState<{ id: string; msg: string } | null>(null);
  // A transient feedback line for copy/download/open (no undo) — a separate channel from the
  // delete snackbar so the two never fight over one slot.
  const [flash, setFlash] = useState<string | null>(null);

  // Deferred-commit deletes: id → the pending row, its original index, and its server commit
  // timer. On undo we cancel the timer and restore the row at its index; on unmount we flush
  // (commit) so nothing silently resurrects. A ref so it survives re-renders.
  const pending = useRef<
    Map<string, { item: Item; index: number; timer: ReturnType<typeof setTimeout> }>
  >(new Map());

  const load = useCallback(() => {
    setError(null);
    api
      .researchReports()
      .then((r) => setReports(r.items))
      .catch((e) => setError(errMsg(e)));
    api
      .researchVideos()
      .then((r) => setVideos(r.items))
      .catch((e) => setError(errMsg(e)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // On unmount, commit any still-pending delete (its undo window never closed on screen).
  useEffect(() => {
    const map = pending.current;
    return () => {
      for (const { item, timer } of map.values()) {
        clearTimeout(timer);
        commitDelete(item);
      }
      map.clear();
    };
  }, []);

  // Auto-dismiss the copy/download/open feedback toast.
  useEffect(() => {
    if (flash === null) return;
    const t = setTimeout(() => setFlash(null), FLASH_MS);
    return () => clearTimeout(t);
  }, [flash]);

  // Undo a still-pending delete: inert once the delete has committed (the entry is gone), so
  // a lingering tap can never resurrect an already-deleted row.
  function undoDelete(id: string) {
    const p = pending.current.get(id);
    if (!p) return;
    clearTimeout(p.timer);
    pending.current.delete(id);
    restoreLocally(p.item, p.index);
    setToast((t) => (t?.id === id ? null : t));
  }

  function commitDelete(item: Item) {
    const done =
      item.kind === "report"
        ? api.deleteResearchReport(item.row.id)
        : api.deleteResearchVideo(item.row.video_id);
    // Best-effort: a failed commit leaves the row gone locally; the next load reconciles.
    done.catch(() => {});
  }

  function removeLocally(item: Item) {
    if (item.kind === "report") setReports((rs) => rs?.filter((r) => r.id !== item.row.id) ?? rs);
    else setVideos((vs) => vs?.filter((v) => v.video_id !== item.row.video_id) ?? vs);
  }

  function restoreLocally(item: Item, index: number) {
    if (item.kind === "report") setReports((rs) => (rs ? insertAt(rs, index, item.row) : rs));
    else setVideos((vs) => (vs ? insertAt(vs, index, item.row) : vs));
  }

  function deleteItem(item: Item) {
    setMenuFor(null);
    const id = itemId(item);
    const list = item.kind === "report" ? reports : videos;
    const index = (list ?? []).findIndex((r) =>
      item.kind === "report"
        ? (r as ReportListItem).id === id
        : (r as VideoListItem).video_id === id,
    );
    removeLocally(item);
    const timer = setTimeout(() => {
      pending.current.delete(id);
      commitDelete(item);
      // Retire the snackbar when its own delete commits — undo is no longer possible.
      setToast((t) => (t?.id === id ? null : t));
    }, undoMs);
    pending.current.set(id, { item, index: index < 0 ? 0 : index, timer });
    setToast({ id, msg: `Deleted “${clip(itemTitle(item))}”.` });
  }

  // --- consolidated ⋯ actions -----------------------------------------------------------
  // Copy/download need the full item, which the listing omits, so they fetch it on demand.
  function openInJerv(item: Item) {
    setMenuFor(null);
    onOpenInJerv(jervSeed(item));
  }

  async function copyReport(row: ReportListItem) {
    setMenuFor(null);
    try {
      const report = await api.researchReport(row.id);
      setFlash(
        (await copyText(report.report_md))
          ? "Report copied."
          : "Couldn't copy — clipboard unavailable.",
      );
    } catch (e) {
      setFlash(errMsg(e));
    }
  }

  async function downloadReport(row: ReportListItem) {
    setMenuFor(null);
    try {
      const report = await api.researchReport(row.id);
      downloadText(`${slug(row.title || row.question)}.md`, report.report_md);
      setFlash("Downloading report.md…");
    } catch (e) {
      setFlash(errMsg(e));
    }
  }

  async function copyVideoText(row: VideoListItem, what: "summary" | "transcript") {
    setMenuFor(null);
    try {
      const video = await api.researchVideo(row.video_id);
      const text = what === "summary" ? video.summary : videoTranscript(video);
      const label = what === "summary" ? "Summary" : "Transcript";
      setFlash(
        (await copyText(text)) ? `${label} copied.` : "Couldn't copy — clipboard unavailable.",
      );
    } catch (e) {
      setFlash(errMsg(e));
    }
  }

  function openSource(row: VideoListItem) {
    setMenuFor(null);
    window.open(row.url, "_blank", "noopener,noreferrer");
  }

  function toggleChannel(channel: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(channel)) next.delete(channel);
      else next.add(channel);
      return next;
    });
  }

  const activeList = tab === "report" ? reports : videos;
  const filtered = useMemo<(ReportListItem | VideoListItem)[] | null>(() => {
    if (!activeList) return null;
    const q = query.trim().toLowerCase();
    if (!q) return activeList;
    return activeList.filter((it) =>
      tab === "report"
        ? `${(it as ReportListItem).title ?? ""} ${(it as ReportListItem).question}`
            .toLowerCase()
            .includes(q)
        : `${(it as VideoListItem).title} ${(it as VideoListItem).channel_name}`
            .toLowerCase()
            .includes(q),
    );
  }, [activeList, query, tab]);

  // The Videos tab is grouped into per-channel sections, sorted by channel name.
  const channelGroups = useMemo<{ channel: string; videos: VideoListItem[] }[] | null>(() => {
    if (tab !== "video" || !filtered) return null;
    return groupByChannel(filtered as VideoListItem[]);
  }, [tab, filtered]);

  const nReports = reports?.length ?? 0;
  const nVideos = videos?.length ?? 0;

  return (
    <main
      className="screen-body rl-screen"
      onTouchStart={(e) => e.stopPropagation()}
      onTouchMove={(e) => e.stopPropagation()}
    >
      <div className="seg-row rl-seg">
        <button
          type="button"
          className={`seg${tab === "report" ? " seg-on" : ""}`}
          aria-pressed={tab === "report"}
          onClick={() => setTab("report")}
        >
          <FileIcon size={16} /> Reports <span className="rl-seg-n">{nReports}</span>
        </button>
        <button
          type="button"
          className={`seg${tab === "video" ? " seg-on" : ""}`}
          aria-pressed={tab === "video"}
          onClick={() => setTab("video")}
        >
          <VideoIcon size={16} /> Videos <span className="rl-seg-n">{nVideos}</span>
        </button>
      </div>

      <div className="rl-searchbar">
        <SearchIcon size={18} />
        <input
          type="search"
          aria-label="Search this tab"
          placeholder={tab === "report" ? "search reports…" : "search videos…"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {error !== null && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {filtered === null && error === null ? (
        <p className="muted rl-empty">Loading…</p>
      ) : filtered && filtered.length === 0 ? (
        <p className="muted rl-empty">
          {query.trim()
            ? `Nothing matches “${query.trim()}” — try another term.`
            : tab === "report"
              ? "No reports yet — deep-research reports jerv saves show up here."
              : "No video analyses yet — analysed videos show up here."}
        </p>
      ) : tab === "report" ? (
        <div className="rl-list">
          {(filtered as ReportListItem[])?.map((row) => (
            <ReportRow
              key={row.id}
              row={row}
              onOpen={() => onOpen("report", row.id)}
              onMenu={() => setMenuFor({ kind: "report", row })}
            />
          ))}
        </div>
      ) : (
        <div className="rl-list">
          {channelGroups?.map((group) => {
            const isCollapsed = collapsed.has(group.channel);
            return (
              <div className="rl-group" key={group.channel}>
                <button
                  type="button"
                  className="rl-group-head"
                  aria-expanded={!isCollapsed}
                  onClick={() => toggleChannel(group.channel)}
                >
                  <span className={`rl-group-chev${isCollapsed ? "" : " rl-group-chev-open"}`}>
                    <ChevronRightIcon size={16} />
                  </span>
                  <span className="rl-group-name">{group.channel}</span>
                  <span className="rl-group-n">{group.videos.length}</span>
                </button>
                {!isCollapsed &&
                  group.videos.map((row) => (
                    <VideoRow
                      key={row.video_id}
                      row={row}
                      onOpen={() => onOpen("video", row.video_id)}
                      onMenu={() => setMenuFor({ kind: "video", row })}
                    />
                  ))}
              </div>
            );
          })}
        </div>
      )}

      {menuFor !== null && (
        <ActionSheet
          item={menuFor}
          onClose={() => setMenuFor(null)}
          onView={() => {
            const id = itemId(menuFor);
            setMenuFor(null);
            onOpen(menuFor.kind, id);
          }}
          onOpenInJerv={() => openInJerv(menuFor)}
          onCopyReport={() => void copyReport(menuFor.row as ReportListItem)}
          onDownloadReport={() => void downloadReport(menuFor.row as ReportListItem)}
          onCopySummary={() => void copyVideoText(menuFor.row as VideoListItem, "summary")}
          onCopyTranscript={() => void copyVideoText(menuFor.row as VideoListItem, "transcript")}
          onOpenSource={() => openSource(menuFor.row as VideoListItem)}
          onDelete={() => deleteItem(menuFor)}
        />
      )}

      {flash !== null && (
        <output className="rl-toast">
          <span className="rl-toast-msg">{flash}</span>
          <button type="button" className="rl-toast-undo" onClick={() => setFlash(null)}>
            Dismiss
          </button>
        </output>
      )}

      {toast !== null && (
        <output className="rl-toast">
          <TrashIcon size={15} />
          <span className="rl-toast-msg">{toast.msg}</span>
          <button type="button" className="rl-toast-undo" onClick={() => undoDelete(toast.id)}>
            Undo
          </button>
        </output>
      )}
    </main>
  );
}

function insertAt<T>(list: T[], index: number, item: T): T[] {
  const next = list.slice();
  next.splice(Math.min(index, next.length), 0, item);
  return next;
}

function clip(s: string): string {
  return s.length > 34 ? `${s.slice(0, 33)}…` : s;
}

/** Group videos into per-channel sections sorted by channel name (case-insensitive); rows
 * keep the server's newest-first order within a section. */
function groupByChannel(videos: VideoListItem[]): { channel: string; videos: VideoListItem[] }[] {
  const map = new Map<string, VideoListItem[]>();
  for (const v of videos) {
    const channel = v.channel_name || "Unknown channel";
    const bucket = map.get(channel);
    if (bucket) bucket.push(v);
    else map.set(channel, [v]);
  }
  return [...map.entries()]
    .map(([channel, vids]) => ({ channel, videos: vids }))
    .sort((a, b) => a.channel.localeCompare(b.channel, undefined, { sensitivity: "base" }));
}

function ReportRow({
  row,
  onOpen,
  onMenu,
}: { row: ReportListItem; onOpen: () => void; onMenu: () => void }) {
  return (
    <div className="rl-card">
      <button type="button" className="rl-card-body" onClick={onOpen}>
        <span className="rl-disc rl-disc-report" aria-hidden="true">
          <FileIcon size={18} />
        </span>
        <span className="rl-main">
          {/* The short LLM title, falling back to the raw question until it lands. */}
          <span className="rl-title">{row.title || row.question}</span>
          <span className="rl-badges">
            <span className={`rl-cx rl-cx-${row.complexity}`}>{row.complexity}</span>
          </span>
          <span className="rl-chips">
            <span className="rl-chip">{row.sub_agents} agents</span>
            <span className="rl-chip">
              {row.rounds} round{row.rounds === 1 ? "" : "s"}
            </span>
            <span className="rl-foot">
              <span className="rl-dot" aria-hidden="true" />
              research · {fmtDate(row.created_at)}
            </span>
          </span>
        </span>
        <ChevronRightIcon size={16} />
      </button>
      <button type="button" className="rl-kebab" onClick={onMenu} aria-label="Report actions">
        <MoreIcon size={18} />
      </button>
    </div>
  );
}

function VideoRow({
  row,
  onOpen,
  onMenu,
}: { row: VideoListItem; onOpen: () => void; onMenu: () => void }) {
  return (
    <div className="rl-card rl-card-video">
      <button type="button" className="rl-card-body" onClick={onOpen}>
        <VideoThumb row={row} />
        <span className="rl-main">
          <span className="rl-title rl-title-video">{row.title}</span>
          <span className="rl-foot">
            <span className="rl-dot" aria-hidden="true" />
            {fmtDate(row.published_at)}
          </span>
        </span>
      </button>
      <button type="button" className="rl-kebab" onClick={onMenu} aria-label="Video actions">
        <MoreIcon size={18} />
      </button>
    </div>
  );
}

/** The video thumbnail: the provider's still when we can address one, falling back to the
 * camera glyph when there's no thumbnail URL or the image itself fails to load. */
function VideoThumb({ row }: { row: VideoListItem }) {
  const src = thumbUrl(row);
  const [failed, setFailed] = useState(false);
  return (
    <span className="rl-thumb" aria-hidden="true">
      {src && !failed ? (
        <img
          className="rl-thumb-img"
          src={src}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
      ) : (
        <VideoIcon size={22} />
      )}
      {row.duration_s !== null && (
        <span className="rl-thumb-dur">{fmtDuration(row.duration_s)}</span>
      )}
    </span>
  );
}

/** The single consolidated per-item action sheet: view, open-in-jerv, the type-specific
 * copy/download/open-source, and a tap-again delete. Report bodies + video summaries/
 * transcripts are fetched on demand by the parent (the listing carries no body). */
function ActionSheet({
  item,
  onClose,
  onView,
  onOpenInJerv,
  onCopyReport,
  onDownloadReport,
  onCopySummary,
  onCopyTranscript,
  onOpenSource,
  onDelete,
}: {
  item: Item;
  onClose: () => void;
  onView: () => void;
  onOpenInJerv: () => void;
  onCopyReport: () => void;
  onDownloadReport: () => void;
  onCopySummary: () => void;
  onCopyTranscript: () => void;
  onOpenSource: () => void;
  onDelete: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const kindWord = item.kind === "report" ? "report" : "video";
  return (
    <Sheet title={itemTitle(item)} onClose={onClose}>
      <div className="rl-actions">
        <button type="button" className="rl-action" onClick={onView}>
          <ChevronRightIcon size={19} /> View
        </button>
        <button type="button" className="rl-action" onClick={onOpenInJerv}>
          <MessageIcon size={19} /> Open in jerv conversation
        </button>
        {item.kind === "report" ? (
          <>
            <button type="button" className="rl-action" onClick={onCopyReport}>
              <ClipIcon size={19} /> Copy report
            </button>
            <button type="button" className="rl-action" onClick={onDownloadReport}>
              <FileIcon size={19} /> Download report (.md)
            </button>
          </>
        ) : (
          <>
            <button type="button" className="rl-action" onClick={onCopySummary}>
              <ClipIcon size={19} /> Copy summary
            </button>
            <button type="button" className="rl-action" onClick={onCopyTranscript}>
              <ClipIcon size={19} /> Copy transcript
            </button>
            {item.row.url && (
              <button type="button" className="rl-action" onClick={onOpenSource}>
                <GlobeIcon size={19} /> Open source ↗
              </button>
            )}
          </>
        )}
        <button
          type="button"
          className={`rl-action rl-action-del${armed ? " rl-action-armed" : ""}`}
          onClick={() => (armed ? onDelete() : setArmed(true))}
        >
          <TrashIcon size={19} />
          {armed ? `Tap again — deletes this ${kindWord}` : "Delete"}
        </button>
      </div>
    </Sheet>
  );
}
