import { type TouchEvent, useCallback, useEffect, useRef, useState } from "react";
import { type Principal, type SearchResult, api, setUnauthorizedHandler } from "./api/client";
import { closeTopModalLayer, useModalLayerCount } from "./backLayers";
import { EditLayer } from "./components/EditLayer";
import { Launcher, type LauncherTarget } from "./components/Launcher";
import { MoveDomainSheet } from "./components/MoveDomainSheet";
import { PresenceToast } from "./components/PresenceToast";
import { TopBar } from "./components/TopBar";
import { closeHomeBackLayer, useHomeBackDepth } from "./homeBack";
import { useNoteActions } from "./notes/useNoteActions";
import { type StreamAttachment, type StreamItem, useNotes } from "./notes/useNotes";
import { AutomationsScreen } from "./screens/AutomationsScreen";
import { CalendarScreen } from "./screens/CalendarScreen";
import { ControlScreen } from "./screens/ControlScreen";
import { DataScreen } from "./screens/DataScreen";
import { EntityListScreen } from "./screens/EntityListScreen";
import { EntityScreen } from "./screens/EntityScreen";
import { GraphScreen } from "./screens/GraphScreen";
import { type ComposeHandoff, HomeScreen } from "./screens/HomeScreen";
import { ImageScreen } from "./screens/ImageScreen";
import { IntakeLinksScreen } from "./screens/IntakeLinksScreen";
import { JcodeScreen } from "./screens/JcodeScreen";
import { LLMSettingsScreen } from "./screens/LLMSettingsScreen";
import { ListDetailScreen } from "./screens/ListDetailScreen";
import { ListsScreen } from "./screens/ListsScreen";
import { LocationScreen } from "./screens/LocationScreen";
import { LoginScreen } from "./screens/LoginScreen";
import {
  NoteScreen,
  type NoteViewSource,
  noteViewFromItem,
  noteViewFromSearch,
} from "./screens/NoteScreen";
import { OpsScreen } from "./screens/OpsScreen";
import { ResearchDetailScreen } from "./screens/ResearchDetailScreen";
import { type ResearchKind, ResearchScreen } from "./screens/ResearchScreen";
import { ReviewScreen } from "./screens/ReviewScreen";
import { RunsScreen } from "./screens/RunsScreen";
import { SearchScreen } from "./screens/SearchScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { TalkScreen } from "./screens/TalkScreen";
import { TasksScreen } from "./screens/TasksScreen";
import { WikiLandingScreen } from "./screens/WikiLandingScreen";
import { WikiScreen } from "./screens/WikiScreen";
import { useBackGesture } from "./useBackGesture";

type Session =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "in"; principal: Principal };

type Card =
  | "ops"
  | "automations"
  | "tasks"
  | "data"
  | "settings"
  | "llm-settings"
  | "search"
  | "research"
  | "review"
  | "entities"
  | "lists"
  | "calendar"
  | "graph"
  | "location"
  | "wiki"
  | "image"
  | "intake"
  | "petcontrol"
  | "jcode";

// Automations, Tasks, Image and jcode bring their own full-screen overlay (own back
// bar + slide-in), so they render outside the shared subscreen TopBar wrapper — hence
// no entry here. Every Card that uses the wrapper needs a title.
const SCREEN_TITLES: Record<
  Exclude<Card, "automations" | "tasks" | "image" | "jcode" | "petcontrol">,
  string
> = {
  ops: "Ops",
  data: "Data",
  settings: "Settings",
  "llm-settings": "LLM Settings",
  search: "Search",
  research: "Research Library",
  review: "Review",
  entities: "Entities",
  lists: "Lists",
  calendar: "Calendar",
  graph: "Map",
  location: "Location",
  wiki: "Wiki",
  intake: "Intake Links",
};

const CARD_EXIT_MS = 150;

export function App() {
  const [session, setSession] = useState<Session>({ status: "loading" });
  const [card, setCard] = useState<Card | null>(null);
  const [cardClosing, setCardClosing] = useState(false);
  const [launcherOpen, setLauncherOpen] = useState(false);
  // The Runs surface stacks one layer above Automations: Automations' "All runs"
  // drill-through opens it, and its own back bar closes it back to Automations.
  const [runsOpen, setRunsOpen] = useState(false);
  // A calendar action (reschedule/cancel/ask) hands a prompt to the Full Brain
  // composer: close the card, then HomeScreen flips to Full Brain and seeds it.
  const [compose, setCompose] = useState<ComposeHandoff | null>(null);
  const clearCompose = useCallback(() => setCompose(null), []);
  // A Tasks run → open its session in Full Brain: close the card, hand the id +
  // persona to HomeScreen, which flips to the right tab and opens it.
  const [openSession, setOpenSession] = useState<{ id: string; agent: string } | null>(null);
  const clearOpenSession = useCallback(() => setOpenSession(null), []);
  // A session opened from a self-contained card (Tasks) drops that card to reveal
  // the chat on home — leaving nothing in the layer stack to pop. Remember the card
  // so the back gesture climbs back to it instead of exiting the app.
  const [sessionBackTo, setSessionBackTo] = useState<Card | null>(null);
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
  // The wiki reader stacks above the wiki landing (and above Search, when a wiki
  // hit is tapped there) — its own layer, like the entity page.
  const [wikiArticle, setWikiArticle] = useState<string | null>(null);
  const [talkArticle, setTalkArticle] = useState<string | null>(null);
  const [wikiClosing, setWikiClosing] = useState(false);
  // The Research Library detail (a report or a video) stacks one layer above the
  // Research list card, like the wiki reader over its landing.
  const [researchDetail, setResearchDetail] = useState<{ kind: ResearchKind; id: string } | null>(
    null,
  );

  // The bare home stream is on screen only when no card, launcher, or stacked
  // reading layer covers it; while it's buried behind one there's no reason to
  // keep polling the list (the outbox still flushes on reconnect from anywhere).
  const homeVisible =
    card === null &&
    !launcherOpen &&
    noteView === null &&
    entityView === null &&
    wikiArticle === null &&
    talkArticle === null &&
    listView === null &&
    researchDetail === null &&
    !runsOpen;

  // Lives at the app level so its state (and the outbox) survives every
  // sub-screen; `homeVisible` gates the list poll to when the stream is shown.
  const notes = useNotes(session.status === "in", homeVisible);
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

  // Keep the owner's display timezone in sync with this device's zone, so the
  // agent's server-rendered time prose matches the client-localized cards. Only
  // PUTs on a change, and a best-effort failure is harmless (server falls back
  // to UTC). Re-detect zone could change if the device travels.
  useEffect(() => {
    if (session.status !== "in") return;
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone) return;
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (!stale && s.owner_timezone !== zone) {
          return api.updateSettings({ owner_timezone: zone });
        }
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [session.status]);

  async function logout() {
    try {
      await api.logout();
    } catch {
      // Even if the server call fails the local session is done.
    }
    setCard(null);
    setLauncherOpen(false);
    setRunsOpen(false);
    setNoteView(null);
    setEntityView(null);
    setListView(null);
    setWikiArticle(null);
    setTalkArticle(null);
    setResearchDetail(null);
    setSession({ status: "anonymous" });
  }

  function navigate(target: LauncherTarget) {
    // Picking a new card abandons any "return to the session's source card" intent.
    setSessionBackTo(null);
    setCard(target);
  }

  // Automations owns its own full-screen overlay, so it closes straight to the
  // launcher (no subscreen slide-out to play) and drops any open Runs layer.
  function closeAutomations() {
    setRunsOpen(false);
    setCard(null);
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

  function closeWikiArticle() {
    if (reducedMotion()) {
      setWikiArticle(null);
      return;
    }
    setWikiClosing(true);
    setTimeout(() => {
      setWikiClosing(false);
      setWikiArticle(null);
    }, CARD_EXIT_MS);
  }

  // The Talk board stacks above the reader; jumping to the article closes Talk and opens it.
  function openArticleFromTalk(id: string) {
    setTalkArticle(null);
    setWikiArticle(id);
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

  // "Open in jerv conversation" from the Research Library: drop the detail + list card +
  // launcher to reveal home, then seed the owner's current Research (jerv) chat — the
  // agent that produced these artifacts — with a reference to the item.
  function openInJerv(text: string) {
    setResearchDetail(null);
    setCard(null);
    setLauncherOpen(false);
    setCompose({ text, mode: "research" });
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
    // The graph and location Maps own vertical drags (pan/pinch), so don't arm
    // the down-swipe dismiss over them — use the back/bolt controls to leave.
    if (card === "graph" || card === "location") {
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
  // the topmost open layer, in the same z-order the overlays render. Sheets
  // (and any future <Dialog>) self-register in the shared back-layer stack and
  // render above every screen, so they count via `modalDepth` and close first;
  // the move sheet is one of them. The edit layer is a full screen, so it stays
  // an explicit term here.
  const modalDepth = useModalLayerCount();
  // The home surface's own open layers (Full Brain Sessions/Proposals panel, an open
  // Proposal) — counted so the back gesture climbs them before it reaches the bare chat.
  const homeDepth = useHomeBackDepth();
  const overlayDepth =
    modalDepth +
    homeDepth +
    (actions.editing !== null ? 1 : 0) +
    (talkArticle !== null ? 1 : 0) +
    (wikiArticle !== null ? 1 : 0) +
    (entityView !== null ? 1 : 0) +
    (noteView !== null ? 1 : 0) +
    (listView !== null ? 1 : 0) +
    (researchDetail !== null ? 1 : 0) +
    (runsOpen ? 1 : 0) +
    (card !== null ? 1 : 0) +
    (launcherOpen ? 1 : 0) +
    (sessionBackTo !== null ? 1 : 0);

  function closeTopLayer() {
    // Sheets/dialogs render above every screen, so pop the topmost one first;
    // its own onClose (e.g. cancelMove) clears the owning state as it unmounts.
    if (closeTopModalLayer()) return;
    if (actions.editing !== null) return actions.cancelEdit();
    // The Talk board stacks above the reader (opened from it), so it climbs off first.
    if (talkArticle !== null) return setTalkArticle(null);
    // The wiki reader is the topmost reading layer (opened from the landing or a
    // search hit), so it climbs off before the entity/note/card layers beneath.
    if (wikiArticle !== null) return closeWikiArticle();
    if (entityView !== null) return closeEntityView();
    if (noteView !== null) return closeNoteView();
    if (listView !== null) {
      setListView(null);
      setListsKey((k) => k + 1);
      return;
    }
    // The Research detail stacks above the Research list card, so it climbs off first.
    if (researchDetail !== null) return setResearchDetail(null);
    // Runs stacks above Automations, so it climbs off first.
    if (runsOpen) return setRunsOpen(false);
    if (card === "automations") return closeAutomations();
    // Tasks/Image/jcode close straight to the launcher (own overlay, no subscreen slide).
    if (card === "tasks") return setCard(null);
    if (card === "image") return setCard(null);
    if (card === "jcode") return setCard(null);
    if (card === "petcontrol") return setCard(null);
    if (card !== null) return closeCardToLauncher();
    // Drops the depth immediately; the launcher plays its retreat off `open`.
    if (launcherOpen) return setLauncherOpen(false);
    // The home conversation surface is beneath everything above, so its own layers (an
    // open Proposal, then the Sessions/Proposals panel) climb off only once nothing
    // higher remains — and before the Tasks return, since they sit atop that chat.
    if (closeHomeBackLayer()) return;
    // A session opened from Tasks dropped its card to reveal the chat — climb back
    // to that card rather than letting the platform back gesture exit the app.
    if (sessionBackTo !== null) {
      const back = sessionBackTo;
      setSessionBackTo(null);
      return setCard(back);
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
          openSession={openSession}
          onOpenSessionConsumed={clearOpenSession}
        />
      </div>

      <Launcher
        open={launcherOpen}
        active={card === null}
        onClose={() => setLauncherOpen(false)}
        onNavigate={navigate}
      />

      {/* Automations is a self-contained full-screen overlay (its own back bar +
          slide-in), rendered below — it skips the shared subscreen TopBar. */}
      {card !== null &&
        card !== "automations" &&
        card !== "tasks" &&
        card !== "image" &&
        card !== "petcontrol" &&
        card !== "jcode" && (
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
              <SettingsScreen
                deviceLabel={session.principal.label}
                onLogout={() => void logout()}
              />
            )}
            {card === "llm-settings" && <LLMSettingsScreen />}
            {card === "data" && <DataScreen />}
            {card === "search" && (
              <SearchScreen onOpenResult={openNoteFromSearch} onOpenWiki={setWikiArticle} />
            )}
            {/* The Research Library list; a row/⋯-View opens the detail layer above. */}
            {card === "research" && (
              <ResearchScreen
                onOpen={(kind, id) => setResearchDetail({ kind, id })}
                onOpenInJerv={openInJerv}
              />
            )}
            {/* The wiki landing: search-first rails over the article set; a row
              opens the reader layer above. */}
            {card === "wiki" && <WikiLandingScreen onOpenArticle={setWikiArticle} />}
            {card === "calendar" && (
              <CalendarScreen
                onOpenNote={(noteId) => void openNoteById(noteId)}
                onCompose={(text, appt) => {
                  setCard(null);
                  setLauncherOpen(false);
                  setCompose({ text, appt });
                }}
              />
            )}
            {card === "review" && <ReviewScreen />}
            {card === "intake" && <IntakeLinksScreen />}
            {/* Rows open the same entity layer the analysis chips use. */}
            {card === "entities" && <EntityListScreen onOpenEntity={setEntityView} />}
            {/* The graph Map drills into focus in place; the sheet opens the entity layer. */}
            {card === "graph" && <GraphScreen onOpenEntity={setEntityView} />}
            {/* Cards open the list detail layer; listsKey remounts on its close. */}
            {card === "lists" && <ListsScreen key={listsKey} onOpenList={setListView} />}
            {/* Owner-only location surface: Devices / Timeline / Map. */}
            {card === "location" && <LocationScreen />}
          </div>
        )}

      {/* The Workflow launcher card opens Automations as a top-level surface; its
          "All runs" drill-through raises the Runs surface one layer above. */}
      {card === "automations" && (
        <AutomationsScreen onClose={closeAutomations} onOpenRuns={() => setRunsOpen(true)} />
      )}
      {runsOpen && <RunsScreen onClose={() => setRunsOpen(false)} />}

      {/* The image launcher is a self-contained full-screen overlay (its own
          bespoke top bar — gallery shortcut + residency dot — that the shared
          TopBar can't carry), so it renders outside the shared subscreen wrapper. */}
      {card === "image" && <ImageScreen onClose={() => setCard(null)} />}

      {/* Tasks is a self-contained full-screen overlay (its own back bar), like
          Automations — so it renders outside the shared subscreen TopBar wrapper. */}
      {card === "tasks" && (
        <TasksScreen
          onClose={() => setCard(null)}
          onOpenSession={(sessionId, agent) => {
            // Drop both the Tasks card AND the launcher beneath it, so home/Full
            // Brain is revealed (not the launcher) to receive the session handoff.
            // Leave a return marker so the back gesture climbs back to Tasks.
            setCard(null);
            setLauncherOpen(false);
            setSessionBackTo("tasks");
            setOpenSession({ id: sessionId, agent });
          }}
        />
      )}

      {/* Code mode (jcode) is a self-contained full-screen overlay (its own back
          bar + internal list↔session navigation), like Tasks/Automations. */}
      {card === "jcode" && <JcodeScreen onClose={() => setCard(null)} />}
      {card === "petcontrol" && <ControlScreen onClose={() => setCard(null)} />}

      {/* The wiki reader brings its own subscreen + TopBar (like the entity
          page), so it renders outside the shared wrapper. It stacks above the
          wiki landing (or Search, when a wiki hit is tapped there). */}
      {wikiArticle !== null && (
        <div className={wikiClosing ? "wiki-layer-closing" : undefined}>
          <WikiScreen
            key={wikiArticle}
            articleId={wikiArticle}
            syncStatus={notes.syncStatus}
            onClose={closeWikiArticle}
            onOpenTalk={setTalkArticle}
          />
        </div>
      )}

      {talkArticle !== null && (
        <TalkScreen
          key={talkArticle}
          articleId={talkArticle}
          syncStatus={notes.syncStatus}
          onClose={() => setTalkArticle(null)}
          onOpenArticle={openArticleFromTalk}
        />
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

      {/* The Research Library detail (report/video) — its own subscreen + TopBar, stacked
          above the Research list card like the wiki reader over its landing. */}
      {researchDetail !== null && (
        <ResearchDetailScreen
          key={`${researchDetail.kind}-${researchDetail.id}`}
          kind={researchDetail.kind}
          id={researchDetail.id}
          syncStatus={notes.syncStatus}
          onClose={() => setResearchDetail(null)}
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

      {/* L7b: the app-open presence toast — the owner's own latest place, freshness-
          honest (teal fresh / amber "last known"), self-dismissing. Keyed on the
          principal so it raises once per app-open; "open" jumps to the Location
          surface. Absent when there's no usable fix (the toast renders nothing). */}
      <PresenceToast key={session.principal.principal_id} onOpen={() => setCard("location")} />
    </div>
  );
}
