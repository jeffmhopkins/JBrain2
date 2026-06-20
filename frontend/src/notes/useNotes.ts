// Stream state: server notes merged with the local outbox. Sends append
// locally first (instant pending row), then a flush + reload reconciles
// with the server by client_id.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type AttachmentOut, type NoteOut, type NoteUpdate, api } from "../api/client";
import { freshCoords } from "../location";
import { useForeground } from "../visibility";
import { lifecycleChip } from "./lifecycle";
import { type OutboxStore, type PendingNote, createIdbStore, flushOutbox } from "./outbox";

export interface StreamAttachment {
  id: string | null; // null while the blob is still queued locally
  filename: string;
  mediaType: string;
  sizeBytes: number;
  /** Images: true once OCR/caption text is cached server-side. */
  hasExtracts: boolean;
  /** Images: true once a non-empty description is cached (full analysis). */
  hasDescription: boolean;
}

export interface StreamItem {
  key: string;
  /** Server note id; null while the note only exists in the outbox. */
  id: string | null;
  domain: string;
  destination: string | null;
  body: string;
  createdAt: Date;
  /** pending/processing → indexing chip, failed → failure chip; null = outbox row. */
  ingestState: string | null;
  /** True once the analysis pipeline finished — the lifecycle chip's end. */
  analyzed: boolean;
  /** "human" or "agent" — the stream tags agent-authored notes; outbox rows
   * are always "human" (the owner just wrote them). */
  provenance: string;
  attachments: StreamAttachment[];
  pending: boolean;
  /** Hidden from the stream server-side; outbox rows are never hidden. */
  hidden: boolean;
}

export type SyncStatus = "synced" | "pending" | "unreachable";

export interface SendInput {
  domain: string;
  destination: string | null;
  body: string;
  files: File[];
}

export interface NotesController {
  items: StreamItem[];
  syncStatus: SyncStatus;
  /** Pull the server list now — wired to events that create notes out of band
   * (e.g. enacting a Proposal) so the stream reflects them without waiting for
   * the poll tick. */
  refresh(): Promise<void>;
  send(input: SendInput): Promise<void>;
  update(id: string, patch: NoteUpdate): Promise<void>;
  remove(id: string): Promise<void>;
  /** Hide (true) or unhide (false) a note from the stream; stays in Search. */
  setHidden(id: string, hidden: boolean): Promise<void>;
  byId(id: string): StreamItem | undefined;
  /** Cache-first note lookup; falls back to paging the list (see fetchNoteById). */
  fetchById(id: string): Promise<StreamItem | null>;
  /** Uploads to an existing note and refreshes; returns the new attachment. */
  addAttachment(noteId: string, file: File): Promise<StreamAttachment>;
  /** Removes an attachment and refreshes the stream. */
  removeAttachment(attachmentId: string): Promise<void>;
}

// The resting poll cadence. While any note is still moving through the pipeline
// (indexing → ocr → analyzing) we poll far faster so the lifecycle chip and a
// freshly-enacted note settle live instead of crawling forward 30s at a time.
const IDLE_INTERVAL_MS = 30_000;
const ACTIVE_INTERVAL_MS = 2_500;
const PAGE_SIZE = 100;

/** A note still working through the pipeline — a "pending"-tone lifecycle chip.
 * "failed" is terminal, so it doesn't keep the fast poll alive. Mirrors the chip
 * the row actually shows so what we poll on is what the owner sees moving. */
function inFlight(item: StreamItem): boolean {
  const chip = lifecycleChip(item);
  return chip !== null && chip.tone === "pending";
}
// There is no GET /api/notes/{id}; a cache miss (e.g. a search hit older than
// the stream window) pages the list a bounded number of times before giving
// up and leaving the caller with the search-result preview.

function serverItem(note: NoteOut): StreamItem {
  return {
    key: note.client_id,
    id: note.id,
    domain: note.domain,
    destination: note.destination,
    body: note.body,
    createdAt: new Date(note.created_at),
    ingestState: note.ingest_state,
    analyzed: note.analyzed,
    provenance: note.provenance,
    attachments: note.attachments.map((a: AttachmentOut) => ({
      id: a.id,
      filename: a.filename,
      mediaType: a.media_type,
      sizeBytes: a.size_bytes,
      hasExtracts: a.has_extracts,
      hasDescription: a.has_description,
    })),
    pending: false,
    hidden: note.hidden,
  };
}

function pendingItem(note: PendingNote): StreamItem {
  return {
    key: note.client_id,
    id: null,
    domain: note.domain,
    destination: note.destination,
    body: note.body,
    createdAt: new Date(note.created_at),
    ingestState: null,
    analyzed: false, // analysis can only have run server-side
    provenance: "human", // an outbox row is the owner's own capture
    attachments: note.attachments.map((a) => ({
      id: null,
      filename: a.filename,
      mediaType: a.media_type,
      sizeBytes: a.blob.size,
      hasExtracts: false, // OCR can only have run server-side
      hasDescription: false,
    })),
    pending: true,
    hidden: false,
  };
}

export async function fetchNoteById(id: string): Promise<StreamItem | null> {
  try {
    return serverItem(await api.getNote(id));
  } catch {
    return null;
  }
}

export function useNotes(enabled: boolean, store?: OutboxStore): NotesController {
  const storeRef = useRef<OutboxStore | null>(store ?? null);
  if (storeRef.current === null) storeRef.current = createIdbStore();
  const outbox = storeRef.current;

  const [serverItems, setServerItems] = useState<StreamItem[]>([]);
  const [pending, setPending] = useState<StreamItem[]>([]);
  const [reachable, setReachable] = useState(true);

  const sync = useCallback(async () => {
    try {
      await flushOutbox(outbox);
    } catch {
      // Flush failures surface through the reachability probe below;
      // a 401 has already flipped the app to the login screen.
    }
    try {
      const page = await api.listNotes(PAGE_SIZE);
      // Newest-first from the API; the stream renders oldest-first.
      setServerItems(page.notes.map(serverItem).reverse());
      setReachable(true);
    } catch {
      setReachable(false);
    }
    setPending((await outbox.all()).map(pendingItem));
  }, [outbox]);

  // Any server note still mid-pipeline drives the faster cadence; flipping it
  // re-arms the interval below (and runs an immediate sync, so an enacted or
  // freshly-settled note reflects at once rather than on the next slow tick).
  const anyInFlight = useMemo(() => serverItems.some(inFlight), [serverItems]);

  // A backgrounded PWA must not poll the server; suspending while hidden and
  // catching up the instant it returns to the foreground is the whole point.
  const foreground = useForeground();

  useEffect(() => {
    if (!enabled || !foreground) return;
    void sync();
    const onOnline = () => void sync();
    window.addEventListener("online", onOnline);
    const period = anyInFlight ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;
    const interval = setInterval(() => void sync(), period);
    return () => {
      window.removeEventListener("online", onOnline);
      clearInterval(interval);
    };
  }, [enabled, sync, anyInFlight, foreground]);

  const send = useCallback(
    async (input: SendInput) => {
      // Location is recorded at write time (never awaited): an offline note
      // flushed hours later still carries where it was actually written.
      const coords = freshCoords();
      const note: PendingNote = {
        client_id: crypto.randomUUID(),
        domain: input.domain,
        destination: input.destination,
        body: input.body,
        created_at: new Date().toISOString(),
        // East of UTC: getTimezoneOffset is minutes WEST, so negate it. Lets
        // the server recover the local capture date for extraction.
        tz_offset_minutes: -new Date().getTimezoneOffset(),
        attachments: input.files.map((f) => ({
          filename: f.name,
          media_type: f.type || "application/octet-stream",
          blob: f,
        })),
        ...(coords ?? {}),
      };
      await outbox.put(note);
      // Instant local append; the background sync reconciles afterwards.
      setPending((prev) => [...prev, pendingItem(note)]);
      void sync();
    },
    [outbox, sync],
  );

  const update = useCallback(
    async (id: string, patch: NoteUpdate) => {
      await api.updateNote(id, patch);
      await sync();
    },
    [sync],
  );

  const remove = useCallback(
    async (id: string) => {
      await api.deleteNote(id);
      await sync();
    },
    [sync],
  );

  const setHidden = useCallback(
    async (id: string, hidden: boolean) => {
      // Optimistic: drop (or restore via reload) the row so the stream reacts
      // before the round-trip; sync reconciles with the server afterwards.
      if (hidden) setServerItems((prev) => prev.filter((i) => i.id !== id));
      await (hidden ? api.hideNote(id) : api.unhideNote(id));
      await sync();
    },
    [sync],
  );

  const items = useMemo(() => {
    const serverKeys = new Set(serverItems.map((i) => i.key));
    const merged = [...serverItems, ...pending.filter((p) => !serverKeys.has(p.key))];
    merged.sort((a, b) => a.createdAt.getTime() - b.createdAt.getTime());
    return merged;
  }, [serverItems, pending]);

  const byId = useCallback((id: string) => items.find((i) => i.id === id), [items]);

  const fetchById = useCallback(
    async (id: string): Promise<StreamItem | null> => byId(id) ?? (await fetchNoteById(id)),
    [byId],
  );

  const addAttachment = useCallback(
    async (noteId: string, file: File): Promise<StreamAttachment> => {
      const out = await api.uploadAttachment(noteId, file, file.name);
      await sync();
      return {
        id: out.id,
        filename: out.filename,
        mediaType: out.media_type,
        sizeBytes: out.size_bytes,
        hasExtracts: out.has_extracts,
        hasDescription: out.has_description,
      };
    },
    [sync],
  );

  const removeAttachment = useCallback(
    async (attachmentId: string): Promise<void> => {
      await api.deleteAttachment(attachmentId);
      await sync();
    },
    [sync],
  );

  const syncStatus: SyncStatus = !reachable
    ? "unreachable"
    : pending.length > 0
      ? "pending"
      : "synced";

  return {
    items,
    syncStatus,
    refresh: sync,
    send,
    update,
    remove,
    setHidden,
    byId,
    fetchById,
    addAttachment,
    removeAttachment,
  };
}
