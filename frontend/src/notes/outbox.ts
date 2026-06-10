// Offline outbox: notes are written here first (with any attachment Blobs),
// appear in the stream as "pending sync", and are flushed to the server on
// start / online / send / a 30s interval. POST /api/notes is idempotent on
// client_id, so a flush interrupted after the POST simply re-sends and gets
// the same note back.

import { ApiError, api } from "../api/client";

export interface PendingAttachment {
  filename: string;
  media_type: string;
  blob: Blob;
}

export interface PendingNote {
  client_id: string;
  domain: string;
  destination: string | null;
  body: string;
  created_at: string;
  // Capture-time UTC offset in minutes east of UTC (the negation of JS
  // getTimezoneOffset). Sent so the server can recover the note's local date
  // for extraction; absent on pre-Phase-3 rows.
  tz_offset_minutes?: number;
  attachments: PendingAttachment[];
  // Captured at write time so an offline note keeps its true location even
  // when the flush happens somewhere else. Absent on pre-Phase-2 rows.
  latitude?: number;
  longitude?: number;
  accuracy_m?: number;
}

export interface OutboxStore {
  all(): Promise<PendingNote[]>;
  put(note: PendingNote): Promise<void>;
  remove(clientId: string): Promise<void>;
}

export function createMemoryStore(): OutboxStore {
  const items = new Map<string, PendingNote>();
  return {
    all: async () => [...items.values()].sort((a, b) => a.created_at.localeCompare(b.created_at)),
    put: async (note) => {
      items.set(note.client_id, note);
    },
    remove: async (clientId) => {
      items.delete(clientId);
    },
  };
}

const DB_NAME = "jbrain";
const STORE = "outbox";

function promisify<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function openDb(): Promise<IDBDatabase> {
  const req = indexedDB.open(DB_NAME, 1);
  req.onupgradeneeded = () => {
    req.result.createObjectStore(STORE, { keyPath: "client_id" });
  };
  return promisify(req);
}

export function createIdbStore(): OutboxStore {
  // jsdom and very old browsers lack IndexedDB; notes then queue in memory
  // for the session instead of crashing capture entirely.
  if (typeof indexedDB === "undefined") return createMemoryStore();

  const db = openDb();
  async function tx(mode: IDBTransactionMode): Promise<IDBObjectStore> {
    return (await db).transaction(STORE, mode).objectStore(STORE);
  }
  return {
    all: async () => {
      const rows = await promisify((await tx("readonly")).getAll());
      return (rows as PendingNote[]).sort((a, b) => a.created_at.localeCompare(b.created_at));
    },
    put: async (note) => {
      await promisify((await tx("readwrite")).put(note));
    },
    remove: async (clientId) => {
      await promisify((await tx("readwrite")).delete(clientId));
    },
  };
}

export interface FlushResult {
  sent: number;
  remaining: number;
}

let flushing: Promise<FlushResult> | null = null;

/** Serialized: concurrent callers share the in-flight flush. */
export function flushOutbox(store: OutboxStore): Promise<FlushResult> {
  if (!flushing) {
    flushing = doFlush(store).finally(() => {
      flushing = null;
    });
  }
  return flushing;
}

async function doFlush(store: OutboxStore): Promise<FlushResult> {
  const pending = await store.all();
  let sent = 0;
  let remaining = pending.length;

  for (const note of pending) {
    try {
      const created = await api.createNote({
        client_id: note.client_id,
        domain: note.domain,
        destination: note.destination,
        body: note.body,
        created_at: note.created_at,
        ...(note.tz_offset_minutes !== undefined
          ? { tz_offset_minutes: note.tz_offset_minutes }
          : {}),
        ...(note.latitude !== undefined &&
        note.longitude !== undefined &&
        note.accuracy_m !== undefined
          ? { latitude: note.latitude, longitude: note.longitude, accuracy_m: note.accuracy_m }
          : {}),
      });
      while (note.attachments.length > 0) {
        const att = note.attachments[0];
        if (!att) break;
        await api.uploadAttachment(created.id, att.blob, att.filename);
        // Persist progress per attachment so a retry never re-uploads.
        note.attachments = note.attachments.slice(1);
        await store.put(note);
      }
      await store.remove(note.client_id);
      sent += 1;
      remaining -= 1;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        // Permanent rejection (unknown domain, oversized file): drop the
        // item rather than wedge the whole queue behind it.
        await store.remove(note.client_id);
        remaining -= 1;
        continue;
      }
      // Network or server fault — stop and let a later flush retry in order.
      break;
    }
  }
  return { sent, remaining };
}
