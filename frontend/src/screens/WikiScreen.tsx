// The wiki article reader (Phase 6, Wave B1 — docs/mocks/wiki-reader-*.html):
// a read-only, machine-written article rendered current-only. An infobox, an
// amber read-only pill, a prose lead, type-guided sections (domain dot + label)
// with nested subsections, lists + tables, inline [n] citations that tap-open a
// citation card AND index a numbered References list, and a "Discuss this
// article" sheet (the only correction path — the wiki is never edited directly).
//
// Mirrors EntityScreen's shell verbatim: the loading|error|done machine, the
// useEffect fetch, the swipe-down-to-close handler, and the subscreen/TopBar/
// screen-body structure. No backend — it renders against the FIXTURE.

import { type TouchEvent, useEffect, useRef, useState } from "react";
import { type WikiArticleOut, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { ChatIcon, EyeOffIcon } from "../components/icons";
import { DOMAIN_TITLE } from "../notes/modes";
import type { SyncStatus } from "../notes/useNotes";
import { ArticleBody } from "./wiki/ArticleBody";
import { CitationCard } from "./wiki/CitationCard";
import { DiscussSheet } from "./wiki/DiscussSheet";
import { Infobox } from "./wiki/Infobox";
import { ReferencesList, refDomId } from "./wiki/ReferencesList";
import { withCitations } from "./wiki/citations";
import { wikiDomainColor } from "./wiki/domain";

const SWIPE_DOWN_PX = 56;
// How long a jumped-to reference stays flashed (mirrors the mock's 2.2s).
const HIGHLIGHT_MS = 2200;

type WikiState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; article: WikiArticleOut };

interface WikiScreenProps {
  articleId: string;
  syncStatus: SyncStatus;
  onClose: () => void;
  // Open the article's Talk board (the threaded discussion + Build-log). Optional so the reader
  // still renders standalone in tests; the correction sheet (below) stays the quick-fix path.
  onOpenTalk?: (articleId: string) => void;
}

export function WikiScreen({ articleId, syncStatus, onClose, onOpenTalk }: WikiScreenProps) {
  const [state, setState] = useState<WikiState>({ phase: "loading" });
  // The citation number whose card is open (null = closed); and whether the
  // discuss sheet is open. The two sheets are mutually exclusive in the mock.
  const [citeN, setCiteN] = useState<number | null>(null);
  const [discussOpen, setDiscussOpen] = useState(false);
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const swipeStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    let stale = false;
    setState({ phase: "loading" });
    api
      .getWikiArticle(articleId)
      .then((article) => {
        if (!stale) setState({ phase: "done", article });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [articleId]);

  // Swipe-down at scroll-top climbs back, same as every card layer.
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

  // Close the citation card, scroll the matching reference into view, and flash
  // it briefly — the same "jump to references" affordance as the mock.
  function jumpToReference(n: number) {
    setCiteN(null);
    setHighlighted(n);
    const el = scrollerRef.current?.querySelector(`#${refDomId(n)}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => setHighlighted((h) => (h === n ? null : h)), HIGHLIGHT_MS);
  }

  const article = state.phase === "done" ? state.article : null;
  const citedRef = article && citeN !== null ? article.references.find((r) => r.n === citeN) : null;

  return (
    <div className="subscreen subscreen-wiki" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <TopBar
        title={article ? article.title : "Wiki"}
        onBack={onClose}
        syncStatus={syncStatus}
        onBolt={onClose}
      />
      <main className="screen-body wiki-view" ref={scrollerRef}>
        {state.phase === "loading" && <p className="analysis-quiet">loading article…</p>}
        {state.phase === "error" && (
          <p className="analysis-quiet">couldn't load this article — reopen to retry.</p>
        )}
        {article && (
          <>
            <h1 className="wiki-title">{article.title}</h1>
            <div className="wiki-subtitle">{article.subtitle}</div>
            <div className="wiki-ro">
              <EyeOffIcon size={13} />
              Read-only — correct it by discussing
            </div>

            <Infobox infobox={article.infobox} onCite={setCiteN} />

            {article.lead.map((para, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: lead paragraphs are static.
              <p className="wiki-p wiki-lead" key={i}>
                {withCitations(para.text, setCiteN)}
              </p>
            ))}

            {article.sections.map((section) => (
              <section key={section.heading}>
                <h2 className="wiki-sec">
                  <span
                    className="wiki-dom"
                    style={{ background: wikiDomainColor(section.domain) }}
                  />
                  {section.heading}
                  {section.domain !== "general" && (
                    <span className="wiki-dom-lbl">
                      {(DOMAIN_TITLE[section.domain] ?? section.domain).toLowerCase()}
                    </span>
                  )}
                </h2>
                <ArticleBody blocks={section.blocks} onCite={setCiteN} />
                {section.subsections?.map((sub) => (
                  <div key={sub.heading}>
                    <h3 className="wiki-sub">{sub.heading}</h3>
                    <ArticleBody blocks={sub.blocks} onCite={setCiteN} />
                  </div>
                ))}
              </section>
            ))}

            <ReferencesList references={article.references} highlighted={highlighted} />
          </>
        )}
      </main>

      {article && (
        <div className="wiki-discuss-bar">
          {onOpenTalk && (
            <button
              type="button"
              className="wiki-discuss-btn wiki-discuss-talk"
              onClick={() => onOpenTalk(articleId)}
            >
              <ChatIcon size={18} />
              Discussion
            </button>
          )}
          <button type="button" className="wiki-discuss-btn" onClick={() => setDiscussOpen(true)}>
            <ChatIcon size={18} />
            Discuss this article
          </button>
        </div>
      )}

      {citedRef && (
        <CitationCard
          reference={citedRef}
          onClose={() => setCiteN(null)}
          onJump={jumpToReference}
        />
      )}
      {discussOpen && article && (
        <DiscussSheet
          articleId={articleId}
          domains={
            article.sections.length
              ? [...new Set(article.sections.map((s) => s.domain))]
              : ["general"]
          }
          onClose={() => setDiscussOpen(false)}
        />
      )}
    </div>
  );
}
