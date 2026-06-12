// Lists home (docs/mocks/lists-home-card.html): the owner's lists as cards with
// a domain dot, item previews, and progress. Tapping a whole card opens its
// checklist (the detail layer). A "＋ New list" inline creator picks a title and
// domain. Lists are the owner's own data — every action writes directly.

import { useEffect, useState } from "react";
import { type ListOut, api } from "../api/client";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";

const DOMAINS = ["general", "health", "finance", "location"];

type State = { phase: "loading" } | { phase: "error" } | { phase: "done"; lists: ListOut[] };

export interface ListsDeps {
  lists: () => Promise<ListOut[]>;
  createList: (title: string, domain: string) => Promise<ListOut>;
}

interface ListsScreenProps {
  onOpenList: (listId: string) => void;
  /** Injectable for tests; defaults to the live API client. */
  deps?: ListsDeps;
}

const openCount = (l: ListOut): number => l.items.filter((i) => !i.checked).length;
const pct = (l: ListOut): number =>
  l.items.length === 0 ? 0 : Math.round((100 * (l.items.length - openCount(l))) / l.items.length);

export function ListsScreen({ onOpenList, deps }: ListsScreenProps) {
  const load = deps?.lists ?? api.lists;
  const create = deps?.createList ?? api.createList;
  const [state, setState] = useState<State>({ phase: "loading" });
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [domain, setDomain] = useState("general");

  useEffect(() => {
    let stale = false;
    load()
      .then((lists) => {
        if (!stale) setState({ phase: "done", lists });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [load]);

  async function submit(): Promise<void> {
    const t = title.trim();
    if (!t) return;
    try {
      const made = await create(t, domain);
      setState((s) => (s.phase === "done" ? { phase: "done", lists: [made, ...s.lists] } : s));
      setTitle("");
      setDomain("general");
      setCreating(false);
      onOpenList(made.id);
    } catch {
      /* a failed create just leaves the form open to retry */
    }
  }

  return (
    <main className="screen-body lists-screen">
      {creating ? (
        <div className="list-create">
          <input
            // biome-ignore lint/a11y/noAutofocus: a deliberately-summoned inline form
            autoFocus
            aria-label="New list title"
            placeholder="list title…"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
              if (e.key === "Escape") setCreating(false);
            }}
          />
          <div className="list-domains" aria-label="Domain">
            {DOMAINS.map((d) => (
              <button
                key={d}
                type="button"
                aria-pressed={domain === d}
                className={`domain-pick${domain === d ? " on" : ""}`}
                onClick={() => setDomain(d)}
              >
                <span className="dot" style={{ background: DOMAIN_COLOR[d] ?? "var(--steel)" }} />
                {DOMAIN_TITLE[d] ?? d}
              </button>
            ))}
          </div>
          <div className="list-create-actions">
            <button type="button" className="ghost" onClick={() => setCreating(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              disabled={!title.trim()}
              onClick={() => void submit()}
            >
              Create
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="list-new" onClick={() => setCreating(true)}>
          ＋ New list
        </button>
      )}

      {state.phase === "loading" && <p className="analysis-quiet">loading lists…</p>}
      {state.phase === "error" && (
        <p className="analysis-quiet">couldn't load lists — check the connection.</p>
      )}
      {state.phase === "done" && state.lists.length === 0 && !creating && (
        <p className="analysis-quiet">no lists yet — make one, or ask Full Brain to.</p>
      )}

      {state.phase === "done" && state.lists.length > 0 && (
        <div className="list-grid">
          {state.lists.map((l) => (
            <button key={l.id} type="button" className="list-card" onClick={() => onOpenList(l.id)}>
              <span className="lc-head">
                <span
                  className="dot"
                  style={{ background: DOMAIN_COLOR[l.domain] ?? "var(--steel)" }}
                />
                <span className="lc-name">{l.title}</span>
              </span>
              <span className="lc-prev">
                {l.items.slice(0, 3).map((i) => (
                  <span key={i.id} className={i.checked ? "done" : ""}>
                    {i.body}
                  </span>
                ))}
                {l.items.length > 3 && <span className="more">+{l.items.length - 3} more</span>}
                {l.items.length === 0 && <span className="more">empty</span>}
              </span>
              <span className="lc-bar">
                <i style={{ width: `${pct(l)}%` }} />
              </span>
              <span className="lc-count">
                {openCount(l)}/{l.items.length} open
              </span>
            </button>
          ))}
        </div>
      )}
    </main>
  );
}
