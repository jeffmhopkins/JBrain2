// Shared conversation-controller stubs for tests that mount a surface which OWNS a
// `useFullBrain` but is not about all of it — chiefly the Entry note conversation, whose
// suite cares about one thread and not about sessions, proposals or the capability probe.

import { vi } from "vitest";
import type { ChatEvent, ChatRequest, TranscriptTurn } from "../agent/types";
import type { FullBrainDeps } from "../agent/useFullBrain";

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
