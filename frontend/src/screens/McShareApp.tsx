// What opening /mc/s/{token} mounts instead of the owner app: the owner's map page over
// the public, token-keyed routes, with no way back into JBrain, no world picker and nothing
// that changes the server. The token is the whole credential; a revoked one (or a world
// that's gone) reads as an ended link.

import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, type MinecraftShareView, type MinecraftSlot, api } from "../api/client";
import { EyeIcon } from "../components/icons";
import { parseMcSharePath, shareMap } from "../minecraftMapSource";
import { MapPage } from "./MinecraftMap";

/** A shared world as the map page's slot record: only what the page reads is real. */
function sharedSlot(v: MinecraftShareView): MinecraftSlot {
  return {
    id: v.slot,
    folder: "",
    name: v.name,
    exists: true,
    active: v.active,
    seed: null,
    gamemode: "",
    difficulty: "",
    cheats: false,
    origin: null,
    bytes: 0,
    last_played: v.last_played,
    backups: 0,
    last_backup: null,
    last_download: null,
  };
}

type ShareState =
  | { phase: "loading" }
  | { phase: "ended" }
  | { phase: "unreachable" }
  | { phase: "ready"; view: MinecraftShareView };

/** The whole app at /mc/s/{token}. */
export function McShareApp() {
  const token = parseMcSharePath() ?? "";
  const [state, setState] = useState<ShareState>({ phase: "loading" });
  const [nowMs, setNowMs] = useState(Date.now);
  const root = useRef<HTMLDivElement>(null);
  const source = useMemo(() => shareMap(token), [token]);

  useEffect(() => {
    let gone = false;
    api
      .mcShareView(token)
      .then((view) => {
        if (!gone) setState({ phase: "ready", view });
      })
      .catch((err) => {
        if (gone) return;
        setState({
          phase: err instanceof ApiError && err.status === 404 ? "ended" : "unreachable",
        });
      });
    return () => {
      gone = true;
    };
  }, [token]);

  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  if (state.phase !== "ready") {
    return (
      <main className="mc-share-msg">
        {state.phase === "loading" ? (
          <p className="mc-note">Loading the map…</p>
        ) : (
          <>
            <span className="mc-share-msg-icon" aria-hidden="true">
              <EyeIcon size={28} />
            </span>
            <h1>
              {state.phase === "ended" ? "This map link was turned off" : "The map isn't answering"}
            </h1>
            <p className="mc-note">
              {state.phase === "ended"
                ? "The person who shared it turned it off, or the world is gone. Ask them for a new link."
                : "The server may be restarting. Try again in a minute."}
            </p>
          </>
        )}
        <span className="mc-share-mark">
          JBrain<span>.</span>
        </span>
      </main>
    );
  }

  const slot = sharedSlot(state.view);
  return (
    <MapPage
      host={{
        source,
        slots: [slot],
        slotOf: (id) => (id === slot.id ? slot : null),
        startSlot: slot.id,
        running: state.view.active,
        onlineCount: 0,
        nowMs,
        covered: false,
        root,
        owner: null,
        focus: state.view.focus.xuid,
      }}
    />
  );
}
