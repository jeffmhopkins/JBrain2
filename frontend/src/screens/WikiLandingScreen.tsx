// The wiki landing (Phase 6, Wave B2a — docs/mocks/wiki-landing-a-search-rails.html):
// a living, search-first home over the machine-written article set. A search box
// (filters the rails as you type), an amber read-only line, then three derived
// rails — Recently updated (horizontal cards), Most connected (hubs, post-RLS link
// counts), and a collapsible Browse-by-type index. Taxonomy is derived (entity type
// + centrality + recency), never hand-maintained. Read-only on fixtures; tapping any
// entry opens the reader. Mirrors the screen-body idiom of SearchScreen/EntityList.

import { useEffect, useMemo, useState } from "react";
import {
  type WikiHubEntry,
  type WikiLandingEntry,
  type WikiLandingOut,
  type WikiRecentEntry,
  api,
} from "../api/client";
import {
  ChevronRightIcon,
  EyeOffIcon,
  GraphIcon,
  ListIcon,
  RefreshIcon,
  SearchIcon,
} from "../components/icons";
import { EntityTypeIcon } from "../entities/kinds";

type LandingState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; landing: WikiLandingOut };

interface WikiLandingScreenProps {
  onOpenArticle: (articleId: string) => void;
  /** Injectable for tests; defaults to the real client. */
  load?: () => Promise<WikiLandingOut>;
}

/** Case-insensitive match over an entry's title + blurb. */
function entryMatches(entry: WikiLandingEntry, q: string): boolean {
  if (q === "") return true;
  const hay = `${entry.title} ${entry.blurb}`.toLowerCase();
  return hay.includes(q);
}

export function WikiLandingScreen({ onOpenArticle, load }: WikiLandingScreenProps) {
  const doLoad = load ?? (() => api.getWikiLanding());
  const [state, setState] = useState<LandingState>({ phase: "loading" });
  const [query, setQuery] = useState("");
  // Type groups collapse independently; a search opens them all so matches show.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // biome-ignore lint/correctness/useExhaustiveDependencies: load once on mount; doLoad is stable for the screen's life (real client or a test stub).
  useEffect(() => {
    let stale = false;
    setState({ phase: "loading" });
    doLoad()
      .then((landing) => {
        if (!stale) setState({ phase: "done", landing });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, []);

  const q = query.trim().toLowerCase();
  const landing = state.phase === "done" ? state.landing : null;

  // Filtering the rails as you type keeps search in-place (the full hybrid search
  // lives in the Search screen); empty query shows everything.
  const filtered = useMemo(() => {
    if (!landing) return null;
    return {
      recent: landing.recent.filter((e) => entryMatches(e, q)),
      hubs: landing.hubs.filter((e) => entryMatches(e, q)),
      groups: landing.groups
        .map((g) => ({ ...g, entries: g.entries.filter((e) => entryMatches(e, q)) }))
        .filter((g) => g.entries.length > 0),
    };
  }, [landing, q]);

  function toggleGroup(type: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  // While searching, every group is forced open so matches are never hidden.
  const searching = q !== "";
  const noMatches =
    filtered !== null &&
    filtered.recent.length === 0 &&
    filtered.hubs.length === 0 &&
    filtered.groups.length === 0;

  return (
    <main className="screen-body wiki-landing">
      <div className="wiki-search-box">
        <SearchIcon size={18} />
        <input
          type="search"
          aria-label="Search the wiki"
          placeholder="Search the wiki…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="wiki-ro wiki-ro-landing">
        <EyeOffIcon size={13} />
        Machine-written from your notes · read-only
      </div>

      {state.phase === "loading" && <p className="analysis-quiet">loading the wiki…</p>}
      {state.phase === "error" && (
        <p className="analysis-quiet">couldn't load the wiki — reopen to retry.</p>
      )}

      {filtered && (
        <>
          {noMatches && (
            <p className="search-empty">nothing matched “{query.trim()}” — try different words.</p>
          )}

          {filtered.recent.length > 0 && (
            <>
              <div className="wiki-railhead">
                <RefreshIcon size={14} />
                Recently updated
              </div>
              <div className="wiki-hrail">
                {filtered.recent.map((entry) => (
                  <RecentCard key={entry.id} entry={entry} onOpen={onOpenArticle} />
                ))}
              </div>
            </>
          )}

          {filtered.hubs.length > 0 && (
            <>
              <div className="wiki-railhead">
                <GraphIcon size={14} />
                Most connected
              </div>
              {filtered.hubs.map((entry) => (
                <HubRow key={entry.id} entry={entry} onOpen={onOpenArticle} />
              ))}
            </>
          )}

          {filtered.groups.length > 0 && (
            <>
              <div className="wiki-railhead">
                <ListIcon size={14} />
                Browse by type
              </div>
              {filtered.groups.map((group) => {
                const open = searching || !collapsed.has(group.type);
                return (
                  <section className={`wiki-grp${open ? "" : " wiki-grp-closed"}`} key={group.type}>
                    <button
                      type="button"
                      className="wiki-grphead"
                      aria-expanded={open}
                      aria-label={`${group.type}, ${group.entries.length} articles`}
                      onClick={() => toggleGroup(group.type)}
                    >
                      <ChevronRightIcon size={16} />
                      <span className="wiki-gt">{group.type}</span>
                      <span className="wiki-gc">{group.entries.length}</span>
                    </button>
                    {open && (
                      <div className="wiki-grp-rows">
                        {group.entries.map((entry) => (
                          <IndexRow key={entry.id} entry={entry} onOpen={onOpenArticle} />
                        ))}
                      </div>
                    )}
                  </section>
                );
              })}
            </>
          )}
        </>
      )}
    </main>
  );
}

function RecentCard({ entry, onOpen }: { entry: WikiRecentEntry; onOpen: (id: string) => void }) {
  return (
    <button type="button" className="wiki-ucard" onClick={() => onOpen(entry.id)}>
      <EntityTypeIcon kind={entry.kind} size={30} />
      <span className="wiki-ucard-t">{entry.title}</span>
      <span className="wiki-ucard-when">{entry.when}</span>
    </button>
  );
}

function HubRow({ entry, onOpen }: { entry: WikiHubEntry; onOpen: (id: string) => void }) {
  return (
    <button type="button" className="wiki-lrow" onClick={() => onOpen(entry.id)}>
      <EntityTypeIcon kind={entry.kind} size={34} />
      <span className="wiki-lrow-tx">
        <span className="wiki-lrow-t">{entry.title}</span>
        <span className="wiki-lrow-blurb">{entry.blurb}</span>
      </span>
      <span className="wiki-lrow-cnt" aria-label={`${entry.links} links`}>
        <GraphIcon size={12} />
        {entry.links}
      </span>
    </button>
  );
}

function IndexRow({ entry, onOpen }: { entry: WikiLandingEntry; onOpen: (id: string) => void }) {
  return (
    <button type="button" className="wiki-lrow" onClick={() => onOpen(entry.id)}>
      <EntityTypeIcon kind={entry.kind} size={34} />
      <span className="wiki-lrow-tx">
        <span className="wiki-lrow-t">{entry.title}</span>
        <span className="wiki-lrow-blurb">{entry.blurb}</span>
      </span>
    </button>
  );
}
