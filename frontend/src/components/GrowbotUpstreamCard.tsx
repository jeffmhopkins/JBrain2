// "Walks and spins": whether the official GrowBot repo has published new body policies since
// the copy the box serves (binding mock docs/mocks/growbot-policies/a-body-card.html, chosen by
// the owner 2026-11-12). Bringing them in is a sync PR, not a button, so the card says who does
// it rather than offering an action that cannot work from a phone.

import { useCallback, useEffect, useState } from "react";
import { ApiError, type GrowbotUpstream, type GrowbotUpstreamPolicy, api } from "../api/client";
import { ago, day } from "../growbot";

const CHANGE_TAG: Record<GrowbotUpstreamPolicy["change"], string> = {
  new: "ok",
  changed: "warn",
  renamed: "warn",
  removed: "bad",
};

function short(path: string): string {
  return path.replace(/^app\/policies\//, "").replace(/^app\//, "");
}

// The answer when the box could not be asked at all: no check to show, just why.
function unread(error: string): GrowbotUpstream {
  return {
    state: "unknown",
    repo_url: "",
    pinned: null,
    head: null,
    head_at: "",
    ahead_by: null,
    commits: [],
    policies: null,
    other_files: null,
    compare_url: "",
    checked_at: null,
    error,
  };
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function GrowbotUpstreamCard() {
  const [up, setUp] = useState<GrowbotUpstream | null>(null);
  // An older box has no such route: the card stays away rather than showing an error.
  const [absent, setAbsent] = useState(false);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState("");

  useEffect(() => {
    let live = true;
    api
      .growbotUpstream()
      .then((u) => live && setUp(u))
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) setAbsent(true);
        else setUp(unread(e instanceof Error ? e.message : String(e)));
      });
    return () => {
      live = false;
    };
  }, []);

  const check = useCallback(async () => {
    setChecking(true);
    setFailed("");
    try {
      setUp(await api.growbotCheckUpstream());
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  }, []);

  if (absent || up === null) return null;

  const policies = up.policies ?? [];
  const behind = up.state === "behind";
  const level =
    checking || (up.state === "unknown" && !up.checked_at && !up.error)
      ? ""
      : behind
        ? "warn"
        : up.state === "current"
          ? "ok"
          : "bad";
  // Only ever a link the box built to GitHub, never whatever a stored answer says.
  const github = up.compare_url.startsWith("https://github.com/") ? up.compare_url : "";
  const stateText = checking
    ? "Checking GitHub…"
    : up.state === "current"
      ? "Up to date"
      : behind
        ? up.policies === null
          ? "Upstream has moved"
          : policies.length
            ? `${policies.length} new or changed`
            : "No new policies"
        : up.checked_at || up.error
          ? "Couldn't check"
          : "Not checked yet";

  return (
    <section className="gb-card" aria-label="Walks and spins">
      <div className="gb-card-head">
        <h3>Walks and spins</h3>
        <span className="gb-state">
          <span className={`gb-dot ${level}`} aria-hidden="true" />
          <span className={level ? `gb-${level}-t` : ""}>{stateText}</span>
        </span>
      </div>
      {up.state === "current" && (
        <p className="gb-hint">
          Your robot runs the same walks and spins as the official GrowBot repo.
        </p>
      )}
      {behind && policies.length > 0 && (
        <ul className="gb-up-list">
          {policies.map((p) => (
            <li key={p.path}>
              <span className="gb-mono gb-up-nm">{short(p.path)}</span>
              <span className={`gb-tag ${CHANGE_TAG[p.change]}`}>{p.change}</span>
            </li>
          ))}
        </ul>
      )}
      {behind && up.policies !== null && policies.length === 0 && (
        <p className="gb-hint">Upstream changed other files but published no new walks or spins.</p>
      )}
      {behind && up.commits.length > 0 && (
        <details className="gb-up-more">
          <summary>
            {up.ahead_by === null ? `${up.commits.length}+ commits` : plural(up.ahead_by, "commit")}{" "}
            since your copy
            {up.other_files ? `, ${plural(up.other_files, "other file")}` : ""}
          </summary>
          <ul className="gb-up-list">
            {up.commits.map((c) => (
              <li key={c.sha}>
                <span className="gb-up-nm">{c.title}</span>
                {c.date && <span className="gb-up-when">{day(c.date)}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {behind && policies.length > 0 && (
        <p className="gb-hint">
          These come in with a GrowBot sync, which is a code change: ask Claude to "sync GrowBot to
          upstream". When it has merged, <strong>Ops → Update</strong> puts the new walks and spins
          on the robot.
        </p>
      )}
      {up.error && <p className="gb-hint gb-bad-t">{up.error}</p>}
      {failed && <p className="gb-hint gb-bad-t">{failed}</p>}
      <dl className="gb-facts">
        {up.pinned && (
          <>
            <dt>Your copy</dt>
            <dd className="gb-mono">{up.pinned.slice(0, 7)}</dd>
          </>
        )}
        {up.head && up.head !== up.pinned && (
          <>
            <dt>Upstream</dt>
            <dd className="gb-mono">
              {up.head.slice(0, 7)}
              {up.head_at ? ` · ${day(up.head_at)}` : ""}
            </dd>
          </>
        )}
        <dt>Checked</dt>
        <dd>{up.checked_at ? ago(up.checked_at) : "not yet"}</dd>
      </dl>
      <div className="gb-acts">
        <button type="button" className="gb-btn" onClick={() => void check()} disabled={checking}>
          {checking ? "Checking…" : "Check now"}
        </button>
        {behind && github && (
          <a className="gb-btn" href={github} target="_blank" rel="noreferrer">
            See on GitHub
          </a>
        )}
      </div>
    </section>
  );
}
