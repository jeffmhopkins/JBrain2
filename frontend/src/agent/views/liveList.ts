// A tiny shared store so every `list_card` in the transcript shows LIVE list
// state, not the snapshot baked into its tool result. Cards key on `list_id`:
// the first to render seeds the store from its payload and fetches the current
// list; any later card (or a toggle on any card) updates the one store, so two
// cards of the same list never drift apart. Module-level on purpose — it's a
// per-session cache shared across every mounted ListCard.

import { type ListItemOut, api } from "../../api/client";

export interface LiveList {
  title: string;
  domain: string;
  items: ListItemOut[];
}

type Listener = () => void;

const store = new Map<string, LiveList>();
const listeners = new Set<Listener>();
const inflight = new Set<string>();

function emit(): void {
  for (const l of listeners) l();
}

export function subscribeLiveLists(l: Listener): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function getLiveList(id: string): LiveList | undefined {
  return store.get(id);
}

/** Seed the store from a card's payload if nothing's there yet — so the card
 * shows something instantly, before the live fetch lands. No emit: a pure seed
 * never needs to wake other cards. */
export function seedLiveList(id: string, data: LiveList): void {
  if (!store.has(id)) store.set(id, data);
}

/** Fetch the current list and publish it to every card. Deduped per id. */
export async function loadLiveList(id: string): Promise<void> {
  if (!id || inflight.has(id)) return;
  inflight.add(id);
  try {
    const l = await api.getList(id);
    store.set(id, { title: l.title, domain: l.domain, items: l.items });
    emit();
  } catch {
    // Keep whatever we have (a deleted/out-of-scope list 404s — leave the snapshot).
  } finally {
    inflight.delete(id);
  }
}

/** Toggle an item optimistically across every card, then persist; revert on
 * failure. */
export function toggleLiveItem(listId: string, itemId: string, checked: boolean): void {
  const apply = (value: boolean): void => {
    const cur = store.get(listId);
    if (!cur) return;
    store.set(listId, {
      ...cur,
      items: cur.items.map((i) => (i.id === itemId ? { ...i, checked: value } : i)),
    });
    emit();
  };
  apply(checked);
  api.setListItemChecked(itemId, checked).catch(() => apply(!checked));
}

/** Tests only: drop the shared cache so cases don't leak into each other. */
export function resetLiveLists(): void {
  store.clear();
  listeners.clear();
  inflight.clear();
}
