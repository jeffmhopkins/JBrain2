// JShare — the owner's public share links. Any file goes up (a picture, a recording, a
// video, an HTML page, a zip holding a self-contained site) and the box mints it its own
// public link. The list is what is live right now: deleting an item unpublishes it, so the
// confirm says the link stops working rather than only that a file goes.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type ShareItem, type ShareKind, api } from "../api/client";
import { Dialog } from "../components/Dialog";
import { Sheet } from "../components/Sheet";
import {
  CodeIcon,
  CopyIcon,
  ExternalLinkIcon,
  FileIcon,
  GlobeIcon,
  ImageIcon,
  LinkIcon,
  MicIcon,
  MoreIcon,
  UploadIcon,
  VideoIcon,
  XIcon,
} from "../components/icons";
import "./jshare.css";

const TITLE_MAX = 200;
const COPIED_MS = 2000;

export interface JShareDeps {
  list: () => Promise<ShareItem[]>;
  upload: (
    file: File,
    title: string,
    onProgress: (sent: number, total: number) => void,
    signal?: AbortSignal,
  ) => Promise<ShareItem>;
  rename: (id: string, title: string) => Promise<ShareItem>;
  remove: (id: string) => Promise<void>;
}

interface Props {
  /** Injectable for tests; defaults to the live API client. */
  deps?: JShareDeps;
}

const LIVE: JShareDeps = {
  list: api.jshareItems,
  upload: api.jshareUpload,
  rename: api.jshareRename,
  remove: api.jshareDelete,
};

const KIND: Record<ShareKind, { label: string; icon: ReactNode }> = {
  image: { label: "Picture", icon: <ImageIcon size={20} /> },
  audio: { label: "Audio", icon: <MicIcon size={20} /> },
  video: { label: "Video", icon: <VideoIcon size={20} /> },
  html: { label: "Web page", icon: <CodeIcon size={20} /> },
  site: { label: "Website", icon: <GlobeIcon size={20} /> },
  file: { label: "File", icon: <FileIcon size={20} /> },
};

export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb < 100 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

function fmtViews(n: number): string {
  if (n === 0) return "not opened yet";
  return `${n} view${n === 1 ? "" : "s"}`;
}

type Upload = { name: string; sent: number; total: number; ctl: AbortController };

export function JShareScreen({ deps = LIVE }: Props) {
  const [items, setItems] = useState<ShareItem[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [picked, setPicked] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let stale = false;
    deps
      .list()
      .then((list) => {
        if (!stale) setItems(list);
      })
      .catch(() => {
        if (!stale) setLoadFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [deps]);

  // An upload in flight is abandoned with the screen; the box discards a partial body.
  const uploadRef = useRef<Upload | null>(null);
  uploadRef.current = upload;
  useEffect(
    () => () => {
      uploadRef.current?.ctl.abort();
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
    },
    [],
  );

  async function startUpload(file: File, title: string): Promise<void> {
    const ctl = new AbortController();
    setPicked(null);
    setUploadError(null);
    setUpload({ name: file.name, sent: 0, total: file.size, ctl });
    try {
      const made = await deps.upload(
        file,
        title,
        (sent, total) => setUpload((u) => (u && u.ctl === ctl ? { ...u, sent, total } : u)),
        ctl.signal,
      );
      setItems((list) => [made, ...(list ?? [])]);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setUploadError(err instanceof Error ? err.message : "The upload failed — try again.");
      }
    } finally {
      setUpload((u) => (u && u.ctl === ctl ? null : u));
    }
  }

  const copy = useCallback(async (item: ShareItem) => {
    try {
      await navigator.clipboard.writeText(item.url);
      setCopiedId(item.id);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopiedId(null), COPIED_MS);
    } catch {
      setProblem("Couldn't reach the clipboard — open the link and copy it from there.");
    }
  }, []);

  async function send(item: ShareItem): Promise<void> {
    try {
      await navigator.share({ title: item.title, url: item.url });
    } catch {
      // Dismissing the share sheet is not an error worth saying.
    }
  }

  async function rename(item: ShareItem, title: string): Promise<void> {
    try {
      const updated = await deps.rename(item.id, title);
      setItems((list) => (list ?? []).map((x) => (x.id === updated.id ? updated : x)));
      setDetailId(null);
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Couldn't rename it — try again.");
    }
  }

  async function remove(item: ShareItem): Promise<void> {
    setConfirmId(null);
    try {
      await deps.remove(item.id);
      setItems((list) => (list ?? []).filter((x) => x.id !== item.id));
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Couldn't delete it — try again.");
    }
  }

  const canSend = typeof navigator.share === "function";
  const detail = items?.find((x) => x.id === detailId) ?? null;
  const confirming = items?.find((x) => x.id === confirmId) ?? null;
  const pct = upload?.total ? Math.round((upload.sent / upload.total) * 100) : 0;

  return (
    <main className="screen-body js-screen">
      <input
        ref={fileRef}
        type="file"
        hidden
        aria-label="Choose a file to share"
        onChange={(e) => {
          const file = e.currentTarget.files?.[0] ?? null;
          // Cleared so picking the same file again still fires a change.
          e.currentTarget.value = "";
          if (file) setPicked(file);
        }}
      />
      <button
        type="button"
        className="js-add"
        disabled={upload !== null}
        onClick={() => fileRef.current?.click()}
      >
        <UploadIcon size={20} />
        <span className="js-add-txt">
          <span className="js-add-h">Share a file</span>
          <span className="js-add-s">
            Pictures, audio, video, web pages, zipped sites, anything
          </span>
        </span>
      </button>

      {upload && (
        // biome-ignore lint/a11y/useSemanticElements: a card of blocks; <output> only takes phrasing content.
        <div className="js-upload" role="status">
          <div className="js-upload-h">
            Uploading <b>{upload.name}</b>
          </div>
          {/* biome-ignore lint/a11y/useFocusableInteractive: a progressbar announces progress; it isn't operated. */}
          <div
            className="js-bar"
            role="progressbar"
            aria-label="Upload"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
          >
            <i style={{ width: `${pct}%` }} />
          </div>
          <div className="js-upload-row">
            <span className="js-meta">
              {fmtSize(upload.sent)} of {fmtSize(upload.total)} · keep this screen open
            </span>
            <button type="button" className="js-btn" onClick={() => upload.ctl.abort()}>
              <XIcon size={16} />
              Cancel
            </button>
          </div>
        </div>
      )}

      {uploadError && (
        <div className="js-alert" role="alert">
          <span>Not shared — {uploadError}</span>
          <button
            type="button"
            className="js-icon"
            aria-label="Dismiss"
            onClick={() => setUploadError(null)}
          >
            <XIcon size={18} />
          </button>
        </div>
      )}
      {problem && (
        <div className="js-alert" role="alert">
          <span>{problem}</span>
          <button
            type="button"
            className="js-icon"
            aria-label="Dismiss"
            onClick={() => setProblem(null)}
          >
            <XIcon size={18} />
          </button>
        </div>
      )}

      {loadFailed && <p className="analysis-quiet">couldn't load your shares — reopen to retry.</p>}
      {items === null && !loadFailed && <p className="analysis-quiet">loading your shares…</p>}
      {items !== null && items.length === 0 && (
        <p className="analysis-quiet">nothing shared yet — add a file to get its link.</p>
      )}

      {items !== null && items.length > 0 && (
        <ul className="js-list" aria-label="Shared files">
          {items.map((item) => (
            <li key={item.id} className="js-item">
              <div className="js-row">
                <Thumb item={item} />
                <div className="js-text">
                  <span className="js-title">{item.title}</span>
                  <span className="js-meta">
                    {KIND[item.kind]?.label ?? "File"} · {fmtSize(item.size_bytes)} ·{" "}
                    {fmtDate(item.created_at)} · {fmtViews(item.view_count)}
                  </span>
                </div>
                <button
                  type="button"
                  className="js-icon"
                  aria-label={`More for ${item.title}`}
                  onClick={() => setDetailId(item.id)}
                >
                  <MoreIcon size={20} />
                </button>
              </div>
              <div className="js-actions">
                <button
                  type="button"
                  className="js-btn js-btn-primary"
                  onClick={() => void copy(item)}
                >
                  <CopyIcon size={16} />
                  {copiedId === item.id ? "Copied" : "Copy link"}
                </button>
                {canSend && (
                  <button type="button" className="js-btn" onClick={() => void send(item)}>
                    <LinkIcon size={16} />
                    Send…
                  </button>
                )}
                <a className="js-btn" href={item.url} target="_blank" rel="noopener noreferrer">
                  <ExternalLinkIcon size={16} />
                  Open
                </a>
              </div>
            </li>
          ))}
        </ul>
      )}

      {picked && (
        <UploadSheet
          file={picked}
          onClose={() => setPicked(null)}
          onUpload={(title) => void startUpload(picked, title)}
        />
      )}

      {detail && (
        <DetailSheet
          item={detail}
          onClose={() => setDetailId(null)}
          onRename={(title) => void rename(detail, title)}
          onDelete={() => {
            setDetailId(null);
            setConfirmId(detail.id);
          }}
        />
      )}

      {confirming && (
        <Dialog
          title="Delete this share?"
          confirmLabel="Delete & unpublish"
          onCancel={() => setConfirmId(null)}
          onConfirm={() => void remove(confirming)}
        >
          “{confirming.title}” is deleted from the box and its link stops working at once — anyone
          you sent it to gets nothing.
        </Dialog>
      )}
    </main>
  );
}

function Thumb({ item }: { item: ShareItem }) {
  const [broken, setBroken] = useState(false);
  if (item.kind === "image" && !broken) {
    return (
      <img
        className="js-thumb"
        src={item.url}
        alt=""
        loading="lazy"
        onError={() => setBroken(true)}
      />
    );
  }
  return (
    <span className="js-thumb js-thumb-ic" aria-hidden="true">
      {KIND[item.kind]?.icon ?? <FileIcon size={20} />}
    </span>
  );
}

function UploadSheet({
  file,
  onClose,
  onUpload,
}: {
  file: File;
  onClose: () => void;
  onUpload: (title: string) => void;
}) {
  const [title, setTitle] = useState("");
  return (
    <Sheet title="Share a file" onClose={onClose}>
      <p className="js-file">
        <b>{file.name}</b> · {fmtSize(file.size)}
      </p>
      <label className="sheet-field">
        Title (optional)
        <input
          value={title}
          maxLength={TITLE_MAX}
          placeholder={file.name}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onUpload(title);
          }}
        />
      </label>
      <p className="js-note">Anyone with the link can open it, until you delete it here.</p>
      <button type="button" className="sheet-primary" onClick={() => onUpload(title)}>
        Upload &amp; make link
      </button>
    </Sheet>
  );
}

function DetailSheet({
  item,
  onClose,
  onRename,
  onDelete,
}: {
  item: ShareItem;
  onClose: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const [title, setTitle] = useState(item.title);
  const trimmed = title.trim();
  const save = () => {
    if (trimmed && trimmed !== item.title) onRename(trimmed);
  };
  return (
    <Sheet title={item.title} onClose={onClose}>
      <dl className="js-kv">
        <dt>File</dt>
        <dd>
          {item.filename} · {item.media_type}
        </dd>
        <dt>Link</dt>
        <dd className="js-url">{item.url}</dd>
        <dt>Last opened</dt>
        <dd>{item.last_viewed_at ? fmtDate(item.last_viewed_at) : "never"}</dd>
      </dl>
      <label className="sheet-field">
        Title
        <input
          value={title}
          maxLength={TITLE_MAX}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
          }}
        />
      </label>
      <button
        type="button"
        className="sheet-primary"
        disabled={!trimmed || trimmed === item.title}
        onClick={save}
      >
        Save title
      </button>
      <button type="button" className="js-delete btn-destructive" onClick={onDelete}>
        Delete &amp; unpublish
      </button>
    </Sheet>
  );
}
