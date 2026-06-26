// The share-link entry: when the PWA is opened at /jcode/s/{sid}#t=token (a copied
// share link), this — not the full owner app — mounts. It redeems the secret for a
// session cookie scoped to that ONE session, then shows just that session screen. A
// share recipient has no access to the rest of the app (every owner route 403s their
// cookie), so there is deliberately no home, launcher, or nav here.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { parseShareLink } from "../jcode/share";
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
    const link = parseShareLink();
    if (!link) {
      setState({ status: "error" });
      return;
    }
    // Strip the secret from the address bar at once — it shouldn't linger in history
    // or get re-shared by copying the URL after it's been redeemed.
    window.history.replaceState(null, "", `/jcode/s/${link.sid}`);
    let stale = false;
    void (async () => {
      try {
        await api.jcodeRedeemShare(link.token);
        const session = await api.jcodeGetSession(link.sid);
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
