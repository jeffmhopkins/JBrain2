import { type TouchEvent, useCallback, useEffect, useRef, useState } from "react";
import { type Principal, type SearchResult, api, setUnauthorizedHandler } from "./api/client";
import { EditLayer } from "./components/EditLayer";
import { Launcher, type LauncherHandle, type LauncherTarget } from "./components/Launcher";
import { MoveDomainSheet } from "./components/MoveDomainSheet";
import { TopBar } from "./components/TopBar";
import { useNoteActions } from "./notes/useNoteActions";
import { type StreamAttachment, type StreamItem, useNotes } from "./notes/useNotes";
import { CalendarScreen } from "./screens/CalendarScreen";
import { EntityListScreen } from "./screens/EntityListScreen";
import { EntityScreen } from "./screens/EntityScreen";
import { GraphScreen } from "./screens/GraphScreen";
import { type ComposeHandoff, HomeScreen } from "./screens/HomeScreen";
import { LLMSettingsScreen } from "./screens/LLMSettingsScreen";
import { ListDetailScreen } from "./screens/ListDetailScreen";
import { ListsScreen } from "./screens/ListsScreen";
import { LoginScreen } from "./screens/LoginScreen";
import {
  NoteScreen,
  type NoteViewSource,
  noteViewFromItem,
  noteViewFromSearch,
} from "./screens/NoteScreen";
import { OpsScreen } from "./screens/OpsScreen";
import { ReviewScreen } from "./screens/ReviewScreen";
import { SearchScreen } from "./screens/SearchScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { useBackGesture } from "./useBackGesture";

type Session =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "in"; principal: Principal };

type Card =
  | "ops"
  | "settings"
  | "llm-settings"
  | "search"
  | "review"
  | "entities"
  | "lists"
  | "calendar"
  | "graph";

const SCREEN_TITLES: Record<Card, string> = {
  ops: "Ops",
  settings: "Settings",
  "llm-settings": "LLM Settings",
  search: "Search",
  review: "Review",
  entities: "Entities",
  lists: "Lists",
  calendar: "Calendar",
  graph: "Map",
};

const CARD_EXIT_MS = 150;

export function App() {
  const [session, setSession] = useState<Session>({ status: "loading" });
  const [card, setCard] = useState<Card | null>(null);
  const [cardClosing, setCardClosing] = useState(false);
  const [launcherOpen, setLauncherOpen] = useState(false);
  // A calendar action (reschedule/cancel/ask) hands a prompt to the Full Brain
  // composer: close the card, then HomeScreen flips to Full Brain and seeds it.
  const [compose, setCompose] = useState<ComposeHandoff | null>(null);
  const clearCompose = useCallback(() => setCompose(null), []);
  // The note view is its own tree layer above home AND above search results.
  const [noteView, setNoteView] = useState<NoteViewSource | null>(null);
  const [noteClosing, setNoteClosing] = useState(false);
  // The entity page stacks one layer above the note view (analysis chips).
  const [entityView, setEntityView] = useState<string | null>(null);
  const [entityClosing, setEntityClosing] = useState(false);
  // A list's checklist is its own layer above the Lists grid; `listsKey` remounts
  // the grid on close so its card previews/counts reflect any edits.
  const [listView, setListView] = useState<string | null>(null);
  const [listsKey, setListsKey] = useState(0);
  // Lets the back gesture run the launcher's own slide-down close, the same
  // retreat swipe-down/Escape trigger — not an abrupt unmount.
  const launcherRef = useRef<LauncherHandle>(null);

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
    setEntityView(null);
    setListView(null);
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

  function closeEntityView() {
    if (reducedMotion()) {
      setEntityView(null);
      return;
    }
    setEntityClosing(true);
    setTimeout(() => {
      setEntityClosing(false);
      setEntityView(null);
    }, CARD_EXIT_MS);
  }

  function openNoteFromStream(item: StreamItem) {
    setNoteView(noteViewFromItem(item));
  }

  function openNoteFromSearch(result: SearchResult) {
    setNoteView(noteViewFromSearch(result));
  }

  // Entity-page mention tap: open the note view beneath if the note is
  // reachable; otherwise stay put — the snippet is already on screen.
  async function openNoteFromEntity(noteId: string) {
    const item = await notes.fetchById(noteId);
    if (item === null) return;
    setNoteView(noteViewFromItem(item));
    closeEntityView();
  }

  // A Full Brain source card (Worked block) opens the cited note over the chat.
  async function openNoteById(noteId: string) {
    const item = await notes.fetchById(noteId);
    if (item !== null) setNoteView(noteViewFromItem(item));
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
    // The Map owns vertical drags (pan/pinch), so don't arm the down-swipe
    // dismiss over it — use the back/bolt controls to leave instead.
    if (card === "graph") {
      swipeStart.current = null;
      return;
    }
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

  // The platform back gesture climbs one level, exactly like swipe-down: close
  // the topmost open layer, in the same z-order the overlays render. The edit
  // layer and move sheet run their own dismissal, so they sit on top here too.
  const overlayDepth =
    (actions.editing !== null ? 1 : 0) +
    (actions.moveTarget !== null ? 1 : 0) +
    (entityView !== null ? 1 : 0) +
    (noteView !== null ? 1 : 0) +
    (listView !== null ? 1 : 0) +
    (card !== null ? 1 : 0) +
    (launcherOpen ? 1 : 0);

  function closeTopLayer() {
    if (actions.editing !== null) return actions.cancelEdit();
    if (actions.moveTarget !== null) return actions.cancelMove();
    if (entityView !== null) return closeEntityView();
    if (noteView !== null) return closeNoteView();
    if (listView !== null) {
      setListView(null);
      setListsKey((k) => k + 1);
      return;
    }
    if (card !== null) return closeCardToLauncher();
    if (launcherOpen) {
      // Run the launcher's own slide-down retreat, then it settles closed.
      if (launcherRef.current) launcherRef.current.close();
      else setLauncherOpen(false);
    }
  }

  useBackGesture(overlayDepth, closeTopLayer);

  if (session.status === "loading") {
    return <main className="centered muted">Loading…</main>;
  }

  if (session.status === "anonymous") {
    return <LoginScreen onLogin={(principal) => setSession({ status: "in", principal })} />;
  }

  return (
    <div className="shell">
      {/* The home top bar lives inside HomeScreen so it can swap the wordmark for
          the active Full Brain session; sub-screen overlays bring their own. */}
      {/* Home stays mounted so stream scroll position survives sub-screens. */}
      <div className={`screen-home${card === null && !launcherOpen ? "" : " screen-hidden"}`}>
        <HomeScreen
          notes={notes}
          actions={actions}
          onOpenNote={openNoteFromStream}
          onOpenNoteById={(noteId) => void openNoteById(noteId)}
          onOpenEntity={setEntityView}
          onOpenSearch={() => setCard("search")}
          onOpenLauncher={() => setLauncherOpen(true)}
          compose={compose}
          onComposeConsumed={clearCompose}
        />
      </div>

      <Launcher
        ref={launcherRef}
        open={launcherOpen}
        onClose={() => setLauncherOpen(false)}
        onNavigate={navigate}
      />

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
          {card === "llm-settings" && <LLMSettingsScreen />}
          {card === "search" && <SearchScreen onOpenResult={openNoteFromSearch} />}
          {card === "calendar" && (
            <CalendarScreen
              onOpenNote={(noteId) => void openNoteById(noteId)}
              onCompose={(text, appt) => {
                setCard(null);
                setCompose({ text, appt });
              }}
            />
          )}
          {card === "review" && <ReviewScreen />}
          {/* Rows open the same entity layer the analysis chips use. */}
          {card === "entities" && <EntityListScreen onOpenEntity={setEntityView} />}
          {/* The graph Map drills into focus in place; the sheet opens the entity layer. */}
          {card === "graph" && <GraphScreen onOpenEntity={setEntityView} />}
          {/* Cards open the list detail layer; listsKey remounts on its close. */}
          {card === "lists" && <ListsScreen key={listsKey} onOpenList={setListView} />}
        </div>
      )}

      {listView !== null && (
        <ListDetailScreen
          key={listView}
          listId={listView}
          syncStatus={notes.syncStatus}
          onClose={() => {
            setListView(null);
            setListsKey((k) => k + 1);
          }}
        />
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
            onOpenEntity={setEntityView}
          />
        </div>
      )}

      {entityView !== null && (
        <div className={entityClosing ? "entity-layer-closing" : undefined}>
          <EntityScreen
            key={entityView}
            entityId={entityView}
            syncStatus={notes.syncStatus}
            onClose={closeEntityView}
            onOpenEntity={setEntityView}
            onOpenNote={(noteId) => void openNoteFromEntity(noteId)}
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
