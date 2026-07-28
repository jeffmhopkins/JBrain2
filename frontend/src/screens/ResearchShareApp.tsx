// The public report-share entry: when the PWA is opened at /share/{token} (a copied share
// link), this — not the full owner app — mounts. It fetches the shared report (or a folder
// index) straight from the public /api/research-share endpoint; there is no login, no owner
// nav, no shell. A report renders through the SAME <ReportDetailBody> the owner's detail layer
// uses, so a shared report reads identically. Chosen GUI: docs/mocks/research-share (variant B).

import { useEffect, useState } from "react";
import { type ReportDetail, type ShareReportItem, api } from "../api/client";
import { parseResearchSharePath } from "../research/share";
import { ReportDetailBody } from "./ResearchDetailScreen";

type State =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "report"; report: ReportDetail }
  | {
      phase: "index";
      label: string | null;
      items: ShareReportItem[];
      open: ReportDetail | null;
      // A transient "couldn't open that one" notice — a failed member fetch must NOT discard
      // the loaded folder index.
      notice: string | null;
    };

export function ResearchShareApp() {
  const [state, setState] = useState<State>({ phase: "loading" });
  const token = parseResearchSharePath();

  useEffect(() => {
    if (!token) {
      setState({ phase: "error" });
      return;
    }
    let stale = false;
    void (async () => {
      try {
        const view = await api.researchShareView(token);
        if (stale) return;
        if (view.kind === "report" && view.report) {
          setState({ phase: "report", report: view.report });
        } else {
          setState({
            phase: "index",
            label: view.label,
            items: view.reports ?? [],
            open: null,
            notice: null,
          });
        }
      } catch {
        if (!stale) setState({ phase: "error" });
      }
    })();
    return () => {
      stale = true;
    };
  }, [token]);

  async function openMember(id: string): Promise<void> {
    if (!token || state.phase !== "index") return;
    try {
      const report = await api.researchShareReport(token, id);
      setState((s) => (s.phase === "index" ? { ...s, open: report, notice: null } : s));
    } catch {
      // Keep the index — a transient per-report failure shouldn't strand the whole folder.
      setState((s) =>
        s.phase === "index" ? { ...s, notice: "Couldn't open that report — try again." } : s,
      );
    }
  }
  function backToIndex(): void {
    setState((s) => (s.phase === "index" ? { ...s, open: null } : s));
  }

  if (state.phase === "loading") {
    return (
      <main className="rs-shell">
        <p className="rs-empty" aria-live="polite">
          Loading…
        </p>
      </main>
    );
  }
  if (state.phase === "error") {
    return (
      <main className="rs-shell">
        <ShareBand />
        <p className="rs-empty">This share link is invalid or has been revoked.</p>
      </main>
    );
  }

  // A single-report link.
  if (state.phase === "report") {
    return (
      <main className="rs-shell">
        <ShareBand />
        <div className="rs-body">
          <ReportDetailBody report={state.report} />
        </div>
        <ShareFoot />
      </main>
    );
  }

  // A folder link with a member opened — a back row returns to the index.
  if (state.open) {
    return (
      <main className="rs-shell">
        <ShareBand label={state.label} onBack={backToIndex} />
        <div className="rs-body">
          <ReportDetailBody report={state.open} />
        </div>
        <ShareFoot />
      </main>
    );
  }

  // The folder index (a folder link, no member opened yet).
  return (
    <main className="rs-shell">
      <ShareBand label={state.label} folder />
      <div className="rs-index">
        <h1 className="rs-index-ttl">{state.label ?? "Shared reports"}</h1>
        <p className="rs-index-cnt">
          {state.items.length} report{state.items.length === 1 ? "" : "s"}
        </p>
        {state.notice && (
          <p className="rs-notice" aria-live="polite">
            {state.notice}
          </p>
        )}
        {state.items.map((item) => (
          <button
            key={item.id}
            type="button"
            className="rs-card"
            onClick={() => void openMember(item.id)}
          >
            <span className="rs-card-q">{item.title || item.question}</span>
          </button>
        ))}
      </div>
      <ShareFoot />
    </main>
  );
}

/** The amber "shared research · read-only" context band (DESIGN.md amber = research/read-only). */
function ShareBand({
  label,
  folder,
  onBack,
}: { label?: string | null; folder?: boolean; onBack?: () => void }) {
  return (
    <div className="rs-band">
      {onBack ? (
        <button type="button" className="rs-band-back" onClick={onBack}>
          ← {label ?? "Back"}
        </button>
      ) : (
        <span className="rs-band-kick">
          {folder ? "Shared folder" : "Shared research"} · read-only
        </span>
      )}
      <span className="rs-wordmark">
        JBrain<span className="rs-dot">.</span>
      </span>
    </div>
  );
}

function ShareFoot() {
  return <div className="rs-foot">Read-only · shared from a personal JBrain research library.</div>;
}
