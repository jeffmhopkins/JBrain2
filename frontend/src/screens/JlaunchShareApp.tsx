// The public results-share entry: when the PWA is opened at /results/{token} (a copied
// sharelink), this — not the full owner app — mounts. It fetches the shared run's public
// results straight from /api/jlaunch-share/{token}; there is no login, no owner nav, no
// shell. Renders the headline block + machine specs + an artifact download. Matches the
// chosen GUI-gate variant's Result styling (docs/mocks/jlaunch/).

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { jlaunchArtifactUrl, parseJlaunchSharePath } from "../jlaunch/share";
import type { PublicJlaunchRun } from "../jlaunch/types";

type State = { phase: "loading" } | { phase: "error" } | { phase: "ready"; run: PublicJlaunchRun };

export function JlaunchShareApp() {
  const [state, setState] = useState<State>({ phase: "loading" });
  const token = parseJlaunchSharePath();

  useEffect(() => {
    if (!token) {
      setState({ phase: "error" });
      return;
    }
    let stale = false;
    void (async () => {
      try {
        const run = await api.jlaunchShareView(token);
        if (!stale) setState({ phase: "ready", run });
      } catch {
        if (!stale) setState({ phase: "error" });
      }
    })();
    return () => {
      stale = true;
    };
  }, [token]);

  if (state.phase === "loading") {
    return <div className="jl-share-shell jl-share-msg">Loading results…</div>;
  }
  if (state.phase === "error") {
    return (
      <div className="jl-share-shell jl-share-msg">
        This results link is unknown or has been revoked.
      </div>
    );
  }

  const { run } = state;
  return (
    <div className="jl-share-shell">
      <div className="jl-share-band">
        <span className="jl-share-brand">
          JBrain<span className="jl-dot">.</span>
        </span>
        <span className="jl-share-kicker">shared computation result</span>
      </div>

      <h1 className="jl-share-title">{run.title}</h1>
      <p className="jl-share-repo">{run.repo}</p>

      <div className="jl-card">
        <h3 className="jl-h3">Headline{run.verify_ok ? " · verified OK" : ""}</h3>
        <pre className="jl-headline">{run.headline.join("\n") || "(no headline captured)"}</pre>
      </div>

      <div className="jl-card">
        <h3 className="jl-h3">Machine &amp; timing</h3>
        <div className="jl-specs">
          {machineChips(run.machine).map((c) => (
            <span key={c} className="jl-chip">
              {c}
            </span>
          ))}
        </div>
        {run.phase_times.length > 0 && (
          <div className="jl-times">
            {run.phase_times.map((p) => (
              <div key={p.phase} className="jl-time">
                <span>{p.phase}</span>
                <span>{fmtSecs(p.seconds)}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {run.has_artifact && token && (
        <a className="jl-download" href={jlaunchArtifactUrl(token)}>
          Download {run.artifact_name}
          {run.artifact_bytes != null ? ` · ${fmtBytes(run.artifact_bytes)}` : ""}
        </a>
      )}

      <p className="jl-share-foot">Computed on the owner's own server via JBrain's job launcher.</p>
    </div>
  );
}

function machineChips(machine: Record<string, unknown>): string[] {
  const out: string[] = [];
  if (typeof machine.cpu === "string") out.push(machine.cpu);
  if (typeof machine.cores === "number") out.push(`${machine.cores} cores`);
  if (typeof machine.mem_bytes === "number") out.push(`${fmtBytes(machine.mem_bytes)} RAM`);
  return out;
}

function fmtSecs(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function fmtBytes(b: number): string {
  if (b >= 1024 ** 3) return `${(b / 1024 ** 3).toFixed(1)} GB`;
  if (b >= 1024 ** 2) return `${Math.round(b / 1024 ** 2)} MB`;
  return `${Math.round(b / 1024)} KB`;
}
