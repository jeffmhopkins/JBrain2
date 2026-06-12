// The Sessions panel (left swipe from Full Brain): the capability records, newest
// first, and the new-session picker. A session's read scope is chosen up front
// and least-privilege by default — picking domains here sets the firewall the
// session's tools read under (docs/ASSISTANT.md "Session capabilities").

import { type ReactNode, useState } from "react";
import { Sheet } from "../components/Sheet";
import type { AgentSession, SessionCreate } from "./types";

const DOMAINS: { code: string; label: string; desc: string }[] = [
  { code: "general", label: "General", desc: "notes, lists, wiki" },
  { code: "health", label: "Health", desc: "labs, meds, medical notes" },
  { code: "finance", label: "Finance", desc: "statements, receipts" },
  { code: "location", label: "Location", desc: "places, geofences" },
];

interface Props {
  sessions: AgentSession[];
  onOpen: (session: AgentSession) => void;
  onCreate: (body: SessionCreate) => Promise<AgentSession>;
  onClose: () => void;
}

export function SessionsPanel({ sessions, onOpen, onCreate, onClose }: Props): ReactNode {
  const [picking, setPicking] = useState(false);
  const active = sessions.filter((s) => s.status === "active");
  const ended = sessions.filter((s) => s.status !== "active");

  return (
    <section className="panel-content" aria-label="Sessions">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to chat" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">Sessions</span>
        <span className="sub">read-scope chosen at start</span>
      </div>
      <div className="panel-body">
        <button type="button" className="row new-session" onClick={() => setPicking(true)}>
          ＋ New session — choose sources
        </button>

        {active.length > 0 && <div className="sect">Active</div>}
        {active.map((s) => (
          <SessionRow key={s.id} session={s} onOpen={onOpen} />
        ))}

        {ended.length > 0 && <div className="sect">Earlier</div>}
        {ended.map((s) => (
          <SessionRow key={s.id} session={s} onOpen={onOpen} />
        ))}

        {sessions.length === 0 && (
          <div className="panel-empty">No sessions yet — start one to ask about your brain.</div>
        )}
      </div>

      {picking && (
        <NewSessionSheet
          onClose={() => setPicking(false)}
          onCreate={async (body) => {
            const created = await onCreate(body);
            setPicking(false);
            onOpen(created);
          }}
        />
      )}
    </section>
  );
}

function SessionRow({
  session,
  onOpen,
}: {
  session: AgentSession;
  onOpen: (s: AgentSession) => void;
}): ReactNode {
  return (
    <button type="button" className="row session-row" onClick={() => onOpen(session)}>
      <div className="r-head">{session.title || "Untitled session"}</div>
      <div className="pills">
        {session.domain_scopes.map((d) => (
          <span key={d} className={`pill ${d}`}>
            {d}
          </span>
        ))}
        {session.domain_scopes.length === 0 && <span className="pill">all domains</span>}
      </div>
    </button>
  );
}

function NewSessionSheet({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (body: SessionCreate) => void | Promise<void>;
}): ReactNode {
  // Least-privilege default: general only. Widening is an explicit act.
  const [selected, setSelected] = useState<Set<string>>(new Set(["general"]));
  const [title, setTitle] = useState("");

  function toggle(code: string): void {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });
  }

  const chosen = DOMAINS.filter((d) => selected.has(d.code)).map((d) => d.code);

  return (
    <Sheet title="New session" onClose={onClose}>
      <p className="lead">
        Choose which knowledge sources this session may <b>read</b>. Narrow by default — widen only
        when you need to. This sets the session's firewall.
      </p>
      <input
        className="session-title"
        aria-label="Session title"
        placeholder="Title (optional)"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
      />
      <div className="domain-opts">
        {DOMAINS.map((d) => (
          <button
            type="button"
            key={d.code}
            className={`opt${selected.has(d.code) ? " on" : ""}`}
            aria-pressed={selected.has(d.code)}
            onClick={() => toggle(d.code)}
          >
            <span className="opt-t">{d.label}</span>
            <span className="opt-d">{d.desc}</span>
          </button>
        ))}
      </div>
      <button
        type="button"
        className="start"
        disabled={chosen.length === 0}
        onClick={() => void onCreate({ domain_scopes: chosen, title: title.trim() })}
      >
        Start session · reads {chosen.length ? chosen.join(" · ") : "nothing"}
      </button>
      <p className="writes-note">
        Reads only. Any change the agent wants is <b>staged as a Proposal</b> for your approval.
      </p>
    </Sheet>
  );
}
