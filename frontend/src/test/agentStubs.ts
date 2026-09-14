// Shared conversation-controller stubs for tests that mount a surface which OWNS a
// `useFullBrain` but is not about it — chiefly `NoteScreen`, whose Thread tab is the
// default and stays mounted behind the other two. Without these the note-screen suites
// would put `/agent/sessions`, `/agent/proposals` and the capability probe on every fetch
// stub in the file, for a conversation none of those tests exercises.

import { vi } from "vitest";
import type { ChatEvent, ChatRequest, TranscriptTurn } from "../agent/types";
import type { FullBrainDeps } from "../agent/useFullBrain";
import type { NoteThreadOut } from "../api/client";

/** A controller whose every dependency answers empty. Override what a test is about. */
export function stubFullBrainDeps(over: Partial<FullBrainDeps> = {}): FullBrainDeps {
  return {
    listSessions: vi.fn(async () => []),
    createSession: vi.fn(async () => {
      throw new Error("a note thread never creates a session");
    }),
    chat: async function* (_body: ChatRequest): AsyncGenerator<ChatEvent> {},
    chatResume: async function* () {},
    sessionLiveRun: vi.fn(async () => null),
    cancelChatRun: vi.fn(async () => {}),
    listProposals: vi.fn(async () => []),
    getTranscript: vi.fn(async (): Promise<TranscriptTurn[]> => []),
    renameSession: vi.fn(async () => {}),
    deleteSession: vi.fn(async () => {}),
    archiveSession: vi.fn(async () => {}),
    unarchiveSession: vi.fn(async () => {}),
    rescopeSession: vi.fn(async () => {}),
    uploadChatAttachment: vi.fn(async () => ({
      id: "att",
      filename: "f",
      media_type: "text/plain",
      size_bytes: 1,
    })),
    getChatCapabilities: vi.fn(async () => ({
      supports_vision: false,
      can_analyze_images: false,
      context_window: 262144,
    })),
    ...over,
  };
}

/** The NoteScreen props that make its Thread tab inert: no conversation, no network. */
export function inertThread(): {
  fbDeps: FullBrainDeps;
  lookupThread: (noteId: string) => Promise<NoteThreadOut | null>;
} {
  return { fbDeps: stubFullBrainDeps(), lookupThread: vi.fn(async () => null) };
}
