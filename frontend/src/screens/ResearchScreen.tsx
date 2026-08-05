// The Research Library (docs/reference/DESIGN.md "Research Library"; binding mock
// docs/mocks/research-library/b-segmented-tabs.html). The owner's browse door to the two
// external-corpus artifacts jerv produces — deep-research reports + analysed videos. A
// Reports/Videos segmented control switches purpose-built per-type lists; an as-you-type
// filter narrows the active tab. Reports show their short LLM title (falling back to the
// question); videos are grouped into collapsible per-channel sections with a thumbnail.
// Reports fold into owner-named folders (app.report_groups, migration 0149) — a ⋯ →
// "Move to folder" sheet files a report (or spins up a new folder), browse mode renders
// each folder as a collapsible section over a trailing Ungrouped one (collapse persisted
// device-local via research/collapsed), and an Organize toggle arms folder rename/delete;
// a search flattens across folders. A per-row ⋯ opens the ONE consolidated action sheet
// (view / open-in-jerv / move / copy / download / open-source / delete) — the detail layer
// is now pure reading. Delete is
// owner-initiated with a deferred-commit undo: the row leaves the list immediately and the
// server DELETE fires only when the undo window closes. Copy/download fetch the full item
// on demand (the listing carries no body). Amber research accent (read-only domain).
// Reachable from the launcher's Research tile; App hosts the detail layer (onOpen) and the
// jerv handoff (onOpenInJerv).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  type MintedShare,
  type ReportGroup,
  type ReportListItem,
  type ShareLinkView,
  type VideoDetail,
  type VideoListItem,
  api,
} from "../api/client";
import { MoveReportSheet } from "../components/MoveReportSheet";
import { RenameReportSheet } from "../components/RenameReportSheet";
import { Sheet } from "../components/Sheet";
import {
  ChevronRightIcon,
  ClipIcon,
  FileIcon,
  GlobeIcon,
  LinkIcon,
  ListIcon,
  MessageIcon,
  MoreIcon,
  PencilIcon,
  ReorderIcon,
  SearchIcon,
  TrashIcon,
  VideoIcon,
} from "../components/icons";
import { UNGROUPED_KEY, loadCollapsed, writeCollapsed } from "../research/collapsed";
import { researchShareUrl } from "../research/share";

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
  // The owner's report folders + which are collapsed on this device (persisted, like the
  // Tasks screen). Folders show only in Reports browse mode; a search flattens across them.
  const [reportGroups, setReportGroups] = useState<ReportGroup[]>([]);
  const [folderCollapsed, setFolderCollapsed] = useState<Set<string>>(loadCollapsed);
  // Organize mode arms folder rename / delete (headers force-expand so nothing you're
  // editing is hidden), mirroring the Tasks "Organize" toggle.
  const [organizing, setOrganizing] = useState(false);
  const [moveFor, setMoveFor] = useState<ReportListItem | null>(null);
  // The report whose title the RenameReportSheet is editing (null = closed).
  const [renameFor, setRenameFor] = useState<ReportListItem | null>(null);
  // The report or folder whose public share links the ShareSheet is managing (null = closed).
  const [shareFor, setShareFor] = useState<ShareTarget | null>(null);
  const [renamingFolder, setRenamingFolder] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [armedFolderDelete, setArmedFolderDelete] = useState<string | null>(null);
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
    // Folders are a browse convenience; a failed load just leaves the Reports tab flat
    // (never blanks the reports themselves).
    api
      .researchReportGroups()
      .then(setReportGroups)
      .catch(() => {});
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

  // --- report folders ------------------------------------------------------------------

  // Fold/unfold a folder, persisting the choice on this device (keyed by folder id, or the
  // Ungrouped sentinel). Organize mode force-expands, so this only fires from browse mode.
  function toggleFolder(bucket: string | null) {
    const key = bucket ?? UNGROUPED_KEY;
    setFolderCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      writeCollapsed(next);
      return next;
    });
  }

  // Optimistically file a report into `groupId` (null = Ungrouped); revert + surface on
  // failure. The listing carries no order within a folder, so nothing else shifts.
  async function moveReportTo(report: ReportListItem, groupId: string | null) {
    setMoveFor(null);
    if (report.group_id === groupId) return;
    const prev = report.group_id;
    setReports(
      (rs) => rs?.map((r) => (r.id === report.id ? { ...r, group_id: groupId } : r)) ?? rs,
    );
    const label =
      groupId === null
        ? "Ungrouped"
        : (reportGroups.find((g) => g.id === groupId)?.name ?? "folder");
    try {
      await api.moveReport(report.id, groupId);
      setFlash(`Moved to “${label}”.`);
    } catch (e) {
      setReports((rs) => rs?.map((r) => (r.id === report.id ? { ...r, group_id: prev } : r)) ?? rs);
      setFlash(errMsg(e));
    }
  }

  // Optimistically rename a report's display title; revert + surface on failure.
  async function renameReport(report: ReportListItem, title: string) {
    setRenameFor(null);
    if (title === (report.title ?? "")) return;
    const prev = report.title;
    setReports((rs) => rs?.map((r) => (r.id === report.id ? { ...r, title } : r)) ?? rs);
    try {
      await api.renameResearchReport(report.id, title);
      setFlash("Report renamed.");
    } catch (e) {
      setReports((rs) => rs?.map((r) => (r.id === report.id ? { ...r, title: prev } : r)) ?? rs);
      setFlash(errMsg(e));
    }
  }

  // Create a folder and file the report into it in one tap (the move sheet's "New folder…").
  async function createFolderAndMove(report: ReportListItem, name: string) {
    setMoveFor(null);
    try {
      const g = await api.createReportGroup(name);
      setReportGroups((gs) => [...gs, g]);
      setReports((rs) => rs?.map((r) => (r.id === report.id ? { ...r, group_id: g.id } : r)) ?? rs);
      await api.moveReport(report.id, g.id);
      setFlash(`Created “${g.name}” — report moved.`);
    } catch (e) {
      setFlash(errMsg(e));
      load();
    }
  }

  async function commitFolderRename(groupId: string) {
    const name = renameText.trim();
    setRenamingFolder(null);
    const current = reportGroups.find((g) => g.id === groupId);
    if (!name || name === current?.name) return;
    setReportGroups((gs) => gs.map((g) => (g.id === groupId ? { ...g, name } : g)));
    try {
      await api.renameReportGroup(groupId, name);
    } catch (e) {
      setFlash(errMsg(e));
      load();
    }
  }

  // Delete a folder: its reports fall to Ungrouped (server SET NULL); mirror that locally.
  async function deleteFolder(groupId: string) {
    setArmedFolderDelete(null);
    setReportGroups((gs) => gs.filter((g) => g.id !== groupId));
    setReports(
      (rs) => rs?.map((r) => (r.group_id === groupId ? { ...r, group_id: null } : r)) ?? rs,
    );
    try {
      await api.deleteReportGroup(groupId);
      setFlash("Folder deleted — its reports moved to Ungrouped.");
    } catch (e) {
      setFlash(errMsg(e));
      load();
    }
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

  // The Reports tab, bucketed into the owner's folders + a trailing Ungrouped list. A
  // report whose folder was deleted (its group_id no longer names a known folder) falls to
  // Ungrouped, so a stale id can never strand a report out of view.
  const reportFolders = useMemo<{
    byGroup: Map<string, ReportListItem[]>;
    ungrouped: ReportListItem[];
  } | null>(() => {
    if (tab !== "report" || !filtered) return null;
    const known = new Set(reportGroups.map((g) => g.id));
    const byGroup = new Map<string, ReportListItem[]>();
    const ungrouped: ReportListItem[] = [];
    for (const r of filtered as ReportListItem[]) {
      const bucket = r.group_id && known.has(r.group_id) ? byGroup.get(r.group_id) : undefined;
      if (r.group_id && known.has(r.group_id)) {
        if (bucket) bucket.push(r);
        else byGroup.set(r.group_id, [r]);
      } else {
        ungrouped.push(r);
      }
    }
    return { byGroup, ungrouped };
  }, [tab, filtered, reportGroups]);

  // Folders show only while browsing (an active search flattens across every folder into
  // one result list, like the video tab).
  const showReportFolders = tab === "report" && !query.trim() && reportGroups.length > 0;

  // One report folder as a section: a collapsible header (a rename/delete row in organize
  // mode) over its rows. The Ungrouped bucket has no header actions and hides itself when
  // empty, so a fully-filed library shows only its real folders.
  function renderReportFolder(group: ReportGroup | null, rows: ReportListItem[]) {
    if (group === null && rows.length === 0) return null;
    const key = group?.id ?? UNGROUPED_KEY;
    const name = group?.name ?? "Ungrouped";
    const isCollapsed = !organizing && folderCollapsed.has(key);
    const canEdit = organizing && group !== null;
    return (
      <div className="rl-group" key={key}>
        <div className={`rl-group-hdr${canEdit ? " rl-group-hdr-edit" : ""}`}>
          {canEdit && renamingFolder === group.id ? (
            <input
              className="rl-group-rename"
              // biome-ignore lint/a11y/noAutofocus: the header morphed into an input on the user's tap.
              autoFocus
              aria-label="Rename folder"
              value={renameText}
              onChange={(e) => setRenameText(e.target.value)}
              onBlur={() => void commitFolderRename(group.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void commitFolderRename(group.id);
                if (e.key === "Escape") setRenamingFolder(null);
              }}
            />
          ) : organizing ? (
            <span className="rl-group-name rl-group-name-static">{name}</span>
          ) : (
            <button
              type="button"
              className="rl-group-head"
              aria-expanded={!isCollapsed}
              aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${name}`}
              onClick={() => toggleFolder(group?.id ?? null)}
            >
              <span className={`rl-group-chev${isCollapsed ? "" : " rl-group-chev-open"}`}>
                <ChevronRightIcon size={16} />
              </span>
              <span className="rl-group-name">{name}</span>
            </button>
          )}
          <span className="rl-group-n">{rows.length}</span>
          {!organizing && group !== null && (
            <button
              type="button"
              className="rl-group-share"
              aria-label={`Share ${name}`}
              onClick={() => setShareFor({ kind: "group", group })}
            >
              <LinkIcon size={14} /> Share
            </button>
          )}
          {canEdit && renamingFolder !== group.id && (
            <span className="rl-group-acts">
              <button
                type="button"
                className="rl-group-act"
                aria-label={`Rename ${name}`}
                onClick={() => {
                  setRenameText(name);
                  setRenamingFolder(group.id);
                }}
              >
                <PencilIcon size={15} />
              </button>
              <button
                type="button"
                className={`rl-group-act${armedFolderDelete === group.id ? " rl-group-act-armed" : ""}`}
                aria-label={
                  armedFolderDelete === group.id ? `Confirm delete ${name}` : `Delete ${name}`
                }
                onClick={() =>
                  armedFolderDelete === group.id
                    ? void deleteFolder(group.id)
                    : setArmedFolderDelete(group.id)
                }
              >
                {armedFolderDelete === group.id ? "delete?" : <TrashIcon size={15} />}
              </button>
            </span>
          )}
        </div>
        {!isCollapsed &&
          rows.map((row) => (
            <ReportRow
              key={row.id}
              row={row}
              onOpen={() => onOpen("report", row.id)}
              onMenu={() => setMenuFor({ kind: "report", row })}
            />
          ))}
      </div>
    );
  }

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

      {tab === "report" && !query.trim() && reportGroups.length > 0 && (
        <div className="rl-toolbar">
          <button
            type="button"
            className={`rl-organize${organizing ? " rl-organize-on" : ""}`}
            aria-pressed={organizing}
            onClick={() => {
              setOrganizing((o) => !o);
              setRenamingFolder(null);
              setArmedFolderDelete(null);
            }}
          >
            <ReorderIcon size={16} />
            {organizing ? "Done" : "Organize folders"}
          </button>
        </div>
      )}

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
        showReportFolders && reportFolders ? (
          <div className="rl-list">
            {reportGroups.map((g) => renderReportFolder(g, reportFolders.byGroup.get(g.id) ?? []))}
            {renderReportFolder(null, reportFolders.ungrouped)}
          </div>
        ) : (
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
        )
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
          onRename={() => {
            const row = menuFor.row as ReportListItem;
            setMenuFor(null);
            setRenameFor(row);
          }}
          onMoveToFolder={() => {
            const row = menuFor.row as ReportListItem;
            setMenuFor(null);
            setMoveFor(row);
          }}
          onShare={() => {
            const row = menuFor.row as ReportListItem;
            setMenuFor(null);
            setShareFor({ kind: "report", row });
          }}
          onCopyReport={() => void copyReport(menuFor.row as ReportListItem)}
          onDownloadReport={() => void downloadReport(menuFor.row as ReportListItem)}
          onCopySummary={() => void copyVideoText(menuFor.row as VideoListItem, "summary")}
          onCopyTranscript={() => void copyVideoText(menuFor.row as VideoListItem, "transcript")}
          onOpenSource={() => openSource(menuFor.row as VideoListItem)}
          onDelete={() => deleteItem(menuFor)}
        />
      )}

      {renameFor !== null && (
        <RenameReportSheet
          currentTitle={renameFor.title || renameFor.question}
          onRename={(title) => void renameReport(renameFor, title)}
          onClose={() => setRenameFor(null)}
        />
      )}

      {moveFor !== null && (
        <MoveReportSheet
          reportTitle={moveFor.title || moveFor.question}
          currentGroupId={moveFor.group_id}
          groups={reportGroups}
          onMove={(gid) => void moveReportTo(moveFor, gid)}
          onCreateAndMove={(name) => void createFolderAndMove(moveFor, name)}
          onClose={() => setMoveFor(null)}
        />
      )}

      {shareFor !== null && (
        <ShareSheet target={shareFor} onClose={() => setShareFor(null)} onFlash={setFlash} />
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

/** Group videos into per-channel sections sorted by channel name (case-insensitive); within
 * a section rows are ordered newest-published first (reverse chronological), so a channel's
 * videos read as a timeline rather than in the order they happened to be analysed. A row with
 * no publish date sorts to the bottom of its section. */
function groupByChannel(videos: VideoListItem[]): { channel: string; videos: VideoListItem[] }[] {
  const map = new Map<string, VideoListItem[]>();
  for (const v of videos) {
    const channel = v.channel_name || "Unknown channel";
    const bucket = map.get(channel);
    if (bucket) bucket.push(v);
    else map.set(channel, [v]);
  }
  return [...map.entries()]
    .map(([channel, vids]) => ({ channel, videos: [...vids].sort(byPublishedDesc) }))
    .sort((a, b) => a.channel.localeCompare(b.channel, undefined, { sensitivity: "base" }));
}

/** Newest-published first; a missing/unparseable publish date sorts last (treated as -∞). */
function byPublishedDesc(a: VideoListItem, b: VideoListItem): number {
  return publishedMs(b) - publishedMs(a);
}
function publishedMs(v: VideoListItem): number {
  const t = v.published_at ? new Date(v.published_at).getTime() : Number.NaN;
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

function ReportRow({
  row,
  onOpen,
  onMenu,
}: { row: ReportListItem; onOpen: () => void; onMenu: () => void }) {
  return (
    <div className="rl-card rl-card-report">
      <button type="button" className="rl-card-body" onClick={onOpen}>
        <span className="rl-rglyph" aria-hidden="true">
          <FileIcon size={17} />
        </span>
        <span className="rl-main">
          {/* The short LLM title, falling back to the raw question until it lands. */}
          <span className="rl-title rl-title-report">{row.title || row.question}</span>
          {/* One muted meta line: complexity (coloured word) · agents · rounds · date. */}
          <span className="rl-subline">
            <span className={`rl-cxword rl-cxword-${row.complexity}`}>{row.complexity}</span>
            {" · "}
            <span>{row.sub_agents} agents</span>
            {" · "}
            <span>
              {row.rounds} round{row.rounds === 1 ? "" : "s"}
            </span>
            {" · "}
            <span>{fmtDate(row.created_at)}</span>
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
  onRename,
  onMoveToFolder,
  onShare,
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
  onRename: () => void;
  onMoveToFolder: () => void;
  onShare: () => void;
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
            <button type="button" className="rl-action" onClick={onRename}>
              <PencilIcon size={19} /> Rename
            </button>
            <button type="button" className="rl-action" onClick={onMoveToFolder}>
              <ListIcon size={19} /> Move to folder
            </button>
            <button type="button" className="rl-action" onClick={onShare}>
              <LinkIcon size={19} /> Share…
            </button>
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

type ShareTarget = { kind: "report"; row: ReportListItem } | { kind: "group"; group: ReportGroup };

/** The public share-link sheet (docs/mocks/research-share, variant B): mint a revocable,
 * no-login link to a report or folder, copy it (shown once), and revoke live links. A report
 * drawn from private notes shows a warning before it's shared. */
function ShareSheet({
  target,
  onClose,
  onFlash,
}: { target: ShareTarget; onClose: () => void; onFlash: (msg: string) => void }) {
  const [existing, setExisting] = useState<ShareLinkView[] | null>(null);
  const [minted, setMinted] = useState<MintedShare | null>(null);
  const [busy, setBusy] = useState(false);
  // A known-library report requires a second tap to publish (warn + confirm); a folder can't
  // be pre-checked from the row, so it warns after minting (the link is revocable).
  const [armed, setArmed] = useState(false);

  const isReport = target.kind === "report";
  const targetId = isReport ? target.row.id : target.group.id;
  const title = isReport ? "Share this report" : `Share “${target.group.name}”`;
  const isLibrary =
    isReport &&
    (target.row.source_mode === "library" || target.row.source_mode === "library_first");

  const reload = useCallback(async () => {
    try {
      const all = await api.researchShares();
      setExisting(
        all.filter((s) => (isReport ? s.report_id === targetId : s.group_id === targetId)),
      );
    } catch (e) {
      onFlash(errMsg(e));
    }
  }, [isReport, targetId, onFlash]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function mint() {
    setBusy(true);
    try {
      const m = await api.mintReportShare(
        isReport
          ? { target_kind: "report", report_id: targetId }
          : { target_kind: "group", group_id: targetId },
      );
      setMinted(m);
      onFlash(
        (await copyText(researchShareUrl(m.token)))
          ? "Link created and copied"
          : "Link created — copy it below",
      );
      void reload();
    } catch (e) {
      onFlash(errMsg(e));
    } finally {
      setBusy(false);
      setArmed(false);
    }
  }

  async function revoke(id: string) {
    try {
      await api.revokeReportShare(id);
      setExisting((xs) => xs?.filter((x) => x.id !== id) ?? xs);
      if (minted && id === minted.id) setMinted(null);
      onFlash("Link revoked");
    } catch (e) {
      onFlash(errMsg(e));
    }
  }

  // The freshly minted link (URL shown once); older links can only be revoked (their secret
  // is unrecoverable, mirroring the jcode share).
  const others = (existing ?? []).filter((s) => s.id !== minted?.id);

  return (
    <Sheet title={title} onClose={onClose}>
      <div className="rl-share">
        <p className="rl-share-sub">
          {isReport
            ? "Anyone with the link can read this report. No sign-in, no expiry — revoke any time."
            : "Anyone with the link sees every report in this folder — including any you add to it later."}
        </p>

        {(isLibrary || minted?.library_warning) && (
          <div className="rl-share-warn">
            <GlobeIcon size={16} />
            <span>
              {isReport ? "This report was" : "This folder contains a report"} written from{" "}
              <b>your private notes</b>. Sharing publishes that content — revoke if you didn't mean
              to.
            </span>
          </div>
        )}

        {minted && (
          <div className="rl-share-linkwrap">
            <div className="rl-share-link">
              <span className="rl-share-url">{researchShareUrl(minted.token)}</span>
              <button
                type="button"
                className="rl-share-copy"
                onClick={async () =>
                  onFlash(
                    (await copyText(researchShareUrl(minted.token)))
                      ? "Link copied"
                      : "Couldn't copy — clipboard unavailable.",
                  )
                }
              >
                <ClipIcon size={14} /> Copy
              </button>
            </div>
            <p className="rl-share-once">Copy it now — the link isn't shown again.</p>
          </div>
        )}

        <button
          type="button"
          className={`rl-share-mint${armed ? " rl-share-mint-armed" : ""}`}
          disabled={busy}
          onClick={() => {
            if (isLibrary && !armed) {
              setArmed(true); // warn + confirm: a notes-derived report needs a second tap
              return;
            }
            void mint();
          }}
        >
          <LinkIcon size={17} />{" "}
          {busy
            ? "Creating…"
            : armed
              ? "Tap again to publish — from private notes"
              : "Create share link"}
        </button>

        {others.length > 0 && (
          <div className="rl-share-list">
            <div className="rl-share-list-h">Active links</div>
            {others.map((s) => (
              <div key={s.id} className="rl-share-row">
                <span className="rl-share-glyph" aria-hidden="true">
                  <LinkIcon size={16} />
                </span>
                <span className="rl-share-meta">
                  <span className="rl-share-when">{fmtDate(s.created_at)}</span>
                  <span className="rl-share-sub2">
                    {s.view_count} view{s.view_count === 1 ? "" : "s"}
                    {s.last_viewed_at
                      ? ` · last opened ${fmtDate(s.last_viewed_at)}`
                      : " · unopened"}
                  </span>
                </span>
                <button type="button" className="rl-share-revoke" onClick={() => void revoke(s.id)}>
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </Sheet>
  );
}
