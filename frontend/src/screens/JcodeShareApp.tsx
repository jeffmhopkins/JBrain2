// The share-link entry: when the PWA is opened at /jcode/s/{sid}#t=token (a copied
// share link), this — not the full owner app — mounts. It redeems the secret for a
// session cookie scoped to that ONE session, then shows just that session screen. A
// share recipient has no access to the rest of the app (every owner route 403s their
// cookie), so there is deliberately no home, launcher, or nav here.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { parseShareLink, parseSharePath } from "../jcode/share";
import type { JcodeSession } from "../jcode/types";
import { JcodeSessionScreen } from "./JcodeSessionScreen";

type State =
  | { status: "loading" }
  | { status: "ready"; session: JcodeSession }
  | { status: "closed" }
  | { status: "error" };

export function JcodeShareApp() {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const sid = parseSharePath();
    if (!sid) {
      setState({ status: "error" });
      return;
    }
    const link = parseShareLink();
    // Strip the secret from the address bar at once — it shouldn't linger in history
    // or get re-shared by copying the URL after it's been redeemed.
    if (link) window.history.replaceState(null, "", `/jcode/s/${sid}`);
    let stale = false;
    void (async () => {
      try {
        // Redeem only when a secret is present (the first open). On a reload — or a
        // re-open of an already-claimed link — there's no secret (or the redeem 401s
        // because it's single-use); either way we fall through to the existing scoped
        // cookie, so a bound browser keeps its access instead of erroring.
        if (link) {
          try {
            await api.jcodeRedeemShare(link.token);
          } catch {
            // Already claimed by this browser (single-use): the cookie below still works.
          }
        }
        const session = await api.jcodeGetSession(sid);
        if (!stale) setState({ status: "ready", session });
      } catch {
        if (!stale) setState({ status: "error" });
      }
    })();
    return () => {
      stale = true;
    };
  }, []);

  if (state.status === "ready") {
    return (
      <JcodeSessionScreen
        session={state.session}
        shared
        onClose={() => setState({ status: "closed" })}
      />
    );
  }

  return (
    <div className="jcode-shareboot">
      <p className="jcode-empty">
        {state.status === "loading"
          ? "Opening shared session…"
          : state.status === "closed"
            ? "Shared session closed — reopen it from the original link."
            : "This share link is invalid or has expired."}
      </p>
    </div>
  );
}
