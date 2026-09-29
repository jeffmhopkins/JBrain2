// ONE CHILD, ONE THREAD — the merge behind the jpanel conversation view.
//
// The owner: *"if I select lydian or Elora up there it should show those two as conversations
// ... kind of like a normal conversation does with jerv or the other ones in the pwa where
// there's an omnibox at the bottom and a left and right conversation bubble."*
//
// Until now a panel's day lived in two places that could not be read together: the messages
// she and her father sent each other, and — since 0.3.34 — what she had been saying to the pet
// on her wall. Two surfaces, each holding half of one afternoon. A child does not experience
// them as separate: she asks the pet why fish sleep, then records something for Dad about it.
// Split across two tabs, the second sentence has no first half.
//
// So this is a merge rather than a concatenation, and the ordering is the whole value: what
// makes the thread worth reading is that a message lands BETWEEN the question she asked the
// pet and the answer it gave her, exactly where it happened.
//
// Pure and separate from the screen because the ordering is the part that can be wrong in a
// way nobody notices — a bad sort still renders a plausible-looking conversation.

import type { JpanelMessage, JpanelPetChat, JpanelPetTurn, JpanelThread } from "../api/client";

/** A panel as the picker names it: one chip, one child. */
export interface ThreadPanel {
  device_id: string;
  name: string;
  /** Her messages he has not read. Drawn on the chip, because "which of them is waiting on
   *  me?" is the question the picker is looked at to answer. */
  unplayed: number;
}

/** One line in the conversation.
 *
 *  `asked` and `answered` are two items rather than one pet-exchange item so that a message
 *  arriving mid-turn can sort between them, which is the point of merging at all. */
export type ThreadItem =
  | { kind: "message"; key: string; at: number; message: JpanelMessage }
  | { kind: "asked"; key: string; at: number; turn: JpanelPetTurn }
  | { kind: "answered"; key: string; at: number; turn: JpanelPetTurn };

function ms(iso: string): number {
  const at = new Date(iso).getTime();
  // An unparseable timestamp sorts to the TOP rather than the bottom: an item with no time is
  // old news by definition, and floating it to the newest end would put it under the thumb at
  // the exact moment the owner opens the thread.
  return Number.isNaN(at) ? 0 : at;
}

/** Every panel the owner has either messaged or that has talked to its pet.
 *
 *  THE UNION, NOT THE MESSAGE THREADS. A panel a child has only ever spoken to the pet from
 *  has no message row at all, and listing only the message threads would leave her chip off
 *  the picker entirely — she would simply not exist on this screen. */
export function threadPanels(threads: JpanelThread[], chats: JpanelPetChat[]): ThreadPanel[] {
  const out: ThreadPanel[] = threads.map((t) => ({
    device_id: t.device_id,
    name: t.name,
    unplayed: t.unplayed,
  }));
  const seen = new Set(out.map((p) => p.device_id));
  for (const c of chats) {
    if (seen.has(c.device_id)) continue;
    seen.add(c.device_id);
    out.push({ device_id: c.device_id, name: c.label, unplayed: 0 });
  }
  return out;
}

/** One panel's messages and pet turns in one list, OLDEST FIRST.
 *
 *  Both routes serve newest first, which is right for an inbox and wrong for a conversation:
 *  a chat reads downwards and its newest line is the one above the composer.
 *
 *  WHEN SHE ASKED IS NOT WHEN THE ROW WAS WRITTEN. `pet_turn` is inserted once the reply has
 *  been made, so `created_at` is the moment the pet ANSWERED — and `total_ms` is how long the
 *  whole turn took. Her question therefore happened `total_ms` earlier, and placing it there
 *  rather than at the row's own timestamp is what lets a message that arrived while she was
 *  waiting fall in the right place. */
export function threadItems(
  thread: JpanelThread | undefined,
  chat: JpanelPetChat | undefined,
): ThreadItem[] {
  const items: ThreadItem[] = [];
  for (const m of thread?.messages ?? []) {
    items.push({ kind: "message", key: `m:${m.id}`, at: ms(m.created_at), message: m });
  }
  for (const t of chat?.turns ?? []) {
    const answered = ms(t.created_at);
    items.push({
      kind: "asked",
      key: `q:${t.id}`,
      // Clamped at zero elapsed: a turn whose timings are missing or nonsense must still put
      // her question before the answer to it, not after.
      at: answered - Math.max(0, t.total_ms),
      turn: t,
    });
    items.push({ kind: "answered", key: `a:${t.id}`, at: answered, turn: t });
  }
  // A stable sort keeps `asked` before `answered` when a turn reports no elapsed time at all,
  // since they were pushed in that order. Node and every browser this ships to sort stably.
  return items.sort((a, b) => a.at - b.at);
}

/** Whether the reader is at the live end of the conversation.
 *
 *  A polling chat that always scrolls to the bottom yanks the page out from under someone
 *  reading back through this morning, every twenty seconds. One who is already at the bottom
 *  is watching for the next line and wants to be taken to it. The slack is a thumb's worth of
 *  imprecision, not a real distance. */
export function atLiveEnd(el: { scrollTop: number; scrollHeight: number; clientHeight: number }) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}
