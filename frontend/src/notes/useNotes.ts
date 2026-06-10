// Stream state: server notes merged with the local outbox. Sends append
// locally first (instant pending row), then a flush + reload reconciles
// with the server by client_id.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type AttachmentOut, type NoteOut, type NoteUpdate, api } from "../api/client";
import { freshCoords } from "../location";
import { type OutboxStore, type PendingNote, createIdbStore, flushOutbox } from "./outbox";

export interface StreamAttachment {
  id: string | null; // null while the blob is still queued locally
  filename: string;
  mediaType: string;
  sizeBytes: number;
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
  attachments: StreamAttachment[];
  pending: boolean;
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
  send(input: SendInput): Promise<void>;
  update(id: string, patch: NoteUpdate): Promise<void>;
  remove(id: string): Promise<void>;
  byId(id: string): StreamItem | undefined;
  /** Cache-first note lookup; falls back to paging the list (see fetchNoteById). */
  fetchById(id: string): Promise<StreamItem | null>;
  /** Uploads to an existing note and refreshes; returns the new attachment. */
  addAttachment(noteId: string, file: File): Promise<StreamAttachment>;
  /** Removes an attachment and refreshes the stream. */
  removeAttachment(attachmentId: string): Promise<void>;
}

const FLUSH_INTERVAL_MS = 30_000;
const PAGE_SIZE = 100;
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
    attachments: note.attachments.map((a: AttachmentOut) => ({
      id: a.id,
      filename: a.filename,
      mediaType: a.media_type,
      sizeBytes: a.size_bytes,
    })),
    pending: false,
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
    attachments: note.attachments.map((a) => ({
      id: null,
      filename: a.filename,
      mediaType: a.media_type,
      sizeBytes: a.blob.size,
    })),
    pending: true,
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

  useEffect(() => {
    if (!enabled) return;
    void sync();
    const onOnline = () => void sync();
    window.addEventListener("online", onOnline);
    const interval = setInterval(() => void sync(), FLUSH_INTERVAL_MS);
    return () => {
      window.removeEventListener("online", onOnline);
      clearInterval(interval);
    };
  }, [enabled, sync]);

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
    send,
    update,
    remove,
    byId,
    fetchById,
    addAttachment,
    removeAttachment,
  };
}
