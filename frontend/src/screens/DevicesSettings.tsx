// Settings → Devices: the owner's phones (OWNER_TUNNEL_PLAN §3.5, wave A5b; binding mock
// docs/mocks/owner-devices/b-devices-page.html). A phone that scanned the box's QR is a banner
// at the top, because confirming it is the security step: the box's screen is readable from
// its host network, so only the owner comparing the words proves the phone is theirs.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type OwnerDevice, type PublicUrlSetting, api } from "../api/client";
import { setPublicOrigin } from "../publicOrigin";

/** Often enough that a phone that just scanned shows up while the owner is still at the box. */
export const POLL_MS = 2_000;
const LABEL_MAX = 80;

/** "just now", "4 min ago", "3 h ago"; older than a day reads as a date. */
export function seenText(iso: string, now: number): string {
  const s = (now - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleString();
}

// The two home-network-only saves answer the same way: a 403 is where the owner is, not what
// they typed, so it says where to go; a 422 is the box's own words about the value.
function saveError(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) return "Change this from the home network.";
  if (e instanceof ApiError && e.status === 422) return e.message;
  return "Could not save.";
}

export function DevicesSettings() {
  const [devices, setDevices] = useState<OwnerDevice[] | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [armedUntil, setArmedUntil] = useState<number | null>(null);
  const [armError, setArmError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [publicUrl, setPublicUrl] = useState<PublicUrlSetting | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const [urlMsg, setUrlMsg] = useState<{ text: string; error: boolean } | null>(null);
  const [subnetDraft, setSubnetDraft] = useState("");
  const [subnetMsg, setSubnetMsg] = useState<{ text: string; error: boolean } | null>(null);
  // Pending phones already there when the window opened, so only a new one ends the countdown.
  const pendingAtArm = useRef<Set<string>>(new Set());
  const mounted = useRef(true);

  const load = useCallback(() => {
    void api
      .ownerDevices()
      .then((list) => {
        if (mounted.current) setDevices(list);
      })
      .catch(() => {
        if (mounted.current) setDevices((d) => d ?? []);
      });
  }, []);

  useEffect(() => {
    mounted.current = true;
    load();
    void api
      .publicUrl()
      .then((s) => {
        if (!mounted.current) return;
        setPublicUrl(s);
        setUrlDraft(s.url);
      })
      .catch(() => {});
    void api
      .ownerEnrollSubnet()
      .then((s) => {
        if (mounted.current) setSubnetDraft(s.subnet);
      })
      .catch(() => {});
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const list = devices ?? [];
  const pending = list.filter((d) => !d.confirmed && !d.revoked);
  const active = list.filter((d) => d.confirmed && !d.revoked);
  const revoked = list.filter((d) => d.revoked);
  const armed = armedUntil !== null;
  const polling = armed || pending.length > 0;

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [polling, load]);

  useEffect(() => {
    if (armedUntil === null) return;
    const timer = window.setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= armedUntil) setArmedUntil(null);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [armedUntil]);

  // A phone that scanned has used the window, so the card goes back to idle and the banner
  // above takes over.
  const scanned = armed && pending.some((d) => !pendingAtArm.current.has(d.id));
  useEffect(() => {
    if (scanned) setArmedUntil(null);
  }, [scanned]);

  function arm() {
    setArmError(null);
    void api
      .armOwnerEnroll()
      .then((r) => {
        pendingAtArm.current = new Set(pending.map((d) => d.id));
        setNow(Date.now());
        setArmedUntil(new Date(r.expires_at).getTime());
      })
      .catch((e) => {
        setArmError(
          e instanceof ApiError && e.status === 409 ? e.message : "Could not reach the box.",
        );
      });
  }

  function act(call: Promise<void>) {
    setActionError(null);
    void call.then(load).catch(() => setActionError("That didn't go through. Try again."));
  }

  function saveRename() {
    if (!renaming) return;
    const label = renaming.value.trim();
    if (!label || label.length > LABEL_MAX) return;
    act(api.renameOwnerDevice(renaming.id, label));
    setRenaming(null);
  }

  function saveUrl() {
    setUrlMsg(null);
    void api
      .setPublicUrl(urlDraft.trim())
      .then((s) => {
        setPublicUrl(s);
        setUrlDraft(s.url);
        setPublicOrigin(s.url || s.env_url || null);
        setUrlMsg({ text: "Saved.", error: false });
      })
      .catch((e) => setUrlMsg({ text: saveError(e), error: true }));
  }

  function saveSubnet() {
    setSubnetMsg(null);
    void api
      .setOwnerEnrollSubnet(subnetDraft.trim())
      .then((s) => {
        setSubnetDraft(s.subnet);
        setSubnetMsg({ text: "Saved.", error: false });
      })
      .catch((e) => setSubnetMsg({ text: saveError(e), error: true }));
  }

  const secondsLeft = armedUntil === null ? 0 : Math.max(0, Math.round((armedUntil - now) / 1000));
  const renameValid =
    renaming !== null &&
    renaming.value.trim().length > 0 &&
    renaming.value.trim().length <= LABEL_MAX;

  return (
    <>
      {pending.map((d) => (
        <section key={d.id} className="devices-banner" aria-label={`${d.label} wants in`}>
          <span className="devices-banner-ic" aria-hidden="true">
            !
          </span>
          <div className="devices-banner-body">
            <h2 className="devices-banner-title">{d.label} wants in</h2>
            <p className="settings-meta">
              It can't reach your box until you confirm. Check these match the phone:
            </p>
            <ul className="devices-words" aria-label="Words">
              {d.words.map((w, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: the words can repeat; order is the identity.
                <li key={i}>{w}</li>
              ))}
            </ul>
            <div className="devices-actions">
              <button
                type="button"
                className="devices-go"
                onClick={() => act(api.confirmOwnerDevice(d.id))}
              >
                Confirm
              </button>
              <button
                type="button"
                className="btn-destructive"
                onClick={() => act(api.revokeOwnerDevice(d.id))}
              >
                Not mine
              </button>
            </div>
          </div>
        </section>
      ))}
      {actionError && <p className="settings-meta settings-error">{actionError}</p>}

      <section className="settings-card">
        {armed ? (
          <div className="devices-armed">
            <div className="devices-ring" aria-label={`${secondsLeft} seconds left`}>
              {secondsLeft}
            </div>
            <p className="devices-armed-title">Scan the QR on the box's screen</p>
            <p className="settings-meta">With the JBrain owner app, from the home network.</p>
            <button type="button" onClick={() => setArmedUntil(null)}>
              Cancel
            </button>
          </div>
        ) : (
          <>
            <h2 className="settings-label">Add a phone</h2>
            <p className="settings-meta">
              The box's screen shows a code for two minutes. Stand in front of it with the phone, on
              the home network.
            </p>
            <button type="button" className="devices-primary" onClick={arm}>
              Show the code on the box
            </button>
            {armError && <p className="settings-meta settings-error devices-msg">{armError}</p>}
          </>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Your phones · {active.length}</h2>
        {active.length === 0 ? (
          <p className="settings-meta">None yet.</p>
        ) : (
          <ul className="devices-list" aria-label="Your phones">
            {active.map((d) => (
              <li key={d.id} className="devices-row">
                {renaming?.id === d.id ? (
                  <div className="devices-rename">
                    <input
                      value={renaming.value}
                      maxLength={LABEL_MAX}
                      aria-label="Phone name"
                      onChange={(e) => setRenaming({ id: d.id, value: e.currentTarget.value })}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveRename();
                        if (e.key === "Escape") setRenaming(null);
                      }}
                    />
                    <div className="devices-actions">
                      <button type="button" disabled={!renameValid} onClick={saveRename}>
                        Save
                      </button>
                      <button type="button" onClick={() => setRenaming(null)}>
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="devices-row-main">
                      <div className="devices-name">
                        {d.label} <span className="devices-pill">connected</span>
                      </div>
                      <p className="settings-meta">
                        {d.last_seen_at
                          ? `last seen ${seenText(d.last_seen_at, Date.now())}${
                              d.last_ingress ? ` · via ${d.last_ingress}` : ""
                            }`
                          : "never seen"}
                      </p>
                    </div>
                    <div className="devices-row-actions">
                      <button
                        type="button"
                        onClick={() => setRenaming({ id: d.id, value: d.label })}
                      >
                        Rename
                      </button>
                      <button
                        type="button"
                        className="btn-destructive"
                        onClick={() => {
                          if (revoking === d.id) {
                            setRevoking(null);
                            act(api.revokeOwnerDevice(d.id));
                          } else setRevoking(d.id);
                        }}
                        onBlur={() => setRevoking(null)}
                      >
                        {revoking === d.id ? "Tap to confirm" : "Revoke"}
                      </button>
                    </div>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
        {revoked.length > 0 && (
          <p className="settings-meta devices-revoked">
            Revoked: {revoked.map((d) => d.label).join(", ")}
          </p>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Links you share</h2>
        <div className="devices-field">
          <input
            value={urlDraft}
            placeholder={publicUrl?.env_url || "https://brain.example.com"}
            aria-label="Public address"
            inputMode="url"
            onChange={(e) => setUrlDraft(e.currentTarget.value)}
          />
          <button type="button" onClick={saveUrl}>
            Save
          </button>
        </div>
        {urlMsg && (
          <p className={`settings-meta devices-msg${urlMsg.error ? " settings-error" : ""}`}>
            {urlMsg.text}
          </p>
        )}
        <p className="settings-meta devices-msg">
          Share links, chat shares and map shares start with this address, never the phone's private
          one.
        </p>
        {publicUrl && !publicUrl.url && (
          <p className="settings-meta">
            {publicUrl.env_url
              ? `Using ${publicUrl.env_url} from the box's setup.`
              : "Not set: links use the address you opened this app at."}
          </p>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Home network</h2>
        <div className="devices-field">
          <input
            value={subnetDraft}
            placeholder="192.168.1.0/24"
            aria-label="Home subnet"
            onChange={(e) => setSubnetDraft(e.currentTarget.value)}
          />
          <button type="button" onClick={saveSubnet}>
            Save
          </button>
        </div>
        {subnetMsg && (
          <p className={`settings-meta devices-msg${subnetMsg.error ? " settings-error" : ""}`}>
            {subnetMsg.text}
          </p>
        )}
        <p className="settings-meta devices-msg">
          Phones can be added only from this network. You can change it only from home.
        </p>
      </section>
    </>
  );
}
