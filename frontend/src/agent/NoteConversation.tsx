// Entry's main view once a note is selected: that note's conversation, loaded the way
// picking a chat out of jerv's Sessions panel loads one (the owner's ruling of
// 2026-09-14 — *"when you select a note, it basically loads a conversation the same as if
// I had swiped left inside of jerv and picked a different conversation"*).
//
// It is the SHIPPED transcript: `AgentTranscript` is the same component `FullBrainSurface`
// mounts, so the violet Thought chip, the steel Worked chip, the live phase line, the step
// rows and the question block are one definition with two hosts. And it brings NO composer
// — the omnibox below is the composer, here as in every other mode, which is the whole of
// what the owner reversed about the shipped note screen.
//
// What it deliberately lacks next to `FullBrainSurface`: the **Sessions** panel. That panel
// is Full Brain's chat picker, and Entry already has one — the notes list this conversation
// opened from. Two pickers for one surface would be two answers to "which conversation am I
// in". The **Proposals** panel stays: a note thread stages `prefs_write`, whose kind
// (`owner-prefs`) is not an `INLINE_KIND`, so its turn draws the navigational "Review
// proposal" chip — and a host that mounted the transcript without the panel would draw a
// chip whose tap did nothing.

import type { ReactNode } from "react";
import type { ModelLoad } from "../api/client";
import { AgentTranscript, ProposalsAside } from "./FullBrainSurface";
import type { FullBrain } from "./useFullBrain";

export interface NoteConversationProps {
  fb: FullBrain;
  /** True once the lookup has answered that this note has no conversation at all. */
  noThread: boolean;
  /** A Worked-block source card opens the cited note's own screen. */
  onOpenNote?: ((noteId: string) => void) | undefined;
  /** A response entity chip opens the entity page above home. */
  onOpenEntity?: ((entityId: string) => void) | undefined;
  /** Read-aloud controls, as the home conversation surface passes them. */
  readAloud?:
    | {
        playing: string | null;
        autoPlay: boolean;
        onToggle: (key: string, text: string) => void;
        onToggleAuto: () => void;
      }
    | undefined;
  /** The box's in-flight model load — a note turn waits on the same weights a chat turn
   * does, and without it that minute reads as the agent hanging. */
  modelLoad?: ModelLoad | null | undefined;
  /** Refresh the stream after an enacted proposal creates a note out of band. */
  onProposalEnacted?: (() => void) | undefined;
}

export function NoteConversation({
  fb,
  noThread,
  onOpenNote,
  onOpenEntity,
  readAloud,
  modelLoad,
  onProposalEnacted,
}: NoteConversationProps): ReactNode {
  return (
    <div className="fb-shell">
      <AgentTranscript
        fb={fb}
        onOpenNote={onOpenNote}
        onOpenEntity={onOpenEntity}
        onProposalEnacted={onProposalEnacted}
        readAloud={readAloud}
        modelLoad={modelLoad}
        noSessionText={
          noThread
            ? "No conversation yet — the box reads a note once it has indexed it, and the thread opens here."
            : "Opening this note's conversation…"
        }
        emptyText="Say something about this note — it reads the note with you."
      />
      <ProposalsAside fb={fb} onProposalEnacted={onProposalEnacted} />
    </div>
  );
}
