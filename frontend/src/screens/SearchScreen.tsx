// Search (docs/DESIGN.md "Search"): live as-you-type (debounced) with
// passage-first result cards, retrieval-transparency match badges, domain
// filter chips, and the amber degraded banner while semantic search
// recovers. Stale responses are sequence-guarded so fast typing can't
// reorder results.

import { type FormEvent, useEffect, useRef, useState } from "react";
import { type SearchOut, type SearchResult, api } from "../api/client";
import { ClipIcon, SearchIcon } from "../components/icons";
import { dayLabel } from "../notes/grouping";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import { splitMarks } from "../search/marks";

const DOMAIN_CHIPS: { code: string | null; label: string }[] = [
  { code: null, label: "All" },
  { code: "general", label: "General" },
  { code: "health", label: "Medical" },
  { code: "finance", label: "Financial" },
  { code: "location", label: "Location" },
];

/** semantic / both ride the steel tint; keyword stays neutral surface-2. */
export function MatchBadge({ match }: { match: SearchResult["match"] }) {
  const steel = match === "semantic" || match === "both";
  return <span className={`match-badge${steel ? " match-steel" : ""}`}>{match}</span>;
}

function Snippet({ snippet }: { snippet: string }) {
  return (
    <p className="result-snippet">
      {splitMarks(snippet).map((seg, i) =>
        seg.marked ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: segments are static per snippet.
          <mark key={i} className="snip-mark">
            {seg.text}
          </mark>
        ) : (
          // biome-ignore lint/suspicious/noArrayIndexKey: segments are static per snippet.
          <span key={i}>{seg.text}</span>
        ),
      )}
    </p>
  );
}

type SearchState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "done"; query: string; out: SearchOut }
  | { phase: "error" };

interface SearchScreenProps {
  onOpenResult: (result: SearchResult) => void;
  /** Injectable for tests; defaults to the real client. */
  search?: (q: string, domain?: string) => Promise<SearchOut>;
}

const DEBOUNCE_MS = 250;

export function SearchScreen({ onOpenResult, search }: SearchScreenProps) {
  const doSearch = search ?? ((q: string, domain?: string) => api.search(q, domain));
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState<string | null>(null);
  const [state, setState] = useState<SearchState>({ phase: "idle" });
  const seq = useRef(0);

  async function run(q: string, dom: string | null) {
    const mine = ++seq.current;
    if (q === "") {
      setState({ phase: "idle" });
      return;
    }
    setState((prev) => (prev.phase === "done" ? prev : { phase: "loading" }));
    try {
      const out = await doSearch(q, dom ?? undefined);
      if (seq.current === mine) setState({ phase: "done", query: q, out });
    } catch {
      if (seq.current === mine) setState({ phase: "error" });
    }
  }

  // Live search: every keystroke (and chip change) re-queries after a short
  // debounce; results keep showing while the next response is in flight.
  useEffect(() => {
    const q = query.trim();
    const timer = setTimeout(() => void run(q, domain), q === "" ? 0 : DEBOUNCE_MS);
    return () => clearTimeout(timer);
    // biome-ignore lint/correctness/useExhaustiveDependencies: run is stable per render intent.
  }, [query, domain]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    void run(query.trim(), domain);
  }

  return (
    <main className="screen-body search-screen">
      <form className="search-bar" onSubmit={(e) => void submit(e)}>
        <input
          type="search"
          aria-label="Search query"
          placeholder="search your notes…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" className="search-submit" aria-label="Search">
          <SearchIcon size={20} />
          Search
        </button>
      </form>

      <div className="filter-chips" aria-label="Domain filter">
        {DOMAIN_CHIPS.map((chip) => (
          <button
            key={chip.label}
            type="button"
            aria-pressed={domain === chip.code}
            className={`filter-chip${domain === chip.code ? " filter-chip-on" : ""}`}
            onClick={() => setDomain(chip.code)}
          >
            {chip.label}
          </button>
        ))}
      </div>

      {state.phase === "idle" && <p className="search-empty">search by meaning or keywords</p>}
      {state.phase === "loading" && <p className="search-empty">searching…</p>}
      {state.phase === "error" && (
        <p className="search-empty">search failed — check the connection and try again.</p>
      )}

      {state.phase === "done" && (
        <>
          {state.out.degraded && (
            <output className="degraded-banner">
              keyword-only results — semantic search recovering…
            </output>
          )}
          {state.out.results.length === 0 && (
            <p className="search-empty">nothing matched “{state.query}” — try different words.</p>
          )}
          {state.out.results.map((result) => (
            <button
              key={result.chunk_id}
              type="button"
              className="result-card"
              onClick={() => onOpenResult(result)}
            >
              <span className="result-head">
                <span
                  className="domain-dot"
                  style={{ background: DOMAIN_COLOR[result.domain] ?? "var(--steel)" }}
                  title={DOMAIN_TITLE[result.domain] ?? result.domain}
                />
                <span className="result-date">
                  {dayLabel(new Date(result.created_at))}
                  {result.destination ? ` · ${result.destination}` : ""}
                </span>
                <MatchBadge match={result.match} />
              </span>
              <Snippet snippet={result.snippet} />
              <span className="result-context">
                <span className="result-preview">{result.body_preview}</span>
                {result.attachment_count > 0 && (
                  <span className="result-attachments">
                    <ClipIcon size={13} /> {result.attachment_count}
                  </span>
                )}
                {result.source_anchor && (
                  <span className="result-anchor">{result.source_anchor}</span>
                )}
              </span>
            </button>
          ))}
        </>
      )}
    </main>
  );
}
