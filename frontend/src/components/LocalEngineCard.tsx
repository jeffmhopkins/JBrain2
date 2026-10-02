// Ops → Local engine: switch the box between Standard (the `local-llm` gateway) and
// Flash-Next (one model, its own container). Exactly one runs at a time.
//
// Binding mock: docs/mocks/engine-switch/a-segmented-toggle.html (GUI gate settled on A;
// reasoning in docs/reference/DESIGN.md "Ops Local engine card"). A switch stops every local
// model for minutes, so the segmented control only ARMS it: the inline confirm states the
// consequence before anything is sent, and progress is phased text + steps + the notes tail,
// the same register as Server update. The card opens itself while a switch runs, after a
// rollback, and while the box serves a different engine than the one chosen.

import { useEffect, useRef, useState } from "react";
import type { EngineId, EngineState, EngineSwitchStatus } from "../api/client";
import { EngineCancelUnsupported, api } from "../api/client";
import {
  ENGINE_LABEL,
  OTHER_ENGINE,
  activeSince,
  armEngineSwitch,
  dismissEngineSwitch,
  hhmm,
  hhmmss,
  holdEngineArm,
  kickEnginePoll,
  openOnBoxModels,
  setEngineState,
  switchInFlight,
  switchSteps,
  useEnginePolling,
  useEngineSnapshot,
} from "../engineState";
import { OpsCard } from "./OpsCard";

const ENGINES: EngineId[] = ["standard", "flash-next"];
const DASH = "—";

function fmtGb(v: number | null | undefined): string {
  return v == null ? DASH : `${v.toFixed(1)} GB`;
}

function elapsed(fromIso: string, toIso?: string | null): string {
  const from = new Date(fromIso).getTime();
  const to = toIso ? new Date(toIso).getTime() : Date.now();
  if (Number.isNaN(from) || Number.isNaN(to)) return "";
  const s = Math.max(0, Math.round((to - from) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function oneshotText(oneshot: string): string {
  if (oneshot === "update")
    return "A server update is running — the switch waits until it finishes.";
  if (oneshot === "perplexity")
    return "The perplexity test (a debug one-shot) is running on the engine — the switch waits until it finishes.";
  return `A supervisor one-shot (${oneshot}) is running — the switch waits until it finishes.`;
}

type Blocked = { kind: "oneshot" | "install" | "missing"; text: string };

/** Why switching TO `to` cannot start right now, before the server is even asked. */
export function blockedReason(s: EngineState, to: EngineId): Blocked | null {
  if (s.oneshot !== null) return { kind: "oneshot", text: oneshotText(s.oneshot) };
  if (!s.installed[to])
    return {
      kind: "install",
      text: `${ENGINE_LABEL[to]} isn't installed — its weights aren't on the box.`,
    };
  if (s.services[to]?.state === "missing")
    return {
      kind: "missing",
      text: `${ENGINE_LABEL[to]} has no container yet — Ops → Update creates it once its weights are installed.`,
    };
  return null;
}

function logLines(sw: EngineSwitchStatus): string[] {
  const lines = sw.stages.map(
    (st) =>
      `${hhmmss(st.at) ?? ""} ${st.stage.replace("_", " ")}${st.stage === "draining" ? ` · switch → ${sw.target} (${sw.source}${sw.force ? ", forced" : ""})` : ""}`,
  );
  for (const sm of sw.smoke)
    lines.push(
      `smoke ${sm.probe}: ${sm.ok ? "ok" : "FAILED"}${sm.detail ? ` — ${sm.detail}` : ""}`,
    );
  for (const n of sw.notes) lines.push(`note  ${n}`);
  if (sw.reason) lines.push(`reason  ${sw.reason}`);
  return lines;
}

function SmokeChips({ sw }: { sw: EngineSwitchStatus }) {
  return (
    <div className="engine-checks">
      {sw.smoke.map((sm) => (
        <span key={sm.probe} className={`engine-chk ${sm.ok ? "ok" : "err"}`} title={sm.detail}>
          {sm.ok ? "✓" : "✕"} {sm.probe}
        </span>
      ))}
    </div>
  );
}

function Progress({
  sw,
  state,
  cancel,
}: {
  sw: EngineSwitchStatus;
  state: EngineState;
  /** Present only while the server is believed to offer a cancel route. */
  cancel: { run: () => void; busy: boolean } | null;
}) {
  const steps = switchSteps(sw.previous, sw.target, sw.model);
  const at = new Map(sw.stages.map((x) => [x.stage, x.at]));
  // A terminal stage can show for a beat while the api still holds its lock (it waits out
  // the other processes' engine caches before releasing): every step is then behind it.
  const found = steps.findIndex((x) => x.stage === sw.stage);
  const cur = found < 0 ? steps.length : found;
  const step = steps[cur];
  const detail: Record<string, string> = {
    draining: "calls in flight finish first · new local calls wait",
    stopping: `unloading ${ENGINE_LABEL[sw.previous]} · GTT ${fmtGb(state.memory.gtt_used_gb)}`,
    starting: `waiting for ${ENGINE_LABEL[sw.target]} to report running`,
    loading: sw.model ? `loading ${sw.model}` : "loading the test model",
    smoke: "text · tool call · image",
  };
  return (
    <div className="engine-prog">
      <div className="engine-prog-head">
        <span className="engine-dot warn live" aria-hidden="true" />
        {/* Only the headline is live: the elapsed clock and the step list change on every
            poll, and announcing them would drown the one change that matters. */}
        <output className="engine-prog-title">
          {step ? `Step ${cur + 1} of ${steps.length} · ${step.label}` : "Finishing up"}
        </output>
        <span className="engine-prog-el">{elapsed(sw.started_at)}</span>
      </div>
      <p className="engine-prog-detail">{detail[sw.stage] ?? sw.stage}</p>
      {sw.stage === "smoke" && sw.smoke.length > 0 && <SmokeChips sw={sw} />}
      <ol className="engine-steps">
        {steps.map((x, i) => {
          const cls = i < cur ? "done" : i === cur ? "run" : "pending";
          return (
            <li key={x.stage} className={cls}>
              <span className="engine-si" aria-hidden="true">
                {cls === "done" ? "✓" : cls === "run" ? "●" : "○"}
              </span>
              {x.label}
              <span className="engine-sd">{hhmmss(at.get(x.stage)) ?? ""}</span>
            </li>
          );
        })}
      </ol>
      {sw.stage === "draining" && cancel && (
        <button
          type="button"
          className="engine-btn ghost engine-cancel"
          disabled={cancel.busy}
          onClick={cancel.run}
        >
          {cancel.busy ? "Cancelling…" : "Cancel — nothing has stopped yet"}
        </button>
      )}
    </div>
  );
}

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  } catch {
    return false;
  }
}

export function LocalEngineCard() {
  useEnginePolling();
  const snap = useEngineSnapshot();
  const s = snap.state;
  const armed = snap.armed;
  const [open, setOpen] = useState(false);
  const [posting, setPosting] = useState(false);
  const [postError, setPostError] = useState<string | null>(null);
  // The switch this card started, so its success is announced here once — an old `done`
  // from days ago is not news, but an old rollback is (until dismissed).
  const [watched, setWatched] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // The guard text the owner explicitly acknowledged. Bound to the text, so a different
  // reason appearing later needs its own acknowledgement.
  const [ackedGuard, setAckedGuard] = useState<string | null>(null);
  const [cancelUnsupported, setCancelUnsupported] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // A double tap must not send two POSTs: state updates land a render too late to stop it.
  const starting = useRef(false);
  const lastEffective = useRef<EngineId | null>(null);

  const inFlight = switchInFlight(s);
  const sw = s?.switch ?? null;
  const failed =
    sw !== null &&
    (sw.stage === "rolled_back" || sw.stage === "failed") &&
    !snap.dismissed.has(sw.id);
  const fallback = s !== null && !inFlight && s.desired !== s.effective;
  const attention = inFlight || failed || fallback || armed !== null;

  useEffect(() => {
    if (!attention) return;
    setOpen(true);
    if (armed !== null)
      ref.current?.scrollIntoView?.({
        block: "start",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
  }, [attention, armed]);

  // A confirm armed against one engine must not survive that engine changing under it: its
  // stated consequence ("Standard stops…") would be describing a box that no longer exists.
  const effective = s?.effective ?? null;
  useEffect(() => {
    const prev = lastEffective.current;
    lastEffective.current = effective;
    if (prev !== null && effective !== null && prev !== effective) armEngineSwitch(null);
  }, [effective]);

  // Each arming starts unacknowledged.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on every new arm
  useEffect(() => setAckedGuard(null), [armed]);

  useEffect(holdEngineArm, []);

  async function start(engine: EngineId, force: boolean) {
    if (starting.current) return;
    starting.current = true;
    setPosting(true);
    setPostError(null);
    try {
      const status = await api.switchEngine(engine, force);
      armEngineSwitch(null);
      setWatched(status.id);
      if (s !== null) {
        setEngineState({
          ...s,
          switch: status,
          switching: !["done", "rolled_back", "failed"].includes(status.stage),
        });
      }
      kickEnginePoll();
    } catch (err) {
      setPostError(err instanceof Error ? err.message : String(err));
    } finally {
      starting.current = false;
      setPosting(false);
    }
  }

  async function cancelSwitch() {
    setCancelling(true);
    setPostError(null);
    try {
      await api.cancelEngineSwitch();
      kickEnginePoll();
    } catch (err) {
      if (err instanceof EngineCancelUnsupported) setCancelUnsupported(true);
      setPostError(err instanceof Error ? err.message : String(err));
    } finally {
      setCancelling(false);
    }
  }

  function arm(e: EngineId) {
    if (s === null || inFlight) return;
    setPostError(null);
    armEngineSwitch(e === s.effective && s.desired === s.effective ? null : e);
  }

  let summary = "checking…";
  let summaryCls = "";
  if (s === null) {
    if (snap.error) summary = "unavailable";
  } else if (inFlight && sw) {
    const steps = switchSteps(sw.previous, sw.target, sw.model);
    const n = steps.findIndex((x) => x.stage === sw.stage);
    summary = n < 0 ? "switching · finishing" : `switching · step ${n + 1} of ${steps.length}`;
    summaryCls = " warn";
  } else if (failed) {
    summary = `rolled back · ${ENGINE_LABEL[s.effective]}`;
    summaryCls = " bad";
  } else if (fallback) {
    summary = `fallback · ${ENGINE_LABEL[s.effective]}`;
    summaryCls = " warn";
  } else {
    summary = `${ENGINE_LABEL[s.effective]}${s.consistent ? "" : " · not up"}`;
    if (!s.consistent) summaryCls = " warn";
  }

  return (
    <div ref={ref} className="engine-anchor">
      <OpsCard
        title="Local engine"
        open={open}
        onToggle={setOpen}
        summaryCollapsed={<span className={`ops-card-summary${summaryCls}`}>{summary}</span>}
      >
        {s === null ? (
          <p className="muted ops-vrow-empty">{snap.error ?? "Reading the engine…"}</p>
        ) : (
          <EngineBody
            s={s}
            stale={
              snap.error !== null && snap.lastOk !== null
                ? (hhmm(new Date(snap.lastOk).toISOString()) ?? DASH)
                : null
            }
            ackedGuard={ackedGuard}
            onAckGuard={setAckedGuard}
            cancel={cancelUnsupported ? null : { run: () => void cancelSwitch(), busy: cancelling }}
            armed={armed}
            inFlight={inFlight}
            failed={failed}
            fallback={fallback}
            posting={posting}
            postError={postError}
            watched={watched}
            copied={copied}
            onArm={arm}
            onDisarm={() => armEngineSwitch(null)}
            onStart={(e, force) => void start(e, force)}
            onCopy={(text) => {
              void navigator.clipboard?.writeText(text).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              });
            }}
          />
        )}
      </OpsCard>
    </div>
  );
}

function EngineBody({
  s,
  stale,
  ackedGuard,
  onAckGuard,
  cancel,
  armed,
  inFlight,
  failed,
  fallback,
  posting,
  postError,
  watched,
  copied,
  onArm,
  onDisarm,
  onStart,
  onCopy,
}: {
  s: EngineState;
  /** "HH:MM" of the last good read while the latest read failed, else null. */
  stale: string | null;
  ackedGuard: string | null;
  onAckGuard: (guard: string | null) => void;
  cancel: { run: () => void; busy: boolean } | null;
  armed: EngineId | null;
  inFlight: boolean;
  failed: boolean;
  fallback: boolean;
  posting: boolean;
  postError: string | null;
  watched: string | null;
  copied: boolean;
  onArm: (e: EngineId) => void;
  onDisarm: () => void;
  onStart: (e: EngineId, force: boolean) => void;
  onCopy: (text: string) => void;
}) {
  const sw = s.switch;
  const other = OTHER_ENGINE[s.effective];
  const blocked = inFlight ? null : blockedReason(s, other);

  function segSub(e: EngineId): string {
    if (!s.installed[e]) return "not installed";
    if (inFlight && sw?.target === e) return "starting";
    if (inFlight && sw?.previous === e) return "stopping";
    if (!inFlight && s.effective === e) return s.running.includes(e) ? "serving" : "not up";
    if (s.desired === e && s.desired !== s.effective) return "selected · not serving";
    return e === "flash-next" ? "one model · own container" : "gateway · several models";
  }

  const logs = sw ? logLines(sw) : [];
  const gttPct =
    s.memory.gtt_used_gb != null && s.memory.gtt_total_gb
      ? Math.min(100, (s.memory.gtt_used_gb / s.memory.gtt_total_gb) * 100)
      : null;
  const hostFree =
    s.memory.host_total_gb != null && s.memory.host_used_gb != null
      ? s.memory.host_total_gb - s.memory.host_used_gb
      : null;
  const since = activeSince(s);

  let engineHead: string;
  let engineSub: string;
  let engineWarn = false;
  if (inFlight && sw) {
    engineHead = `Switching to ${ENGINE_LABEL[sw.target]}`;
    engineSub = `${ENGINE_LABEL[sw.previous]} → ${ENGINE_LABEL[sw.target]}`;
    engineWarn = true;
  } else if (fallback) {
    engineHead = `${ENGINE_LABEL[s.desired]} selected · ${ENGINE_LABEL[s.effective]} serving`;
    engineSub = `fallback: ${s.fallback_reason ?? `${ENGINE_LABEL[s.desired]} could not be started`}`;
    engineWarn = true;
  } else if (!s.consistent) {
    engineHead = `${ENGINE_LABEL[s.effective]} recorded`;
    engineSub =
      s.running.length === 0
        ? "no engine is up"
        : `up: ${s.running.map((e) => ENGINE_LABEL[e]).join(", ")}`;
    engineWarn = true;
  } else {
    engineHead = `${ENGINE_LABEL[s.effective]} serving`;
    engineSub = since ? `since ${since}` : (s.services[s.effective]?.service ?? "");
  }

  return (
    <>
      <div className="engine-body">
        {stale !== null && (
          <output className="engine-stale">Can't reach the engine · last read {stale}</output>
        )}
        <fieldset className="engine-seg" aria-label="Local engine">
          {ENGINES.map((e) => {
            const isEff = !inFlight && s.effective === e;
            const isTarget = inFlight && sw?.target === e;
            const wanted = !inFlight && s.desired === e && s.desired !== s.effective;
            const segBlocked = e !== s.effective && blockedReason(s, e) !== null;
            const cls = [
              armed === e ? "armed" : "",
              isTarget ? "target" : "",
              wanted ? "wanted" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <button
                key={e}
                type="button"
                aria-pressed={isEff}
                className={cls}
                disabled={inFlight || posting || segBlocked}
                onClick={() => onArm(e)}
              >
                <span className="engine-sn">
                  {(isEff || isTarget || wanted) && (
                    <span
                      className={`engine-dot${isEff ? " on" : " warn"}${isTarget ? " live" : ""}`}
                      aria-hidden="true"
                    />
                  )}
                  {ENGINE_LABEL[e]}
                </span>
                <span className="engine-ss">{segSub(e)}</span>
              </button>
            );
          })}
        </fieldset>

        {armed !== null && !inFlight && (
          <Confirm
            s={s}
            to={armed}
            posting={posting}
            acked={s.guard !== null && ackedGuard === s.guard}
            onAck={(on) => onAckGuard(on ? s.guard : null)}
            onCancel={onDisarm}
            onStart={onStart}
          />
        )}

        {inFlight && sw && <Progress sw={sw} state={s} cancel={cancel} />}

        {!inFlight && sw?.stage === "done" && watched === sw.id && (
          <div className="engine-notice ok">
            {ENGINE_LABEL[sw.target]} is serving{" "}
            <span>
              — {sw.smoke.length > 0 ? "smoke test passed" : "smoke test skipped (see the log)"},
              switched in {elapsed(sw.started_at, sw.ended_at)} at {hhmm(sw.ended_at) ?? DASH}.
            </span>
          </div>
        )}

        {!inFlight && failed && sw && (
          <div className="engine-notice err" role="alert">
            {sw.stage === "rolled_back" ? (
              <>
                Switch to {ENGINE_LABEL[sw.target]} failed — <b>rolled back</b>;{" "}
                {ENGINE_LABEL[s.effective]} is serving again.
              </>
            ) : (
              <>
                Switch to {ENGINE_LABEL[sw.target]} <b>failed</b> and could not be rolled back
                cleanly.
              </>
            )}
            {sw.reason && <span className="engine-reason">Reason: {sw.reason}</span>}
            <div className="engine-acts">
              {blockedReason(s, sw.target) === null && sw.target !== s.effective && (
                <button
                  type="button"
                  className="engine-btn secondary"
                  onClick={() => onArm(sw.target)}
                >
                  Try again
                </button>
              )}
              <button
                type="button"
                className="engine-btn ghost"
                onClick={() => dismissEngineSwitch(sw.id)}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {fallback && (
          <div className="engine-notice warn">
            {ENGINE_LABEL[s.desired]} selected · {ENGINE_LABEL[s.effective]} serving{" "}
            <span>
              — the last start fell back to {ENGINE_LABEL[s.effective]}
              {s.fallback_reason ? `: ${s.fallback_reason}` : ""}. The next update tries{" "}
              {ENGINE_LABEL[s.desired]} again.
            </span>
            <div className="engine-acts">
              <button
                type="button"
                className="engine-btn secondary"
                disabled={posting || blockedReason(s, s.desired) !== null}
                onClick={() => onArm(s.desired)}
              >
                Retry now
              </button>
              {/* Through the confirm like any other change. "Keep" only when nothing would stop
                  or reload; on a box that is not cleanly on one engine it IS a switch, and is
                  named and confirmed as one. */}
              <button
                type="button"
                className="engine-btn ghost"
                disabled={posting || s.oneshot !== null}
                onClick={() => onArm(s.effective)}
              >
                {s.consistent
                  ? `Keep ${ENGINE_LABEL[s.effective]}`
                  : `Switch to ${ENGINE_LABEL[s.effective]}`}
              </button>
            </div>
          </div>
        )}

        {blocked && (
          <div className="engine-notice off">
            {blocked.text}{" "}
            {blocked.kind === "install" && (
              <button type="button" className="engine-link" onClick={openOnBoxModels}>
                Install in On-box models
              </button>
            )}
          </div>
        )}

        {postError && (
          <p className="error" role="alert">
            {postError}
          </p>
        )}
      </div>

      <div className="ops-vrow">
        <span className="ops-vk">Engine</span>
        <div className="ops-vmid">
          <span className="ops-vv">{engineHead}</span>
          <span className={`ops-vsub${engineWarn ? " warn" : ""}`}>{engineSub}</span>
        </div>
      </div>
      <div className="ops-vrow">
        <span className="ops-vk">Memory</span>
        <div className="ops-vmid">
          <div className="ops-vline">
            <span className="ops-vv">
              {fmtGb(s.memory.gtt_used_gb)} <small>GTT used / {fmtGb(s.memory.gtt_total_gb)}</small>
            </span>
            <span className="ops-vextra">host free {fmtGb(hostFree)}</span>
          </div>
          {gttPct !== null && (
            <div className="meter meter-resource">
              <div className="meter-empty" style={{ width: `${100 - gttPct}%` }} />
            </div>
          )}
        </div>
      </div>
      <div className="ops-vrow">
        <span className="ops-vk">Decode</span>
        <div className="ops-vmid">
          <span className="ops-vv">
            {!inFlight && typeof s.decode_tps === "number"
              ? `${s.decode_tps.toFixed(1)} tok/s`
              : DASH}
          </span>
          <span className="ops-vsub">
            {inFlight
              ? "no engine serving right now"
              : typeof s.decode_tps === "number"
                ? "last local turn"
                : "the engine reports no tok/s reading yet"}
          </span>
        </div>
      </div>
      <div className="ops-vrow">
        <span className="ops-vk">Last smoke</span>
        <div className="ops-vmid">
          {sw && sw.smoke.length > 0 ? (
            <>
              <span className="ops-vv">
                {sw.smoke.every((x) => x.ok) ? "passed" : "failed"}{" "}
                <small>
                  · {ENGINE_LABEL[sw.target]}
                  {sw.model ? ` · ${sw.model}` : ""} · {hhmm(sw.ended_at ?? sw.updated_at) ?? DASH}
                </small>
              </span>
              <SmokeChips sw={sw} />
            </>
          ) : (
            <span className="ops-vv">{DASH}</span>
          )}
        </div>
      </div>
      <div className="ops-vrow engine-logrow">
        <div className="engine-loghead">
          <span className="ops-vk">Engine log</span>
          {logs.length > 0 && (
            <button
              type="button"
              className="engine-btn ghost"
              onClick={() => onCopy(logs.join("\n"))}
            >
              {copied ? "Copied" : "Copy"}
            </button>
          )}
        </div>
        {logs.length > 0 ? (
          <pre className="ops-update-log engine-log">{logs.slice(-8).join("\n")}</pre>
        ) : (
          <span className="muted">No switch recorded yet.</span>
        )}
      </div>
    </>
  );
}

function Confirm({
  s,
  to,
  posting,
  acked,
  onAck,
  onCancel,
  onStart,
}: {
  s: EngineState;
  to: EngineId;
  posting: boolean;
  acked: boolean;
  onAck: (on: boolean) => void;
  onCancel: () => void;
  onStart: (e: EngineId, force: boolean) => void;
}) {
  const eff = s.effective;
  // Keeping the engine already up alone stops and reloads nothing; anything else is a switch.
  const keep = to === eff && s.consistent;
  const others = s.running.filter((e) => e !== to).map((e) => ENGINE_LABEL[e]);
  const guard = s.guard;
  return (
    <section className="engine-confirm" aria-label="Confirm engine switch">
      {keep ? (
        <>
          <div className="engine-confirm-title">Keep {ENGINE_LABEL[to]}?</div>
          <p>
            {ENGINE_LABEL[to]} is already the only engine up, so <b>nothing stops or reloads</b> —
            this records {ENGINE_LABEL[to]} as your choice, and the next update stops trying{" "}
            {ENGINE_LABEL[s.desired]}.
          </p>
        </>
      ) : (
        <>
          <div className="engine-confirm-title">Switch to {ENGINE_LABEL[to]}?</div>
          <p>
            Local AI pauses for <b>a few minutes</b> — calls in flight finish first (up to a
            minute), {others.length > 0 ? `${others.join(" and ")} stops` : "nothing else is up"},{" "}
            {ENGINE_LABEL[to]} loads and is smoke-tested (text · tool call · image).{" "}
            {to === "flash-next"
              ? "Every local per-task pick runs on Flash-Next until you switch back."
              : "Your per-task picks go back to their own models."}{" "}
            {to !== eff
              ? `If a check fails it rolls back to ${ENGINE_LABEL[eff]} on its own.`
              : "If a check fails it is stopped again and the reason shown here."}
          </p>
        </>
      )}
      {guard !== null && (
        <>
          <p className="engine-guard">
            Not now: {guard}. {keep ? "" : "Switching stops every local model for minutes. "}
            Going ahead overrides that.
          </p>
          <label className="engine-ack">
            <input
              type="checkbox"
              checked={acked}
              onChange={(e) => onAck(e.currentTarget.checked)}
            />
            I understand — nightly jobs may fail
          </label>
        </>
      )}
      <div className="engine-acts">
        <button type="button" className="engine-btn secondary" onClick={onCancel}>
          Cancel
        </button>
        {/* One button, never swapped for another under the finger: when a guard appears it
            turns into a DISABLED "… anyway" until the box above is ticked, and only then
            sends force. */}
        <button
          type="button"
          className={`engine-btn ${guard === null ? "primary" : "danger"}`}
          disabled={posting || (guard !== null && !acked)}
          onClick={() => onStart(to, guard !== null)}
        >
          {posting
            ? "Starting…"
            : `${keep ? `Keep ${ENGINE_LABEL[to]}` : "Switch"}${guard !== null ? " anyway" : ""}`}
        </button>
      </div>
    </section>
  );
}
