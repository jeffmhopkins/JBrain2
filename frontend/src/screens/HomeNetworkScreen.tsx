// Home network: the house's DNS (docs/archive/HOME_DNS_PLAN.md wave B; the binding mock is
// docs/mocks/home-network/a-devices.html, "Devices first"). The owner's question is "which
// device is telling businesses the most", so devices lead, ranked by how much of their recent
// traffic the lists refused. AdGuard counts lookups per device but not blocks, so that share is
// read from each device's latest lookups and the copy says "recent", never "this week".

import { useCallback, useEffect, useState } from "react";
import {
  type HomeNetDevice,
  type HomeNetDeviceDetail,
  type HomeNetQuery,
  type HomeNetRules,
  type HomeNetSummary,
  api,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import { Sheet } from "../components/Sheet";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  MonitorIcon,
  PencilIcon,
  SmartphoneIcon,
  ThingIcon,
} from "../components/icons";
import "./homenet.css";

/** A whole percent of `part` in `total`, 0 when there is nothing to divide. */
export function share(part: number, total: number): number {
  return total > 0 ? Math.round((part / total) * 100) : 0;
}

export type DeviceKind = "tv" | "phone" | "other";

// Only a name says what a device is: AdGuard sees an address and whatever the router calls it.
const TV = /\b(tv|roku|fire ?stick|chromecast|google tv|apple tv|bravia|vizio|webos|tizen)\b/i;
const PHONE = /\b(phone|iphone|pixel|galaxy|android|ipad|tablet)\b/i;

export function deviceKind(name: string): DeviceKind {
  if (TV.test(name)) return "tv";
  if (PHONE.test(name)) return "phone";
  return "other";
}

/** What the summary says about the router: no device yet means nothing points at the box,
 *  and one device means the router asks on everyone's behalf, so they can't be told apart. */
export function networkState(s: HomeNetSummary): "normal" | "router" | "setup" {
  if (s.devices.length === 0) return "setup";
  if (s.devices.length === 1) return "router";
  return "normal";
}

export function ago(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const mins = Math.max(0, Math.round((now - t) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

const fmt = (n: number) => n.toLocaleString("en-US");

function deviceLabel(d: { ip: string; name: string }): string {
  return d.name || d.ip;
}

function KindIcon({ name }: { name: string }) {
  const kind = deviceKind(name);
  return (
    <span className={`hn-kind${kind === "tv" ? " tv" : ""}`} aria-hidden="true">
      {kind === "tv" ? (
        <MonitorIcon size={20} />
      ) : kind === "phone" ? (
        <SmartphoneIcon size={20} />
      ) : (
        <ThingIcon size={20} />
      )}
    </span>
  );
}

function Meter({ pct }: { pct: number }) {
  return (
    <span className="hn-meter" aria-hidden="true">
      <i style={{ width: `${pct}%` }} />
    </span>
  );
}

/** A name opened from a list or the log, with what the screen knew of it there. */
interface NameTarget {
  domain: string;
  blocked: boolean;
  list: string;
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function HomeNetworkScreen() {
  const [summary, setSummary] = useState<HomeNetSummary | null>(null);
  const [rules, setRules] = useState<HomeNetRules>({ blocked: [], allowed: [] });
  const [down, setDown] = useState<string | null>(null);
  const [openIp, setOpenIp] = useState<string | null>(null);
  const [name, setName] = useState<NameTarget | null>(null);
  const [pausing, setPausing] = useState(false);
  const [notice, setNotice] = useState("");
  // Bumped after a rename so an open device re-reads its name.
  const [epoch, setEpoch] = useState(0);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api.homeNetwork(), api.homeNetworkRules()]);
      setSummary(s);
      setRules(r);
      setDown(null);
    } catch (e) {
      setSummary(null);
      setDown(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function setProtection(enabled: boolean, minutes = 0) {
    setPausing(false);
    try {
      const on = await api.setHomeNetworkProtection(enabled, minutes);
      setNotice(
        on
          ? "Blocking is back on"
          : minutes
            ? `Blocking paused for ${minutes === 60 ? "1 hour" : `${minutes} minutes`}`
            : "Blocking paused",
      );
      await load();
    } catch (e) {
      setNotice(`Couldn't change blocking: ${errorText(e)}`);
    }
  }

  async function setRule(target: NameTarget, action: "block" | "allow" | "clear") {
    setName(null);
    try {
      setRules(await api.setHomeNetworkRule(target.domain, action));
      setNotice(
        action === "clear"
          ? `${target.domain} is back to what the lists say`
          : `${action === "allow" ? "Allowed" : "Blocked"} ${target.domain} for every device`,
      );
    } catch (e) {
      setNotice(`Couldn't change ${target.domain}: ${errorText(e)}`);
    }
  }

  const open = openIp && summary ? summary.devices.find((d) => d.ip === openIp) : undefined;

  return (
    <main className="screen-body hn-screen">
      {open ? (
        <DeviceView
          key={`${open.ip}-${epoch}`}
          device={open}
          onBack={() => setOpenIp(null)}
          onName={setName}
          onRenamed={async (label) => {
            setNotice(`Renamed to ${label}`);
            await load();
            setEpoch((n) => n + 1);
          }}
        />
      ) : (
        <Overview
          summary={summary}
          down={down}
          onOpen={setOpenIp}
          onToggle={() =>
            summary?.protection_enabled ? setPausing(true) : void setProtection(true)
          }
        />
      )}
      {notice && <output className="hn-notice">{notice}</output>}
      {pausing && (
        <Sheet title="Pause blocking" onClose={() => setPausing(false)}>
          <div className="hn-sheet">
            <p className="hn-muted">
              Every device, ads and trackers included, until it comes back on.
            </p>
            <div className="hn-acts">
              <button
                type="button"
                className="hn-btn"
                onClick={() => void setProtection(false, 10)}
              >
                For 10 minutes
              </button>
              <button
                type="button"
                className="hn-btn"
                onClick={() => void setProtection(false, 60)}
              >
                For 1 hour
              </button>
              <button
                type="button"
                className="hn-btn danger"
                onClick={() => void setProtection(false)}
              >
                Until I turn it back on
              </button>
            </div>
          </div>
        </Sheet>
      )}
      {name && (
        <NameSheet target={name} rules={rules} onClose={() => setName(null)} onRule={setRule} />
      )}
    </main>
  );
}

function Banner({ tone, title, children }: { tone: string; title: string; children: string }) {
  return (
    <div className={`hn-banner ${tone}`}>
      <div className="hn-banner-t">{title}</div>
      <p>{children}</p>
    </div>
  );
}

function Overview({
  summary,
  down,
  onOpen,
  onToggle,
}: {
  summary: HomeNetSummary | null;
  down: string | null;
  onOpen: (ip: string) => void;
  onToggle: () => void;
}) {
  if (down) {
    return (
      <Banner tone="bad" title="The house's DNS is not answering">
        {`Devices fall back to the router's second DNS, so they're still filtered, but nothing is logged here. ${down}`}
      </Banner>
    );
  }
  if (!summary) return <p className="hn-empty">Loading…</p>;
  const state = networkState(summary);
  const on = summary.protection_enabled;
  const box = summary.dns_addresses.find((a) => /^\d+\.\d+\.\d+\.\d+$/.test(a)) ?? "this box";
  const devices = [...summary.devices].sort(
    (a, b) => share(b.recent_blocked, b.recent) - share(a.recent_blocked, a.recent),
  );
  return (
    <>
      {state === "router" && (
        <Banner tone="warn" title="Every lookup comes from one address">
          Blocking works for the whole house, but the router is passing lookups on instead of
          telling each device to ask the box, so this screen can't tell the devices apart.
        </Banner>
      )}
      {state === "setup" && (
        <Banner tone="steel" title="Nothing is using it yet">
          {`In the My Spectrum app, set Primary DNS to ${box} and Secondary to 94.140.14.14. Devices switch over as they reconnect.`}
        </Banner>
      )}
      <section className="hn-card hn-status">
        <div className="hn-status-top">
          <span className={`hn-dot ${on ? "ok" : "warn"}`} />
          <div className="hn-grow">
            <div className="hn-status-t">
              {on ? "Blocking ads and trackers" : "Blocking paused"}
            </div>
            <div className="hn-faint">
              {on
                ? "AdGuard Home on this box"
                : summary.resumes_in_s > 0
                  ? `Back on by itself in ${Math.ceil(summary.resumes_in_s / 60)} min`
                  : "Until you turn it back on"}
            </div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label="Blocking"
            className="hn-switch"
            onClick={onToggle}
          >
            <i />
          </button>
        </div>
        <div className="hn-stats">
          <div>
            <b>{fmt(summary.queries)}</b>
            <span>lookups this week</span>
          </div>
          <div>
            <b className="hn-rose">{fmt(summary.blocked)}</b>
            <span>blocked</span>
          </div>
          <div>
            <b>{share(summary.blocked, summary.queries)}%</b>
            <span>of all lookups</span>
          </div>
        </div>
      </section>
      <h2 className="hn-h">Devices · most tracking first</h2>
      <section className="hn-card">
        {devices.length ? (
          devices.map((d) => (
            <DeviceRow
              key={d.ip}
              device={d}
              disabled={state === "router"}
              onOpen={() => onOpen(d.ip)}
            />
          ))
        ) : (
          <p className="hn-empty">Devices appear here once the router points at the box.</p>
        )}
      </section>
    </>
  );
}

function DeviceRow({
  device: d,
  disabled,
  onOpen,
}: {
  device: HomeNetDevice;
  disabled: boolean;
  onOpen: () => void;
}) {
  const pct = share(d.recent_blocked, d.recent);
  return (
    <button type="button" className="hn-row" disabled={disabled} onClick={onOpen}>
      <KindIcon name={d.name} />
      <span className="hn-main">
        <span className="hn-t">{deviceLabel(d)}</span>
        <Meter pct={pct} />
        <span className="hn-s">
          {d.recent
            ? `${fmt(d.recent_blocked)} of ${fmt(d.recent)} recent blocked · ${d.ip}`
            : `${fmt(d.queries)} lookups this week · ${d.ip}`}
        </span>
      </span>
      <span className="hn-r hn-rose">{pct}%</span>
      {!disabled && <ChevronRightIcon size={18} />}
    </button>
  );
}

function DeviceView({
  device,
  onBack,
  onName,
  onRenamed,
}: {
  device: HomeNetDevice;
  onBack: () => void;
  onName: (t: NameTarget) => void;
  onRenamed: (label: string) => Promise<void>;
}) {
  useBackLayer(onBack);
  const [detail, setDetail] = useState<HomeNetDeviceDetail | null>(null);
  const [rows, setRows] = useState<HomeNetQuery[] | null>(null);
  const [blockedOnly, setBlockedOnly] = useState(true);
  const [renaming, setRenaming] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .homeNetworkDevice(device.ip)
      .then(setDetail)
      .catch((e: unknown) => setError(errorText(e)));
  }, [device.ip]);

  useEffect(() => {
    setRows(null);
    api
      .homeNetworkQueries({ client: device.ip, blocked: blockedOnly, limit: 50 })
      .then((q) => setRows(q.rows))
      .catch((e: unknown) => setError(errorText(e)));
  }, [device.ip, blockedOnly]);

  const label = deviceLabel(detail ?? device);
  const recent = detail?.recent ?? device.recent;
  const recentBlocked = detail?.recent_blocked ?? device.recent_blocked;
  const pct = share(recentBlocked, recent);
  const lists = new Map((detail?.top_blocked ?? []).map((t) => [t.domain, t.list]));

  return (
    <>
      <div className="hn-back">
        <button
          type="button"
          className="hn-iconbtn"
          aria-label="Back to all devices"
          onClick={onBack}
        >
          <ChevronLeftIcon size={20} />
        </button>
        <h2 className="hn-title">{label}</h2>
      </div>
      <section className="hn-card hn-head">
        <div className="hn-status-top">
          <KindIcon name={label} />
          <div className="hn-grow">
            <div className="hn-faint hn-mono">{device.ip}</div>
            <div className="hn-muted">{fmt(device.queries)} lookups this week</div>
          </div>
          <button
            type="button"
            className="hn-iconbtn"
            aria-label="Rename"
            onClick={() => setRenaming(true)}
          >
            <PencilIcon size={18} />
          </button>
        </div>
        <Meter pct={pct} />
        <div className="hn-muted">
          <b className="hn-rose">{pct}%</b> of its last {fmt(recent)} lookups blocked
        </div>
      </section>
      {error && <p className="hn-error">{error}</p>}
      {detail && detail.top_blocked.length > 0 && (
        <>
          <h2 className="hn-h">Who it tried to reach</h2>
          <section className="hn-card">
            {detail.top_blocked.map((t) => (
              <button
                key={t.domain}
                type="button"
                className="hn-row"
                onClick={() => onName({ domain: t.domain, blocked: true, list: t.list })}
              >
                <span className="hn-main">
                  <span className="hn-t hn-mono">{t.domain}</span>
                  <span className="hn-s">{t.list ? `Caught by ${t.list}` : "Blocked"}</span>
                </span>
                <span className="hn-r">{fmt(t.count)}</span>
              </button>
            ))}
          </section>
        </>
      )}
      <div className="hn-log-head">
        <h2 className="hn-h">Recent lookups</h2>
        <div className="hn-seg">
          <button type="button" aria-pressed={blockedOnly} onClick={() => setBlockedOnly(true)}>
            Blocked
          </button>
          <button type="button" aria-pressed={!blockedOnly} onClick={() => setBlockedOnly(false)}>
            All
          </button>
        </div>
      </div>
      <section className="hn-card">
        {rows === null ? (
          <p className="hn-empty">Loading…</p>
        ) : rows.length ? (
          rows.map((r, i) => (
            <button
              key={`${r.time}-${r.domain}-${i}`}
              type="button"
              className={`hn-row hn-logrow${r.blocked ? " blocked" : ""}`}
              onClick={() =>
                onName({ domain: r.domain, blocked: r.blocked, list: lists.get(r.domain) ?? "" })
              }
            >
              <span className="hn-main">
                <span className="hn-t hn-mono">{r.domain}</span>
                <span className="hn-s">{ago(r.time)}</span>
              </span>
              {r.blocked && <span className="hn-badge">blocked</span>}
            </button>
          ))
        ) : (
          <p className="hn-empty">
            {blockedOnly ? "Nothing blocked recently." : "No lookups yet."}
          </p>
        )}
      </section>
      {renaming && (
        <RenameSheet
          ip={device.ip}
          current={detail?.name ?? device.name}
          onClose={() => setRenaming(false)}
          onSaved={async (n) => {
            setRenaming(false);
            await onRenamed(n);
          }}
        />
      )}
    </>
  );
}

function RenameSheet({
  ip,
  current,
  onClose,
  onSaved,
}: {
  ip: string;
  current: string;
  onClose: () => void;
  onSaved: (name: string) => Promise<void>;
}) {
  const [value, setValue] = useState(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const trimmed = value.trim();

  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.nameHomeNetworkDevice(ip, trimmed);
      await onSaved(trimmed);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Sheet title="Name this device" onClose={onClose}>
      <div className="hn-sheet">
        <input
          className="hn-input"
          aria-label="Device name"
          value={value}
          maxLength={64}
          onChange={(e) => setValue(e.target.value)}
        />
        {error && <p className="hn-error">{error}</p>}
        <div className="hn-acts">
          <button
            type="button"
            className="hn-btn primary"
            disabled={!trimmed || busy}
            onClick={() => void save()}
          >
            Save
          </button>
          <button type="button" className="hn-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </Sheet>
  );
}

function NameSheet({
  target,
  rules,
  onClose,
  onRule,
}: {
  target: NameTarget;
  rules: HomeNetRules;
  onClose: () => void;
  onRule: (t: NameTarget, action: "block" | "allow" | "clear") => void;
}) {
  const allowedByMe = rules.allowed.includes(target.domain);
  const blockedByMe = rules.blocked.includes(target.domain);
  // The owner's own rule wins over what the log row saw, since it may be newer.
  const blocked = allowedByMe ? false : blockedByMe ? true : target.blocked;
  return (
    <Sheet title={target.domain} onClose={onClose}>
      <div className="hn-sheet">
        {target.list && <p className="hn-muted">Caught by {target.list}</p>}
        <div className="hn-pills">
          <span className={`hn-pill ${blocked ? "bad" : "ok"}`}>
            {blocked ? "Blocked" : "Allowed"}
          </span>
          {allowedByMe && <span className="hn-pill steel">you allowed it</span>}
          {blockedByMe && <span className="hn-pill steel">you blocked it</span>}
        </div>
        <div className="hn-acts">
          {blocked ? (
            <button type="button" className="hn-btn ok" onClick={() => onRule(target, "allow")}>
              Allow for every device
            </button>
          ) : (
            <button type="button" className="hn-btn danger" onClick={() => onRule(target, "block")}>
              Block for every device
            </button>
          )}
          {(allowedByMe || blockedByMe) && (
            <button type="button" className="hn-btn" onClick={() => onRule(target, "clear")}>
              Go back to what the lists say
            </button>
          )}
          <button type="button" className="hn-btn" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="hn-faint">
          Allowing a name fixes a TV menu or app that broke behind a list. It takes effect on the
          device's next lookup, within a minute.
        </p>
      </div>
    </Sheet>
  );
}
