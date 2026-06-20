// The family member's dashboard (JBrain360 M4d) — a standalone, location-only
// surface served at /dash and loaded inside the forked app's WebView. The device
// key lives in the Android Keystore and is exchanged for the session cookie
// natively (POST /api/session/mint), so this app never holds it: it probes the
// cookie's principal, and a member (device-key) session unlocks the Devices /
// Timeline / Map tabs scoped to its own + its family group.
//
// M4d-2a ships the shell, the session gate, and the Devices (presence) tab;
// Timeline and Map land in 2b/2c. Location domain stays on --location (teal).

import { useEffect, useState } from "react";
import { type MemberSubject, type Principal, api } from "../api/client";

type Tab = "devices" | "timeline" | "map";

const TAB_LABEL: Record<Tab, string> = {
  devices: "Devices",
  timeline: "Timeline",
  map: "Map",
};

export interface MemberDeps {
  /** Resolve the session cookie's principal; rejects (401) when unauthenticated. */
  probe: () => Promise<Principal>;
  listRoster: () => Promise<MemberSubject[]>;
}

type Gate = { phase: "probing" } | { phase: "locked" } | { phase: "ready"; label: string };

interface MemberDashboardProps {
  /** Injectable for tests; defaults to the live API client. */
  deps?: MemberDeps;
}

export function MemberDashboard({ deps }: MemberDashboardProps) {
  const probe = deps?.probe ?? api.me;
  const [gate, setGate] = useState<Gate>({ phase: "probing" });
  const [tab, setTab] = useState<Tab>("devices");

  useEffect(() => {
    let stale = false;
    probe()
      .then((p) => {
        if (stale) return;
        // Only a device-key cookie is a member session; an owner (or anything
        // else) belongs on the main app, not here.
        setGate(p.kind === "device_key" ? { phase: "ready", label: p.label } : { phase: "locked" });
      })
      .catch(() => {
        if (!stale) setGate({ phase: "locked" });
      });
    return () => {
      stale = true;
    };
  }, [probe]);

  if (gate.phase === "probing") {
    return <main className="dash-frame dash-center dash-quiet">checking your session…</main>;
  }
  if (gate.phase === "locked") {
    return (
      <main className="dash-frame dash-center dash-quiet">
        not signed in — open JBrain360 from the app to view the family map.
      </main>
    );
  }

  return (
    <main className="dash-frame">
      <header className="dash-head">
        <span className="dash-title">JBrain360</span>
        <span className="dash-quiet">{gate.label}</span>
      </header>
      <div className="seg-row" role="tablist" aria-label="Dashboard views">
        {(Object.keys(TAB_LABEL) as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={`seg${tab === t ? " seg-on" : ""}`}
            onClick={() => setTab(t)}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {tab === "devices" && <DevicesTab deps={deps} />}
      {tab === "timeline" && <p className="dash-quiet dash-pad">timeline arrives in M4d-2b.</p>}
      {tab === "map" && <p className="dash-quiet dash-pad">map arrives in M4d-2c.</p>}
    </main>
  );
}

// --- Devices (presence) tab -----------------------------------------------

type State = { phase: "loading" } | { phase: "error" } | { phase: "done"; roster: MemberSubject[] };

function DevicesTab({ deps }: { deps: MemberDeps | undefined }) {
  const list = deps?.listRoster ?? api.memberRoster;
  const [state, setState] = useState<State>({ phase: "loading" });

  useEffect(() => {
    let stale = false;
    list()
      .then((roster) => {
        if (!stale) setState({ phase: "done", roster });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [list]);

  if (state.phase === "loading") {
    return <p className="dash-quiet dash-pad">loading…</p>;
  }
  if (state.phase === "error") {
    return <p className="dash-quiet dash-pad">couldn't load the roster — check the connection.</p>;
  }
  if (state.roster.length === 0) {
    return <p className="dash-quiet dash-pad">no one to show yet.</p>;
  }
  return (
    <div className="loc-card-list">
      {state.roster.map((m) => (
        <article key={m.subject_id} className="loc-card">
          <div className="loc-card-head">{m.label}</div>
          <div className="loc-card-meta">
            <span>{lastSeen(m.last_seen)}</span>
            {m.battery_pct !== null && <span>· {m.battery_pct}%</span>}
            {m.connection && <span>· {m.connection}</span>}
          </div>
        </article>
      ))}
    </div>
  );
}

/** A compact "last fix" relative time for the roster (never an exact position —
 * just freshness, so a stale dot is never read as "here now"). */
export function lastSeen(iso: string | null): string {
  if (!iso) return "no fixes yet";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
