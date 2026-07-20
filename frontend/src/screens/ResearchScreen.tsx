// The Research Library (docs/reference/DESIGN.md "Research Library"; binding mock
// docs/mocks/research-library/b-segmented-tabs.html). The owner's browse door to the two
// external-corpus artifacts jerv produces — deep-research reports + analysed videos. A
// Reports/Videos segmented control switches purpose-built per-type lists; an as-you-type
// filter narrows the active tab; a per-row ⋯ opens the action sheet (view + delete here;
// R3 adds open-in-jerv / copy / download / open-source). Delete is owner-initiated with a
// deferred-commit undo: the row leaves the list immediately and the server DELETE fires
// only when the undo window closes. Amber research accent (read-only domain). Reachable
// from the launcher's Research tile; App hosts the detail layer (onOpen).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, type ReportListItem, type VideoListItem, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import {
  ChevronRightIcon,
  FileIcon,
  MoreIcon,
  SearchIcon,
  TrashIcon,
  VideoIcon,
} from "../components/icons";

export type ResearchKind = "report" | "video";
type Item = { kind: "report"; row: ReportListItem } | { kind: "video"; row: VideoListItem };

/** The undo window (ms) before a soft-deleted row's server DELETE actually commits. */
const UNDO_MS = 4500;

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
function itemTitle(item: Item): string {
  return item.kind === "report" ? item.row.question : item.row.title;
}

interface ResearchScreenProps {
  /** Open the full-screen detail layer (App owns it). */
  onOpen: (kind: ResearchKind, id: string) => void;
  /** The undo window before a delete commits; injectable so tests don't wait 4.5s. */
  undoMs?: number;
}

export function ResearchScreen({ onOpen, undoMs = UNDO_MS }: ResearchScreenProps) {
  const [tab, setTab] = useState<ResearchKind>("report");
  const [reports, setReports] = useState<ReportListItem[] | null>(null);
  const [videos, setVideos] = useState<VideoListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [menuFor, setMenuFor] = useState<Item | null>(null);
  const [toast, setToast] = useState<{ msg: string; undo?: () => void } | null>(null);

  // Deferred-commit deletes: id → the pending row + its server commit timer. On undo we
  // cancel the timer and restore the row; on unmount we flush (commit) so nothing silently
  // resurrects. The map is a ref so it survives re-renders without re-arming effects.
  const pending = useRef<Map<string, { item: Item; timer: ReturnType<typeof setTimeout> }>>(
    new Map(),
  );

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
    }, undoMs);
    pending.current.set(id, { item, timer });
    setToast({
      msg: `Deleted “${clip(itemTitle(item))}”.`,
      undo: () => {
        const p = pending.current.get(id);
        if (p) clearTimeout(p.timer);
        pending.current.delete(id);
        restoreLocally(item, index < 0 ? 0 : index);
        setToast(null);
      },
    });
  }

  const activeList = tab === "report" ? reports : videos;
  const filtered = useMemo<(ReportListItem | VideoListItem)[] | null>(() => {
    if (!activeList) return null;
    const q = query.trim().toLowerCase();
    if (!q) return activeList;
    return activeList.filter((it) =>
      tab === "report"
        ? (it as ReportListItem).question.toLowerCase().includes(q)
        : `${(it as VideoListItem).title} ${(it as VideoListItem).channel_name}`
            .toLowerCase()
            .includes(q),
    );
  }, [activeList, query, tab]);

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
      ) : (
        <div className="rl-list">
          {filtered?.map((row) =>
            tab === "report" ? (
              <ReportRow
                key={(row as ReportListItem).id}
                row={row as ReportListItem}
                onOpen={() => onOpen("report", (row as ReportListItem).id)}
                onMenu={() => setMenuFor({ kind: "report", row: row as ReportListItem })}
              />
            ) : (
              <VideoRow
                key={(row as VideoListItem).video_id}
                row={row as VideoListItem}
                onOpen={() => onOpen("video", (row as VideoListItem).video_id)}
                onMenu={() => setMenuFor({ kind: "video", row: row as VideoListItem })}
              />
            ),
          )}
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
          onDelete={() => deleteItem(menuFor)}
        />
      )}

      {toast !== null && (
        <output className="rl-toast">
          <TrashIcon size={15} />
          <span className="rl-toast-msg">{toast.msg}</span>
          {toast.undo && (
            <button type="button" className="rl-toast-undo" onClick={toast.undo}>
              Undo
            </button>
          )}
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
          <span className="rl-title">{row.question}</span>
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
        <span className="rl-thumb" aria-hidden="true">
          <VideoIcon size={22} />
          {row.duration_s !== null && (
            <span className="rl-thumb-dur">{fmtDuration(row.duration_s)}</span>
          )}
        </span>
        <span className="rl-main">
          <span className="rl-title rl-title-video">{row.title}</span>
          <span className="rl-channel">{row.channel_name}</span>
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

/** The per-item ⋯ action sheet. Wave R2 wires View + Delete (with a tap-again confirm);
 * Wave R3 adds Open-in-jerv, Copy, Download, and Open-source. */
function ActionSheet({
  item,
  onClose,
  onView,
  onDelete,
}: {
  item: Item;
  onClose: () => void;
  onView: () => void;
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
