// Stream state: server notes merged with the local outbox. Sends append
// locally first (instant pending row), then a flush + reload reconciles
// with the server by client_id.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type AttachmentOut, api } from "../api/client";
import { type OutboxStore, type PendingNote, createIdbStore, flushOutbox } from "./outbox";

export interface StreamAttachment {
  id: string | null; // null while the blob is still queued locally
  filename: string;
}

export interface StreamItem {
  key: string;
  domain: string;
  destination: string | null;
  body: string;
  createdAt: Date;
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
}

const FLUSH_INTERVAL_MS = 30_000;
const PAGE_SIZE = 100;

function serverItem(note: {
  client_id: string;
  domain: string;
  destination: string | null;
  body: string;
  created_at: string;
  attachments: AttachmentOut[];
}): StreamItem {
  return {
    key: note.client_id,
    domain: note.domain,
    destination: note.destination,
    body: note.body,
    createdAt: new Date(note.created_at),
    attachments: note.attachments.map((a) => ({ id: a.id, filename: a.filename })),
    pending: false,
  };
}

function pendingItem(note: PendingNote): StreamItem {
  return {
    key: note.client_id,
    domain: note.domain,
    destination: note.destination,
    body: note.body,
    createdAt: new Date(note.created_at),
    attachments: note.attachments.map((a) => ({ id: null, filename: a.filename })),
    pending: true,
  };
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
      };
      await outbox.put(note);
      // Instant local append; the background sync reconciles afterwards.
      setPending((prev) => [...prev, pendingItem(note)]);
      void sync();
    },
    [outbox, sync],
  );

  const items = useMemo(() => {
    const serverKeys = new Set(serverItems.map((i) => i.key));
    const merged = [...serverItems, ...pending.filter((p) => !serverKeys.has(p.key))];
    merged.sort((a, b) => a.createdAt.getTime() - b.createdAt.getTime());
    return merged;
  }, [serverItems, pending]);

  const syncStatus: SyncStatus = !reachable ? "unreachable" : pending.length > 0 ? "pending" : "synced";

  return { items, syncStatus, send };
}
