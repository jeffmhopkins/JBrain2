import { type TouchEvent, useEffect, useRef, useState } from "react";
import { type Principal, type SearchResult, api, setUnauthorizedHandler } from "./api/client";
import { EditLayer } from "./components/EditLayer";
import { Launcher, type LauncherTarget } from "./components/Launcher";
import { MoveDomainSheet } from "./components/MoveDomainSheet";
import { TopBar } from "./components/TopBar";
import { useNoteActions } from "./notes/useNoteActions";
import { type StreamAttachment, type StreamItem, useNotes } from "./notes/useNotes";
import { HomeScreen } from "./screens/HomeScreen";
import { LoginScreen } from "./screens/LoginScreen";
import {
  NoteScreen,
  type NoteViewSource,
  noteViewFromItem,
  noteViewFromSearch,
} from "./screens/NoteScreen";
import { OpsScreen } from "./screens/OpsScreen";
import { SearchScreen } from "./screens/SearchScreen";
import { SettingsScreen } from "./screens/SettingsScreen";

type Session =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "in"; principal: Principal };

type Card = "ops" | "settings" | "search";

const SCREEN_TITLES: Record<Card, string> = {
  ops: "Ops",
  settings: "Settings",
  search: "Search",
};

const CARD_EXIT_MS = 150;

export function App() {
  const [session, setSession] = useState<Session>({ status: "loading" });
  const [card, setCard] = useState<Card | null>(null);
  const [cardClosing, setCardClosing] = useState(false);
  const [launcherOpen, setLauncherOpen] = useState(false);
  // The note view is its own tree layer above home AND above search results.
  const [noteView, setNoteView] = useState<NoteViewSource | null>(null);
  const [noteClosing, setNoteClosing] = useState(false);

  // Lives at the app level so the outbox keeps flushing while the user is
  // on Ops or Settings.
  const notes = useNotes(session.status === "in");
  const actions = useNoteActions(notes);

  // Any 401 from the API means the cookie expired or was revoked.
  useEffect(() => {
    setUnauthorizedHandler(() => setSession({ status: "anonymous" }));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    api
      .me()
      .then((principal) => setSession({ status: "in", principal }))
      .catch(() => setSession({ status: "anonymous" }));
  }, []);

  async function logout() {
    try {
      await api.logout();
    } catch {
      // Even if the server call fails the local session is done.
    }
    setCard(null);
    setLauncherOpen(false);
    setNoteView(null);
    setSession({ status: "anonymous" });
  }

  function navigate(target: LauncherTarget) {
    setCard(target);
  }

  function reducedMotion(): boolean {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // Climb one level: the card sinks away, revealing the launcher beneath.
  function closeCardToLauncher() {
    if (reducedMotion()) {
      setCard(null);
      return;
    }
    setCardClosing(true);
    setTimeout(() => {
      setCardClosing(false);
      setCard(null);
    }, CARD_EXIT_MS);
  }

  // Chevron: jump straight home — drop the launcher instantly (the card
  // still covers it), then let the card sink to reveal home.
  function jumpHome() {
    setLauncherOpen(false);
    closeCardToLauncher();
  }

  function closeNoteView() {
    if (reducedMotion()) {
      setNoteView(null);
      return;
    }
    setNoteClosing(true);
    setTimeout(() => {
      setNoteClosing(false);
      setNoteView(null);
    }, CARD_EXIT_MS);
  }

  function openNoteFromStream(item: StreamItem) {
    setNoteView(noteViewFromItem(item));
  }

  function openNoteFromSearch(result: SearchResult) {
    setNoteView(noteViewFromSearch(result));
  }

  // Editing is a full-screen layer over wherever you are; underlying
  // layers stay put and the saved body is reflected into an open note view.
  function startEditFromNoteView(
    id: string,
    body: string,
    domain: string,
    createdAt: Date,
    attachments: StreamAttachment[],
  ) {
    actions.startEdit({ id, body, domain, createdAt, attachments });
  }

  async function saveEdit(body: string) {
    const id = actions.editing?.id;
    await actions.submitEdit(body);
    if (id !== undefined) {
      setNoteView((v) => (v !== null && v.id === id ? { ...v, body } : v));
    }
  }

  // Attachment changes from the editor's paperclip or the note view's
  // Attachments tab both land here, so an open note view stays in step.
  // Search-opened views carry attachments: null (unknown); those skip the
  // patch and re-resolve the full note instead.
  async function addAttachmentTo(noteId: string, file: File): Promise<StreamAttachment> {
    const added = await notes.addAttachment(noteId, file);
    setNoteView((v) =>
      v !== null && v.id === noteId && v.attachments !== null
        ? {
            ...v,
            attachments: [...v.attachments, added],
            attachmentCount: v.attachmentCount + 1,
          }
        : v,
    );
    return added;
  }

  async function removeAttachmentFrom(attachmentId: string): Promise<void> {
    await notes.removeAttachment(attachmentId);
    setNoteView((v) =>
      v?.attachments?.some((a) => a.id === attachmentId)
        ? {
            ...v,
            attachments: (v.attachments ?? []).filter((a) => a.id !== attachmentId),
            attachmentCount: Math.max(0, v.attachmentCount - 1),
          }
        : v,
    );
  }

  // Navigation is a tree: home → (swipe up) → launcher → (tap) → card
  // screen. Swiping DOWN climbs back up a level — card screen reopens the
  // launcher here; the launcher's own down-swipe returns home. Armed only
  // when the screen is scrolled to the top so it never fights scrolling.
  const swipeStart = useRef<{ x: number; y: number } | null>(null);
  const subRef = useRef<HTMLDivElement | null>(null);

  function onSubTouchStart(event: TouchEvent) {
    const scroller = subRef.current?.querySelector("main");
    if ((scroller?.scrollTop ?? 0) > 4) {
      swipeStart.current = null;
      return;
    }
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }

  function onSubTouchMove(event: TouchEvent) {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dy = t.clientY - start.y;
    const dx = Math.abs(t.clientX - start.x);
    if (dy > 56 && dy > dx * 2) {
      swipeStart.current = null;
      closeCardToLauncher();
    }
  }

  if (session.status === "loading") {
    return <main className="centered muted">Loading…</main>;
  }

  if (session.status === "anonymous") {
    return <LoginScreen onLogin={(principal) => setSession({ status: "in", principal })} />;
  }

  return (
    <div className="shell">
      <TopBar syncStatus={notes.syncStatus} onBolt={() => setLauncherOpen(true)} />

      {/* Home stays mounted so stream scroll position survives sub-screens. */}
      <div className={`screen-home${card === null && !launcherOpen ? "" : " screen-hidden"}`}>
        <HomeScreen
          notes={notes}
          actions={actions}
          onOpenNote={openNoteFromStream}
          onOpenSearch={() => setCard("search")}
          onOpenLauncher={() => setLauncherOpen(true)}
        />
      </div>

      <Launcher open={launcherOpen} onClose={() => setLauncherOpen(false)} onNavigate={navigate} />

      {card !== null && (
        <div
          className={`subscreen${cardClosing ? " subscreen-closing" : ""}`}
          ref={subRef}
          onTouchStart={onSubTouchStart}
          onTouchMove={onSubTouchMove}
        >
          <TopBar
            title={SCREEN_TITLES[card]}
            onBack={jumpHome}
            syncStatus={notes.syncStatus}
            onBolt={closeCardToLauncher}
          />
          {card === "ops" && (
            <main className="screen-body">
              <OpsScreen />
            </main>
          )}
          {card === "settings" && (
            <SettingsScreen deviceLabel={session.principal.label} onLogout={() => void logout()} />
          )}
          {card === "search" && <SearchScreen onOpenResult={openNoteFromSearch} />}
        </div>
      )}

      {noteView !== null && (
        <div className={noteClosing ? "note-layer-closing" : undefined}>
          <NoteScreen
            key={noteView.id ?? "pending"}
            source={noteView}
            resolve={notes.fetchById}
            syncStatus={notes.syncStatus}
            onClose={closeNoteView}
            onEdit={startEditFromNoteView}
            onMove={actions.startMove}
            onDelete={(id) => {
              void actions.remove(id);
              closeNoteView();
            }}
            onAddAttachment={addAttachmentTo}
            onRemoveAttachment={removeAttachmentFrom}
          />
        </div>
      )}

      {actions.moveTarget !== null && (
        <MoveDomainSheet
          target={actions.moveTarget}
          onClose={actions.cancelMove}
          onMove={(domain, destination) => void actions.submitMove(domain, destination)}
        />
      )}

      {actions.editing !== null && (
        <EditLayer
          editing={actions.editing}
          onCancel={actions.cancelEdit}
          onSave={(body) => void saveEdit(body)}
          onAddFile={(file) => addAttachmentTo(actions.editing?.id ?? "", file)}
          onRemoveAttachment={removeAttachmentFrom}
        />
      )}
    </div>
  );
}
