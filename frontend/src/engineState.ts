// The on-box engine's state, shared by every surface that shows it.
//
// One store and one poller, because two surfaces read the same fact at once: the global
// engine banner (under every top bar) and the Ops "Local engine" card. Each polling on its
// own would double the supervisor reads during a switch, and worse, could disagree for a
// beat about whether a switch is running. The banner never fetches — it only subscribes —
// so a screen rendered in isolation (a test, a share app) costs no request.
//
// Binding mock: docs/mocks/engine-switch/a-segmented-toggle.html.

import { useEffect, useSyncExternalStore } from "react";
import {
  ApiError,
  type EngineId,
  type EngineState,
  type EngineSwitchStage,
  api,
} from "./api/client";
import { isForeground, onForegroundSignals } from "./visibility";

export const ENGINE_LABEL: Record<EngineId, string> = {
  standard: "Standard",
  "flash-next": "Flash-Next",
};

export const OTHER_ENGINE: Record<EngineId, EngineId> = {
  standard: "flash-next",
  "flash-next": "standard",
};

const TERMINAL = new Set<EngineSwitchStage>(["done", "rolled_back", "failed", "cancelled"]);

/** Whether a switch stage is an ending. The ONE definition every surface uses: a stage
 *  missing here reads as "still switching" forever — a 2 s poll, an amber banner and a
 *  card that cannot be re-armed. */
export function isTerminal(stage: EngineSwitchStage): boolean {
  return TERMINAL.has(stage);
}

/** A switch left NO local engine up, and that is still true now. The flag stays on the last
 *  switch record after a later update brings an engine back, so it is read against what is
 *  actually running. */
export function noEngineUp(state: EngineState | null): boolean {
  return (
    state !== null &&
    state.switch?.no_engine_up === true &&
    !switchInFlight(state) &&
    state.running.length === 0
  );
}

/** While a switch runs the stages turn over in seconds; idle, the engine changes only when
 *  someone switches it, so a slow beat keeps the banner honest without load. */
export const ENGINE_POLL_FAST_MS = 2000;
export const ENGINE_POLL_IDLE_MS = 30000;
/** A failed read retries at 2 s, doubling to the idle beat — fast enough to notice the api
 *  coming back after a restart, without hammering one that stays down. */
export const ENGINE_BACKOFF_FIRST_MS = 2000;

export interface EngineSnapshot {
  state: EngineState | null;
  /** The last read's failure, or null. A failed read keeps the previous state. */
  error: string | null;
  /** Epoch ms of the last successful read, so a stale state can say how stale it is. */
  lastOk: number | null;
  /** The engine the owner asked to switch to from elsewhere (the banner's "Switch back"),
   *  which the Ops card picks up as an armed confirm. */
  armed: EngineId | null;
  /** Switch ids whose rollback/failure the owner dismissed. */
  dismissed: ReadonlySet<string>;
  /** Asked to show the engine (the banner's "Details"): Ops opens its Engine page and clears it. */
  focus: boolean;
}

const DISMISS_KEY = "jbrain.engine.dismissed";

function loadDismissed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(DISMISS_KEY);
    const ids = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(ids) ? ids.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

let snapshot: EngineSnapshot = {
  state: null,
  error: null,
  lastOk: null,
  armed: null,
  dismissed: loadDismissed(),
  focus: false,
};
const listeners = new Set<() => void>();

function set(patch: Partial<EngineSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  for (const l of listeners) l();
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** The current snapshot outside React (tests, imperative callers). */
export function peekEngineSnapshot(): EngineSnapshot {
  return snapshot;
}

export function useEngineSnapshot(): EngineSnapshot {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** True while a switch is in flight — read off both signals, since `switching` is the api
 *  process's own lock and the stage is what any process recorded. */
export function switchInFlight(state: EngineState | null): boolean {
  if (state === null) return false;
  return state.switching || (state.switch !== null && !isTerminal(state.switch.stage));
}

// A 403 means this principal has no engine surface; polling it would only fill the log. It
// is latched until the next foreground signal, not forever — a session can be re-authorised
// underneath a running app. A 404 is NOT latched: during an update the api is briefly an
// older or half-started build, and treating that as "no such feature" would silence the
// banner for good on exactly the occasion it matters.
let forbidden = false;
let failures = 0;
let inflight: Promise<void> | null = null;

/** Read the engine state now. Concurrent callers share one request. */
export function refreshEngine(): Promise<void> {
  inflight ??= (async () => {
    try {
      const state = await api.getEngineState();
      failures = 0;
      set({ state, error: null, lastOk: Date.now() });
    } catch (err) {
      failures += 1;
      if (err instanceof ApiError && err.status === 403) forbidden = true;
      set({ error: err instanceof Error ? err.message : String(err) });
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

let pollers = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let stopSignals: (() => void) | null = null;

/** How long until the next read: backing off after failures, else the state's own pace. */
export function nextDelay(): number {
  if (failures > 0)
    return Math.min(ENGINE_BACKOFF_FIRST_MS * 2 ** (failures - 1), ENGINE_POLL_IDLE_MS);
  return switchInFlight(snapshot.state) ? ENGINE_POLL_FAST_MS : ENGINE_POLL_IDLE_MS;
}

function schedule(delay?: number): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
  if (pollers === 0 || forbidden) return;
  const wait = delay ?? nextDelay();
  timer = setTimeout(() => {
    timer = null;
    if (!isForeground()) return; // resumes on the next foreground signal
    void refreshEngine().then(() => schedule());
  }, wait);
}

/** Re-arm the poll at the pace the current state wants — called after a POST so the first
 *  stage shows within two seconds rather than the idle beat. */
export function kickEnginePoll(): void {
  schedule(0);
}

/** Keep the store polled while the calling component is mounted. Reference-counted, so the
 *  shell and the Ops card together still make one request per beat. */
export function useEnginePolling(enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    pollers += 1;
    if (pollers === 1) {
      stopSignals = onForegroundSignals(() => {
        if (!isForeground()) return;
        forbidden = false;
        schedule(0);
      });
    }
    schedule(0);
    return () => {
      pollers -= 1;
      if (pollers === 0) {
        if (timer !== null) clearTimeout(timer);
        timer = null;
        stopSignals?.();
        stopSignals = null;
      }
    };
  }, [enabled]);
}

export function armEngineSwitch(engine: EngineId | null): void {
  set({ armed: engine });
}

let disarmTimer: ReturnType<typeof setTimeout> | null = null;

/** The card's mount/unmount pair for the armed confirm: an armed switch belongs to one visit
 *  to Ops, so leaving drops it — but deferred a tick, because StrictMode unmounts and
 *  remounts every effect once in dev, and a synchronous clear there would eat the arm the
 *  banner's "Switch back" just set. */
export function holdEngineArm(): () => void {
  if (disarmTimer !== null) clearTimeout(disarmTimer);
  disarmTimer = null;
  return () => {
    disarmTimer = setTimeout(() => {
      disarmTimer = null;
      armEngineSwitch(null);
    }, 0);
  };
}

export function dismissEngineSwitch(id: string): void {
  const next = new Set(snapshot.dismissed);
  next.add(id);
  try {
    window.localStorage.setItem(DISMISS_KEY, JSON.stringify([...next].slice(-20)));
  } catch {
    // Private mode: the dismissal holds for this app-open only.
  }
  set({ dismissed: next });
}

export interface EngineNavigator {
  /** Open the Ops screen (where the Engine page lives). */
  ops: () => void;
  /** Open LLM settings' On-box models, where Flash-Next's weights are installed. */
  models: () => void;
}

let navigator_: EngineNavigator | null = null;

/** The shell registers how to reach Ops and On-box models; the banner and the card are
 *  rendered far from the router and have no other way there. */
export function setEngineNavigator(nav: EngineNavigator | null): void {
  navigator_ = nav;
}

export function openEngineCard(arm?: EngineId): void {
  if (arm !== undefined) armEngineSwitch(arm);
  set({ focus: true });
  navigator_?.ops();
}

/** Ops took the focus request and opened its Engine page. */
export function clearEngineFocus(): void {
  if (snapshot.focus) set({ focus: false });
}

export function openOnBoxModels(): void {
  navigator_?.models();
}

/** Reset every module-level piece (tests only). */
export function resetEngineStore(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
  if (disarmTimer !== null) clearTimeout(disarmTimer);
  disarmTimer = null;
  pollers = 0;
  stopSignals?.();
  stopSignals = null;
  forbidden = false;
  failures = 0;
  inflight = null;
  navigator_ = null;
  snapshot = {
    state: null,
    error: null,
    lastOk: null,
    armed: null,
    dismissed: new Set(),
    focus: false,
  };
  for (const l of listeners) l();
}

/** Seed the store directly (tests, and the card after a POST answers). */
export function setEngineState(state: EngineState | null): void {
  set({ state, error: null, lastOk: Date.now() });
}

/** `HH:MM` local time of an ISO timestamp, or null when absent/unparseable. */
export function hhmm(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function hhmmss(iso: string | null | undefined): string | null {
  const base = hhmm(iso);
  if (base === null || !iso) return null;
  return `${base}:${String(new Date(iso).getSeconds()).padStart(2, "0")}`;
}

/** The forward steps of a switch, in order, with the owner's label for each. */
export function switchSteps(
  from: EngineId,
  to: EngineId,
  model: string | null,
): { stage: EngineSwitchStage; label: string }[] {
  return [
    { stage: "draining", label: "Drain local calls" },
    { stage: "stopping", label: `Stop ${ENGINE_LABEL[from]}` },
    { stage: "starting", label: `Start ${ENGINE_LABEL[to]}` },
    { stage: "loading", label: model ? `Load ${model}` : "Load the test model" },
    { stage: "smoke", label: "Smoke test" },
  ];
}

/** When Flash-Next became the serving engine, if the last switch says so. */
export function activeSince(state: EngineState): string | null {
  // The server's own record wins when it has one; the last switch is the fallback.
  if (state.effective_since) return hhmm(state.effective_since);
  const sw = state.switch;
  if (sw && sw.stage === "done" && sw.target === state.effective) return hhmm(sw.ended_at);
  return null;
}
