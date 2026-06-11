// Entities browse (docs/DESIGN.md): established paradigms only — the
// search screen's live filter input (250ms debounce, sequence-guarded) over
// standard list rows in a card. Kind chips come from the loaded data, never
// a hardcoded set. Tapping a row opens the existing entity-page layer.

import { useEffect, useRef, useState } from "react";
import { fmtTemporal } from "../analysis/format";
import { type EntityList, type EntityListItem, api } from "../api/client";

type ListState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; items: EntityListItem[] };

interface EntityListScreenProps {
  onOpenEntity: (entityId: string) => void;
  /** Injectable for tests; defaults to the real client. */
  list?: (q?: string, kind?: string) => Promise<EntityList>;
}

const DEBOUNCE_MS = 250;

/** "3 facts · last seen Jun 10, 2026" — facts only when never reported.
 * last_seen is an instant (max reported_at), so it renders as a LOCAL
 * calendar day: fmtTemporal's instant branch, not the UTC day branch. */
function rowMeta(item: EntityListItem): string {
  const facts = `${item.fact_count} ${item.fact_count === 1 ? "fact" : "facts"}`;
  if (item.last_seen === null) return facts;
  return `${facts} · last seen ${fmtTemporal(item.last_seen, "instant")}`;
}

export function EntityListScreen({ onOpenEntity, list }: EntityListScreenProps) {
  const doList = list ?? ((q?: string, kind?: string) => api.listEntities(q, kind));
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string | null>(null);
  // The unfiltered load defines the chip set; filtered loads keep it stable
  // so narrowing to one kind doesn't collapse the chips to itself.
  const [kinds, setKinds] = useState<string[]>([]);
  const [state, setState] = useState<ListState>({ phase: "loading" });
  const seq = useRef(0);
  const doListRef = useRef(doList);
  doListRef.current = doList;

  useEffect(() => {
    const q = query.trim();
    const timer = setTimeout(
      async () => {
        const mine = ++seq.current;
        setState((prev) => (prev.phase === "done" ? prev : { phase: "loading" }));
        try {
          const out = await doListRef.current(q === "" ? undefined : q, kind ?? undefined);
          if (seq.current !== mine) return; // stale response — a newer query won
          setState({ phase: "done", items: out.items });
          if (q === "" && kind === null) {
            setKinds([...new Set(out.items.map((item) => item.kind))]);
          }
        } catch {
          if (seq.current === mine) setState({ phase: "error" });
        }
      },
      q === "" ? 0 : DEBOUNCE_MS,
    );
    return () => clearTimeout(timer);
  }, [query, kind]);

  const filtered = query.trim() !== "" || kind !== null;

  return (
    <main className="screen-body entity-list-screen">
      <div className="search-bar">
        <input
          type="search"
          aria-label="Filter entities"
          placeholder="filter by name…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {kinds.length > 0 && (
        <div className="filter-chips" aria-label="Kind filter">
          <button
            type="button"
            aria-pressed={kind === null}
            className={`filter-chip${kind === null ? " filter-chip-on" : ""}`}
            onClick={() => setKind(null)}
          >
            All
          </button>
          {kinds.map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={kind === k}
              className={`filter-chip${kind === k ? " filter-chip-on" : ""}`}
              onClick={() => setKind(kind === k ? null : k)}
            >
              {k}
            </button>
          ))}
        </div>
      )}

      {state.phase === "loading" && <p className="analysis-quiet">loading entities…</p>}
      {state.phase === "error" && (
        <p className="analysis-quiet">couldn't load entities — check the connection.</p>
      )}
      {state.phase === "done" && state.items.length === 0 && (
        <p className="analysis-quiet">
          {filtered
            ? "nothing matched — try a different name."
            : "no entities yet — they appear as notes are analyzed."}
        </p>
      )}

      {state.phase === "done" && state.items.length > 0 && (
        <div className="fact-card">
          {state.items.map((item) => (
            <button
              key={item.id}
              type="button"
              className="entity-row"
              onClick={() => onOpenEntity(item.id)}
            >
              <span className="entity-row-main">
                <span className="entity-row-name">
                  {item.canonical_name}
                  {item.status === "provisional" && (
                    <span className="fact-chip fact-chip-muted">provisional</span>
                  )}
                </span>
                <span className="entity-row-kind">{item.kind.toLowerCase()}</span>
              </span>
              <span className="entity-row-meta">{rowMeta(item)}</span>
            </button>
          ))}
        </div>
      )}
    </main>
  );
}
