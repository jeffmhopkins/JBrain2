import { useEffect, useRef, useState } from "react";
import { ApiError, type PanelLookResult, type PanelLookWhat, api } from "../api/client";

// A look takes the panel's 3 s pickup plus its answer: the box gives it 60 s to collect and
// 120 s to answer, so ten 20 s polls outlast both and a panel that never answers ends as
// "expired" from the box rather than as a spinner the owner has to give up on.
const WAIT_S = 20;
const MAX_POLLS = 10;

type Look =
  | { phase: "idle" }
  | { phase: "asking"; what: PanelLookWhat }
  | { phase: "done"; what: PanelLookWhat; out: PanelLookResult }
  | { phase: "failed"; message: string };

// The refusals arrive as `{detail: {reason}}`, which the client reduces to a status; the status
// alone says which one it was, in the owner's words rather than the relay's.
function refusal(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 503) return "The panel isn't picking up right now.";
    if (err.status === 409) return "This panel's firmware can't do that.";
    if (err.status === 429) return "The panel already has asks waiting; try again shortly.";
    return err.message;
  }
  return "Couldn't reach the box.";
}

/** Snapshot and Screenshot for one C6 on Ops → Panels: what the debug console asks with a
 *  token, asked with the owner's own session, so looking at the panel needs no terminal
 *  (CLAUDE.md #10). Read-only on purpose; a reboot or an injected tap stays with the token. */
export function PanelLook({
  deviceId,
  name,
  online,
}: {
  deviceId: string;
  name: string;
  online: boolean;
}) {
  const [look, setLook] = useState<Look>({ phase: "idle" });
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function ask(what: PanelLookWhat) {
    setLook({ phase: "asking", what });
    try {
      const { request_id } = await api.panelLook(deviceId, what);
      for (let i = 0; i < MAX_POLLS && alive.current; i++) {
        const out = await api.panelLookResult(deviceId, request_id, WAIT_S);
        if (out.state === "done" || out.state === "expired") {
          if (alive.current) setLook({ phase: "done", what, out });
          return;
        }
      }
      if (alive.current) setLook({ phase: "failed", message: "The panel didn't answer." });
    } catch (err) {
      if (alive.current) setLook({ phase: "failed", message: refusal(err) });
    }
  }

  const busy = look.phase === "asking";
  return (
    <div className="ops-panel-look">
      <div className="ops-panel-look-actions">
        <button type="button" disabled={busy || !online} onClick={() => void ask("snapshot")}>
          Snapshot
        </button>
        <button type="button" disabled={busy || !online} onClick={() => void ask("screenshot")}>
          Screenshot
        </button>
        {!online && <span className="ops-panel-note">Not picking up right now.</span>}
        {busy && <output className="ops-panel-note">Asking the panel…</output>}
      </div>
      {look.phase === "failed" && (
        <p className="ops-panel-concern" role="alert">
          {look.message}
        </p>
      )}
      {look.phase === "done" && <LookOut name={name} out={look.out} />}
    </div>
  );
}

function LookOut({ name, out }: { name: string; out: PanelLookResult }) {
  if (out.state === "expired") {
    return <p className="ops-panel-concern">The panel didn't answer in time.</p>;
  }
  if (out.result?.ok === false) {
    return (
      <p className="ops-panel-concern">
        The panel couldn't: {out.result.err || "no reason given"}.
      </p>
    );
  }
  if (out.screenshot) {
    return <img className="ops-panel-shot" src={out.screenshot} alt={`Screenshot of ${name}`} />;
  }
  return (
    <pre className="ops-panel-snapshot" aria-label={`Snapshot of ${name}`}>
      {JSON.stringify(out.result?.data ?? out.result, null, 2)}
    </pre>
  );
}
