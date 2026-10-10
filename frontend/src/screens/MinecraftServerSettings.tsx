// Screen B's server-wide sections (DESIGN.md "Minecraft server screen" → "Worlds and
// backups"): the settings every world shares, and the allowlist. Both stay on the main
// screen; what a world owns lives on its own page.
//
// On/off and the settings apply at the next restart, but the API reports only the saved
// value, so "pending" here is what this device saved since the server last started.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type MinecraftAllowlist,
  type MinecraftServerSettings,
  api,
} from "../api/client";
import { Dialog } from "../components/Dialog";
import { MinusIcon, PlusIcon, ShieldIcon, XIcon } from "../components/icons";
import { plural } from "../minecraft";

const MAX_PLAYERS: [number, number] = [1, 30];
const VIEW: [number, number] = [5, 32];
const SLOTS = 5;

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

interface Draft {
  server_name: string;
  max_players: number;
  view_distance: number;
}

const num = (v: number | string | null, fallback: number) => {
  const n = Number(v);
  return Number.isFinite(n) && v !== null && v !== "" ? n : fallback;
};

function toDraft(s: MinecraftServerSettings): Draft {
  return {
    server_name: s.server_name ?? "",
    max_players: num(s.max_players, 10),
    view_distance: num(s.view_distance, 32),
  };
}

/** Clears this device's "pending" once the server is seen starting again. */
function useAppliedOnStart(running: boolean): [boolean, (on: boolean) => void] {
  const [pending, setPending] = useState(false);
  const was = useRef(running);
  useEffect(() => {
    if (running && !was.current) setPending(false);
    was.current = running;
  }, [running]);
  return [pending, setPending];
}

export function ServerSettingsSection({
  running,
  online,
  toast,
  onError,
}: {
  running: boolean;
  online: number;
  toast: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [saved, setSaved] = useState<Draft | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [failed, setFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useAppliedOnStart(running);

  useEffect(() => {
    api
      .minecraftServerSettings()
      .then((s) => setSaved(toDraft(s)))
      .catch(() => setFailed(true));
  }, []);

  const shown = draft ?? saved;
  const dirty = !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved);
  const step = (k: "max_players" | "view_distance", d: number, [lo, hi]: [number, number]) => {
    if (!shown) return;
    setDraft({ ...shown, [k]: Math.max(lo, Math.min(hi, shown[k] + d)) });
  };

  async function save() {
    if (!draft || !draft.server_name.trim()) return;
    setSaving(true);
    try {
      const next = await api.minecraftSaveServerSettings({
        ...draft,
        server_name: draft.server_name.trim(),
      });
      setSaved(toDraft(next));
      setDraft(null);
      setPending(true);
      toast(
        running ? "Saved — applies at the next restart" : "Saved — applies when the server starts",
      );
    } catch (err) {
      onError(`Couldn't save the server settings — ${message(err)}`);
    } finally {
      setSaving(false);
    }
  }

  const stepper = (
    k: "max_players" | "view_distance",
    label: string,
    sub: string,
    range: [number, number],
  ) => (
    <div className="mc-set">
      <div className="mc-set-l">
        <span>{label}</span>
        <span className="mc-set-sub">{sub}</span>
      </div>
      <div className="mc-stepper">
        <button
          type="button"
          className="mc-btn"
          aria-label={`Fewer — ${label}`}
          disabled={!shown || shown[k] <= range[0]}
          onClick={() => step(k, -1, range)}
        >
          <MinusIcon size={18} />
        </button>
        <span className="mc-sv" aria-live="polite">
          {shown?.[k] ?? "—"}
        </span>
        <button
          type="button"
          className="mc-btn"
          aria-label={`More — ${label}`}
          disabled={!shown || shown[k] >= range[1]}
          onClick={() => step(k, 1, range)}
        >
          <PlusIcon size={18} />
        </button>
      </div>
    </div>
  );

  return (
    <>
      <h3 className="mc-sect">
        Server settings<span className="mc-sect-r">every world</span>
      </h3>
      <div className="mc-card">
        {failed && !shown ? (
          <div className="mc-empty">Couldn&apos;t read the server settings.</div>
        ) : (
          <div className="mc-pad mc-pad-roomy">
            <div className="mc-fld">
              <label htmlFor="mc-srv-name">
                Server name {pending && <span className="mc-pend">pending</span>}
              </label>
              <input
                id="mc-srv-name"
                className="mc-inp"
                value={shown?.server_name ?? ""}
                maxLength={40}
                disabled={!shown}
                autoComplete="off"
                onChange={(e) => shown && setDraft({ ...shown, server_name: e.target.value })}
              />
              <span className="mc-note">Shown in Friends → LAN Games.</span>
            </div>
            {stepper("max_players", "Max players", "at once, 1–30", MAX_PLAYERS)}
            {stepper("view_distance", "View distance", "chunks, 5–32", VIEW)}
            <div className="mc-sw-row">
              <span className="mc-sw-l">
                <b>World slots</b>
                <br />
                {SLOTS} — one world loaded at a time
              </span>
              <span className="badge off">{SLOTS}</span>
            </div>
            <div className="mc-sw-row">
              <span className="mc-sw-l">
                <b>Xbox sign-in required</b>
                <br />
                always on — every player is a signed-in Xbox account
              </span>
              <span className="badge off">on</span>
            </div>
            <p className="mc-note">
              {running
                ? `Applies at the next restart${online ? " — nobody is disconnected by saving" : ""}.`
                : "Applies when the server starts."}
            </p>
            <div className="mc-btnrow">
              <button
                type="button"
                className="mc-btn"
                disabled={!dirty || saving}
                onClick={() => setDraft(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="mc-btn mc-btn-primary mc-grow"
                disabled={!dirty || saving || !draft?.server_name.trim()}
                onClick={() => void save()}
              >
                Save settings
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

export function AllowlistSection({
  running,
  online,
  known,
  toast,
  onError,
}: {
  running: boolean;
  online: string[];
  /** Gamertags that have played here, for each row's second line. */
  known: string[];
  toast: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [list, setList] = useState<MinecraftAllowlist | null>(null);
  const [failed, setFailed] = useState(false);
  const [tag, setTag] = useState("");
  const [confirmOff, setConfirmOff] = useState(false);
  const [pending, setPending] = useAppliedOnStart(running);

  const load = useCallback(() => {
    api
      .minecraftAllowlist()
      .then(setList)
      .catch(() => setFailed(true));
  }, []);
  useEffect(load, []);

  async function change(body: { add?: string; remove?: string; enabled?: boolean }) {
    try {
      setList(await api.minecraftChangeAllowlist(body));
      return true;
    } catch (err) {
      onError(`Couldn't change the allowlist — ${message(err)}`);
      return false;
    }
  }

  async function setEnabled(on: boolean) {
    setConfirmOff(false);
    if (await change({ enabled: on })) {
      setPending(!pending);
      const when = running ? "from the next restart" : "when the server starts";
      toast(`Allowlist ${on ? "on" : "off"} ${when}`);
    }
  }

  async function add() {
    const t = tag.trim();
    if (!t || !list) return;
    if (list.players.some((p) => p.toLowerCase() === t.toLowerCase())) {
      toast(`${t} is already on the list`);
      return;
    }
    if (await change({ add: t })) {
      setTag("");
      toast(
        running
          ? `${t} can join now — no restart needed`
          : `${t} added — applied when the server starts`,
      );
    }
  }

  async function remove(t: string) {
    if (await change({ remove: t })) {
      toast(online.includes(t) ? `${t} removed — they stay on until they leave` : `${t} removed`);
    }
  }

  if (!list) {
    return (
      <>
        <h3 className="mc-sect">Allowlist</h3>
        <div className="mc-card">
          <div className="mc-empty">{failed ? "Couldn't read the allowlist." : "Loading…"}</div>
        </div>
      </>
    );
  }
  const on = list.enabled;
  const state = pending
    ? `${on ? "on" : "off"} ${running ? "from the next restart" : "when the server starts"} — ${on ? "anyone on your network can join until then" : "only these gamertags can join until then"}`
    : on
      ? "only these gamertags can join"
      : "anyone on your network can join";
  return (
    <>
      <h3 className="mc-sect">
        Allowlist
        <span className="mc-sect-r">{on ? plural(list.players.length, "gamertag") : "off"}</span>
      </h3>
      <div className="mc-card">
        <div className="mc-pad">
          <div className="mc-sw-row">
            <span className="mc-sw-l" id="mc-allow-l">
              <b>Allowlist {on ? "on" : "off"}</b>{" "}
              {pending && <span className="mc-pend">pending</span>}
              <br />
              {state}
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={on}
              aria-label="Allowlist"
              className={`mc-switch${on ? " on" : ""}`}
              onClick={() => (on ? setConfirmOff(true) : void setEnabled(true))}
            >
              <span className="mc-knob" />
            </button>
          </div>
          <div className={`mc-offbox${on ? "" : " warn"}`}>
            <ShieldIcon size={16} />
            <span>
              <b>Required for internet play.</b>{" "}
              {on
                ? "Internet play (coming later) only switches on with the list on."
                : "Internet play (coming later) won't switch on while it's off."}
            </span>
          </div>
        </div>
        {list.players.map((t) => {
          const isOn = online.includes(t);
          return (
            <div className="mc-alrow" key={t}>
              <span className={`mc-pav${isOn ? " on" : ""}`} aria-hidden="true">
                {(t[0] ?? "?").toUpperCase()}
              </span>
              <span className="mc-alnm">
                {t}
                <span className="mc-sm">
                  {isOn ? "on now" : known.includes(t) ? "has played here" : "hasn't joined yet"}
                </span>
              </span>
              <button
                type="button"
                className="mc-icon-btn"
                aria-label={`Remove ${t} from the allowlist`}
                onClick={() => void remove(t)}
              >
                <XIcon size={18} />
              </button>
            </div>
          );
        })}
        <div className="mc-pad mc-topline">
          <div className="mc-fld">
            <label htmlFor="mc-al-add">Add a gamertag</label>
            <div className="mc-inp-row">
              <input
                id="mc-al-add"
                className="mc-inp"
                placeholder="Gamertag"
                value={tag}
                maxLength={32}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                onChange={(e) => setTag(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void add();
                }}
              />
              <button
                type="button"
                className="mc-btn mc-btn-primary"
                disabled={!tag.trim()}
                onClick={() => void add()}
              >
                <PlusIcon size={18} />
                Add
              </button>
            </div>
            <span className="mc-note">
              {running
                ? "Adding or removing a name is live at once — no restart."
                : "Server stopped — added and removed names apply when it starts."}
              {on ? "" : " Kept for when the list is on."}
            </span>
          </div>
        </div>
      </div>
      {confirmOff && (
        <Dialog
          title="Turn the allowlist off?"
          confirmLabel="Turn off"
          tone="warn"
          onCancel={() => setConfirmOff(false)}
          onConfirm={() => void setEnabled(false)}
        >
          From the {running ? "next restart" : "next start"}, anyone who can reach the server on
          your network can join, and internet play stays off until it&apos;s back on.
        </Dialog>
      )}
    </>
  );
}
