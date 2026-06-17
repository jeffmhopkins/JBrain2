// The wiki Talk board (Phase 6, Wave T1 — docs/mocks/wiki-talk-b-topics.html): a persistent,
// article-anchored editorial board. Threaded topics with open/resolved badges, an auto "Build log"
// topic the builder posts to, signed + timestamped posts (You / Editor / Builder), a "New topic"
// composer, and a per-topic reply box. Owner-only. The wiki stays machine-written — Talk is the
// front-end over the sanctioned levers; the Editor (live agent) voice arrives in Wave T2.
//
// Mirrors the reader's shell: the loading|error|done machine, the useEffect fetch, the
// swipe-down-to-close handler, TopBar + screen-body. Mutations update local board state in place.

import { type TouchEvent, useEffect, useRef, useState } from "react";
import { type WikiTalkOut, type WikiTalkPost, type WikiTalkTopic, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { BookIcon, CheckIcon, ChevronRightIcon, PlusIcon } from "../components/icons";
import type { SyncStatus } from "../notes/useNotes";
import { talkTime } from "./wiki/talkTime";

const SWIPE_DOWN_PX = 56;

type TalkState = { phase: "loading" } | { phase: "error" } | { phase: "done"; board: WikiTalkOut };

interface TalkScreenProps {
  articleId: string;
  syncStatus: SyncStatus;
  onClose: () => void;
  onOpenArticle: (articleId: string) => void;
}

const VOICE: Record<WikiTalkPost["author"], string> = {
  owner: "You",
  editor: "Editor",
  builder: "Builder",
};

export function TalkScreen({ articleId, syncStatus, onClose, onOpenArticle }: TalkScreenProps) {
  const [state, setState] = useState<TalkState>({ phase: "loading" });
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [composing, setComposing] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newBody, setNewBody] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const swipeStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    let stale = false;
    setState({ phase: "loading" });
    api
      .getTalk(articleId)
      .then((board) => {
        if (stale) return;
        setState({ phase: "done", board });
        // Expand the first topic by default, like the mock's opened lead thread.
        if (board.topics[0]) setOpen(new Set([board.topics[0].id]));
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [articleId]);

  function onTouchStart(event: TouchEvent) {
    if ((scrollerRef.current?.scrollTop ?? 0) > 4) {
      swipeStart.current = null;
      return;
    }
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }

  function onTouchMove(event: TouchEvent) {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dy = t.clientY - start.y;
    const dx = Math.abs(t.clientX - start.x);
    if (dy > SWIPE_DOWN_PX && dy > dx * 2) {
      swipeStart.current = null;
      onClose();
    }
  }

  function toggle(id: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Replace one topic in the loaded board (immutable update after a mutation).
  function patchTopic(id: string, update: (t: WikiTalkTopic) => WikiTalkTopic) {
    setState((s) =>
      s.phase === "done"
        ? {
            phase: "done",
            board: { ...s.board, topics: s.board.topics.map((t) => (t.id === id ? update(t) : t)) },
          }
        : s,
    );
  }

  async function createTopic() {
    if (!newTitle.trim() || !newBody.trim() || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      const topic = await api.createTalkTopic(articleId, { title: newTitle, body: newBody });
      setState((s) =>
        s.phase === "done"
          ? { phase: "done", board: { ...s.board, topics: [topic, ...s.board.topics] } }
          : s,
      );
      setOpen((prev) => new Set([topic.id, ...prev]));
      setNewTitle("");
      setNewBody("");
      setComposing(false);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  async function postReply(topicId: string) {
    const body = (drafts[topicId] ?? "").trim();
    if (!body || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      const post = await api.postTalkReply(articleId, topicId, { body });
      patchTopic(topicId, (t) => ({ ...t, posts: [...t.posts, post] }));
      setDrafts((d) => ({ ...d, [topicId]: "" }));
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  async function toggleResolved(topic: WikiTalkTopic) {
    if (busy) return;
    const next = topic.status === "resolved" ? "open" : "resolved";
    setBusy(true);
    setFailed(false);
    try {
      await api.setTalkTopicStatus(articleId, topic.id, next);
      patchTopic(topic.id, (t) => ({ ...t, status: next }));
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  const board = state.phase === "done" ? state.board : null;

  return (
    <div className="subscreen subscreen-talk" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <TopBar
        title={board ? board.title : "Talk"}
        onBack={onClose}
        syncStatus={syncStatus}
        onBolt={onClose}
      />
      <main className="screen-body talk-view" ref={scrollerRef}>
        {state.phase === "loading" && <p className="analysis-quiet">loading discussion…</p>}
        {state.phase === "error" && (
          <p className="analysis-quiet">couldn't load the discussion — reopen to retry.</p>
        )}
        {board && (
          <>
            <div className="talk-bar">
              <h2>Discussion</h2>
              <button
                type="button"
                className="talk-openart"
                onClick={() => onOpenArticle(articleId)}
                aria-label="Open article"
              >
                <BookIcon size={16} />
              </button>
              <button type="button" className="talk-new" onClick={() => setComposing((v) => !v)}>
                <PlusIcon size={14} />
                New topic
              </button>
            </div>

            {composing && (
              <div className="talk-compose">
                <input
                  className="talk-compose-title"
                  placeholder="Topic title"
                  aria-label="Topic title"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                />
                <textarea
                  className="talk-compose-body"
                  placeholder="What's the editorial issue?"
                  aria-label="What's the editorial issue?"
                  value={newBody}
                  onChange={(e) => setNewBody(e.target.value)}
                />
                <div className="talk-compose-row">
                  <button type="button" className="talk-cancel" onClick={() => setComposing(false)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="talk-create"
                    disabled={!newTitle.trim() || !newBody.trim() || busy}
                    onClick={createTopic}
                  >
                    Create topic
                  </button>
                </div>
              </div>
            )}

            {failed && <p className="talk-err">something went wrong — try again.</p>}

            {board.topics.map((topic) => {
              const isOpen = open.has(topic.id);
              const isLog = topic.kind === "build_log";
              return (
                <section key={topic.id} className={`talk-topic${isOpen ? " open" : ""}`}>
                  <button type="button" className="talk-thead" onClick={() => toggle(topic.id)}>
                    <span className="talk-chev">
                      <ChevronRightIcon size={16} />
                    </span>
                    <span className="talk-tt">{topic.title}</span>
                    {isLog ? (
                      <span className="talk-meta">{topic.meta}</span>
                    ) : (
                      <span className={`talk-badge ${topic.status}`}>{topic.status}</span>
                    )}
                  </button>
                  {isOpen && (
                    <div className="talk-tbody">
                      {topic.posts.map((post) => (
                        <Post key={post.id} post={post} />
                      ))}
                      {!isLog && (
                        <>
                          <div className="talk-replybox">
                            <input
                              placeholder="Reply…"
                              aria-label={`Reply to ${topic.title}`}
                              value={drafts[topic.id] ?? ""}
                              onChange={(e) =>
                                setDrafts((d) => ({ ...d, [topic.id]: e.target.value }))
                              }
                            />
                            <button
                              type="button"
                              disabled={!(drafts[topic.id] ?? "").trim() || busy}
                              onClick={() => postReply(topic.id)}
                            >
                              Post
                            </button>
                          </div>
                          <button
                            type="button"
                            className="talk-resolve"
                            onClick={() => toggleResolved(topic)}
                          >
                            {topic.status === "resolved" ? "Reopen" : "Mark resolved"}
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </section>
              );
            })}
          </>
        )}
      </main>
    </div>
  );
}

function Post({ post }: { post: WikiTalkPost }) {
  const bot = post.author !== "owner";
  const rev = post.rev !== null ? ` · rev ${post.rev}` : "";
  return (
    <div className={`talk-reply${bot ? " bot" : ""}`}>
      <div className="talk-sig">
        <b>{VOICE[post.author]}</b> · {talkTime(post.created_at)}
        {rev}
      </div>
      <div className="talk-body">{post.body}</div>
      {post.source && (
        <div className="talk-src">
          {post.source.meta} — {post.source.snippet}
        </div>
      )}
      {post.outcome && (
        <div className="talk-outcome">
          <CheckIcon size={13} />
          {post.outcome}
        </div>
      )}
    </div>
  );
}
