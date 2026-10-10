// The Minecraft server screen — a card-launcher destination, also reached from Ops'
// shortcut row (DESIGN.md "Minecraft server screen"; binding mock
// docs/mocks/minecraft-ops/b-dedicated-screen.html, with C's player table in Players).
//
// Everything shown is what the box reported. Before the first install nothing is claimed:
// no version, world or join details, and no progress figure the API doesn't give.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type MinecraftPlayer,
  type MinecraftPlayers,
  type MinecraftServer,
  type MinecraftStatus,
  type MinecraftUpdate,
  type MinecraftVersion,
  api,
} from "../api/client";
import { Dialog } from "../components/Dialog";
import { Sheet } from "../components/Sheet";
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronRightIcon,
  DownloadIcon,
  ExternalLinkIcon,
  PlayIcon,
  PuzzleIcon,
  RefreshIcon,
  StopIcon,
  XIcon,
} from "../components/icons";
import {
  type ConfirmKind,
  type McState,
  agoOf,
  clockOf,
  confirmContext,
  confirmFor,
  dayOf,
  fmtDur,
  isBehind,
  marketingNumber,
  plural,
  serverState,
  shortDate,
  stateInfo,
  updateRunning,
} from "../minecraft";
import { useForeground } from "../visibility";
import { AllowlistSection, ServerSettingsSection } from "./MinecraftServerSettings";
import { WorldModals } from "./MinecraftWorldSheets";
import {
  ErrLine,
  RulesLayer,
  WorldLayer,
  WorldsEntry,
  WorldsLayer,
  WorldsProvider,
  useWorlds,
} from "./MinecraftWorlds";

const STATUS_POLL_MS = 5000;
/** While a world job runs its phase moves every few seconds, and the card should keep up. */
const JOB_POLL_MS = 2000;
const CLOCK_TICK_MS = 15_000;
/** The roster's slow beat, and the re-read after a join or leave: the session drain writes
 *  a join up to 5 s after the server reports it, so the immediate read can miss a newcomer. */
const PLAYERS_POLL_MS = 15_000;
const PLAYERS_SETTLE_MS = 6000;
const TOAST_MS = 3200;
/** A finished update stays on screen this long, then the section settles back. */
const DONE_SHOWN_S = 5 * 60;

const ADDON_HEAD = "Arrives with the companion add-on.";
const ADDON_REST =
  "Bedrock keeps no player statistics, so these are counted in-game by the add-on once it is installed — nothing is counted yet.";
const LEADER_ROWS = [
  "Most mobs killed",
  "Most blocks mined",
  "Most blocks placed",
  "Furthest travelled",
  "Most deaths",
];

const NOT_INSTALLED: McState[] = ["installing", "install_failed", "absent"];

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

const initial = (tag: string) => (tag[0] ?? "?").toUpperCase();

export function MinecraftScreen() {
  const foreground = useForeground();
  const [status, setStatus] = useState<MinecraftStatus | null>(null);
  const [statusSeq, setStatusSeq] = useState(0);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [version, setVersion] = useState<MinecraftVersion | null>(null);
  const [checking, setChecking] = useState(false);
  const [players, setPlayers] = useState<MinecraftPlayers | null>(null);
  const [playersAt, setPlayersAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  // A Restart reads as one act through the server's own stopping → stopped → starting, until
  // it is seen running again; `restartInFlight` covers the request before the first of those.
  const [restartPending, setRestartPending] = useState(false);
  // One lifecycle or update request at a time: every act is disabled until it answers, so a
  // Stop can't be chased by an Update before the screen has even seen it stopping.
  const [inFlight, setInFlight] = useState<
    "start" | "stop" | "restart" | "update" | "retry" | null
  >(null);
  const restartInFlight = inFlight === "restart";
  const autoInFlight = useRef(false);
  const restartSeenDown = useRef(false);
  const [pending, setPending] = useState<ConfirmKind | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [sheetXuid, setSheetXuid] = useState<string | null>(null);
  const [dismissedFailure, setDismissedFailure] = useState<number | null>(null);
  const [updOrigin, setUpdOrigin] = useState<{ at: number; stopped: boolean } | null>(null);
  const [autoUpdate, setAutoUpdate] = useState<boolean | null>(null);
  const stoppedIntent = useRef<boolean | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const next = await api.minecraftStatus();
      setStatus(next);
      setStatusSeq((n) => n + 1);
      setStatusError(null);
    } catch (err) {
      setStatusError(errorMessage(err));
    }
  }, []);

  const loadVersion = useCallback(async (refresh: boolean) => {
    try {
      setVersion(await api.minecraftVersion(refresh));
    } catch (err) {
      if (refresh) setActionError(`Couldn't check for updates — ${errorMessage(err)}`);
    }
  }, []);

  const loadPlayers = useCallback(async () => {
    try {
      const next = await api.minecraftPlayers();
      setPlayers(next);
      setPlayersAt(Date.now());
    } catch {
      // The roster is a nice-to-have beside the live status; keep the last one.
    }
  }, []);

  useEffect(() => {
    void loadVersion(false);
  }, [loadVersion]);

  const server = status?.server ?? null;
  const job = server?.job ?? null;
  // This device's own upload or long request: poll fast so its job shows at once.
  const [localBusy, setLocalBusy] = useState(false);
  const fast = job !== null || localBusy;

  // The screen's heartbeat: status every few seconds while it can be seen, and the clock
  // that keeps session timers honest between polls. A job's elapsed time ticks by the second.
  useEffect(() => {
    if (!foreground) return;
    void loadStatus();
    const poll = setInterval(() => void loadStatus(), fast ? JOB_POLL_MS : STATUS_POLL_MS);
    const tick = setInterval(() => setNow(Date.now()), fast ? 1000 : CLOCK_TICK_MS);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [foreground, loadStatus, fast]);

  const update = server?.update ?? null;
  const onlineKey = (server?.players ?? []).map((p) => p.xuid).join(",");

  // Totals only move when someone joins or leaves; between those the live session is
  // added client-side.
  // biome-ignore lint/correctness/useExhaustiveDependencies: refetch exactly when the online set changes.
  useEffect(() => {
    void loadPlayers();
    const settle = setTimeout(() => void loadPlayers(), PLAYERS_SETTLE_MS);
    return () => clearTimeout(settle);
  }, [onlineKey, loadPlayers]);

  useEffect(() => {
    if (!foreground) return;
    const id = setInterval(() => void loadPlayers(), PLAYERS_POLL_MS);
    return () => clearInterval(id);
  }, [foreground, loadPlayers]);

  // A poll landing mid-PUT would flip the switch back for a beat; the PUT's answer wins.
  useEffect(() => {
    if (server && !autoInFlight.current) setAutoUpdate(server.auto_update);
  }, [server]);

  // An update's end changes what "latest" and "behind" mean, so re-read the version.
  const lastUpdState = useRef<string | null>(null);
  useEffect(() => {
    const st = update?.state ?? null;
    if (lastUpdState.current !== null && st !== lastUpdState.current) {
      if (st === "done" || st === "failed" || st === "rolled_back") void loadVersion(false);
    }
    lastUpdState.current = st;
  }, [update?.state, loadVersion]);

  // Whether an update began on a stopped server decides its third step's words; the
  // server's own state mid-update can't, since a normal update stops it too.
  useEffect(() => {
    if (!update || !(updateRunning(update) || update.state === "done")) return;
    if (updOrigin?.at === update.started_at) return;
    setUpdOrigin({
      at: update.started_at,
      stopped: stoppedIntent.current ?? server?.state === "stopped",
    });
    stoppedIntent.current = null;
  }, [update, server?.state, updOrigin?.at]);

  useEffect(() => {
    if (!restartPending || restartInFlight || !server) return;
    if (updateRunning(server.update)) {
      // Auto-update turned the restart into an update; its steps say what's happening.
      setRestartPending(false);
    } else if (server.state === "running") {
      if (restartSeenDown.current) setRestartPending(false);
    } else if (server.state === "install_failed") {
      setRestartPending(false);
    } else {
      restartSeenDown.current = true;
    }
  }, [server, restartPending, restartInFlight]);

  useEffect(() => {
    if (toast === null) return;
    const t = setTimeout(() => setToast(null), TOAST_MS);
    return () => clearTimeout(t);
  }, [toast]);

  const state = serverState(status, restartPending);
  const info = stateInfo(state, server);
  const installed = !NOT_INSTALLED.includes(state);
  const ctx = confirmContext(status, version);
  const behind = isBehind(version, update);
  const updating = updateRunning(update);
  const busy = state === "starting" || state === "stopping" || state === "restarting";
  const runningVersion = server?.version ?? version?.running ?? null;

  const worlds = useWorlds({
    serverUp: installed && server !== null,
    running: state === "running",
    online: state === "running" ? (server?.players ?? []).map((p) => p.name) : [],
    job,
    lastJob: server?.last_job ?? null,
    pendingRestart: server?.pending_restart ?? [],
    statusSeq,
    updating,
    state,
    nowMs: now,
    toast: setToast,
    refreshStatus: () => void loadStatus(),
  });
  const busyNow = worlds.upload !== null || worlds.inFlight !== null;
  const mainRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (mainRef.current) mainRef.current.inert = worlds.pages.length > 0;
  }, [worlds.pages.length]);
  useEffect(() => setLocalBusy(busyNow), [busyNow]);

  function ask(kind: ConfirmKind) {
    setActionError(null);
    if (confirmFor(kind, ctx)) setPending(kind);
    else void act(kind);
  }

  async function act(kind: ConfirmKind | "start" | "retry") {
    if (kind === "all" || inFlight !== null) return;
    setPending(null);
    setActionError(null);
    setInFlight(kind);
    try {
      if (kind !== "restart") setRestartPending(false);
      if (kind === "start" || kind === "restart") {
        if (kind === "restart") {
          restartSeenDown.current = false;
          setRestartPending(true);
        }
        const result = kind === "start" ? await api.minecraftStart() : await api.minecraftRestart();
        if (result.deferred) {
          setRestartPending(false);
          setToast("The container is still booting — the server starts on its own once it's up");
        } else if (result.update) {
          // Auto-update ran the backed-up update first; follow it like Update Minecraft.
          stoppedIntent.current = false;
          setRestartPending(false);
          setToast(
            `Auto-update is on — backing up, then installing ${result.update.to ?? "the update"}`,
          );
        }
      } else if (kind === "stop") await api.minecraftStop();
      else if (kind === "retry") {
        await api.minecraftRetryInstall();
      } else if (kind === "update") {
        stoppedIntent.current = ctx.stopped;
        const started = await api.minecraftUpdate();
        if (started.state === "current") {
          stoppedIntent.current = null;
          setToast(`Up to date — ${started.running} is the newest`);
          void loadVersion(false);
        }
      }
    } catch (err) {
      if (kind === "restart") setRestartPending(false);
      setActionError(errorMessage(err));
    } finally {
      setInFlight(null);
    }
    await loadStatus();
  }

  async function check() {
    setChecking(true);
    setActionError(null);
    await loadVersion(true);
    setChecking(false);
  }

  async function toggleAutoUpdate() {
    if (autoUpdate === null) return;
    const next = !autoUpdate;
    setAutoUpdate(next);
    autoInFlight.current = true;
    try {
      await api.minecraftSettings({ auto_update: next });
      setToast(
        next
          ? "Restarts will now update first, after a backup"
          : "Auto-update off — the server stays on its version until you update it",
      );
    } catch (err) {
      setAutoUpdate(!next);
      setActionError(`Couldn't change auto-update — ${errorMessage(err)}`);
    } finally {
      autoInFlight.current = false;
    }
  }

  async function copyAddress(address: string) {
    try {
      await navigator.clipboard.writeText(address);
      setToast(`Copied ${address}`);
    } catch {
      setActionError("Couldn't copy — clipboard unavailable.");
    }
  }

  function toUpdate() {
    document.getElementById("mc-update")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const failed =
    (update?.state === "failed" || update?.state === "rolled_back") &&
    dismissedFailure !== update.started_at
      ? update
      : null;
  const pendingSpec = pending ? confirmFor(pending, ctx) : null;
  const sheetPlayer = players?.players.find((p) => p.xuid === sheetXuid) ?? null;

  return (
    <WorldsProvider value={worlds}>
      <main
        className="screen-body mc-screen"
        ref={mainRef}
        aria-hidden={worlds.pages.length > 0 || undefined}
      >
        <Banner
          failed={failed}
          behind={behind && !updating && !failed}
          latest={version?.latest ?? null}
          running={runningVersion}
          onDetails={toUpdate}
        />

        {statusError && (
          <p className="error" role="alert">
            Can&apos;t reach the Minecraft server — {statusError}
          </p>
        )}
        {actionError && (
          <p className="error" role="alert">
            {actionError}
          </p>
        )}

        {status === null ? (
          !statusError && <p className="muted">Loading…</p>
        ) : (
          <>
            <Hero
              state={state}
              info={info}
              server={server}
              serverError={status.server_error}
              runningVersion={runningVersion}
              updating={updating}
              busy={busy}
              restartInFlight={restartInFlight}
              waiting={inFlight !== null}
              worldName={worlds.active ? (worlds.active.name ?? null) : null}
              jobWhat={job?.what ?? (worlds.upload ? "uploading a world" : worlds.inFlight)}
              onStart={() => void act("start")}
              onStop={() => ask("stop")}
              onRestart={() => ask("restart")}
              onRetry={() => void act("retry")}
            />

            <ErrLine />
            {installed && server && <WorldsEntry />}

            <UpdateSection
              state={state}
              installed={installed}
              version={version}
              update={update}
              failed={failed}
              behind={behind}
              busy={busy}
              waiting={inFlight !== null}
              runningVersion={runningVersion}
              fromStopped={
                updOrigin !== null && updOrigin.at === update?.started_at && updOrigin.stopped
              }
              nowMs={now}
              checking={checking}
              autoUpdate={autoUpdate}
              onUpdate={() => ask("update")}
              onDismiss={() => setDismissedFailure(update?.started_at ?? null)}
              onCheck={() => void check()}
              onToggleAuto={() => void toggleAutoUpdate()}
            />

            <OnlineSection state={state} server={server} nowMs={now} />

            <PlayersSection
              players={players}
              playersAt={playersAt}
              nowMs={now}
              onOpen={setSheetXuid}
            />

            <StatsSection available={players?.stats_available ?? false} />

            {installed && server && (
              <>
                <ServerSettingsSection
                  running={state === "running"}
                  online={server.players.length}
                  toast={setToast}
                  onError={setActionError}
                  pendingRestart={server.pending_restart ?? []}
                  refreshStatus={() => void loadStatus()}
                />
                <AllowlistSection
                  running={state === "running"}
                  online={state === "running" ? server.players.map((p) => p.name) : []}
                  known={(players?.players ?? []).map((p) => p.gamertag)}
                  toast={setToast}
                  onError={setActionError}
                  pendingRestart={server.pending_restart ?? []}
                  refreshStatus={() => void loadStatus()}
                />
              </>
            )}

            <JoinSection
              installed={installed}
              server={server}
              onCopy={(a) => void copyAddress(a)}
            />
          </>
        )}

        {pending && pendingSpec && (
          <Dialog
            title={pendingSpec.title}
            confirmLabel={pendingSpec.confirmLabel}
            tone={pendingSpec.tone}
            onCancel={() => setPending(null)}
            onConfirm={() => void act(pending)}
          >
            {pendingSpec.body}
          </Dialog>
        )}

        {sheetPlayer && (
          <PlayerSheet
            player={sheetPlayer}
            playersAt={playersAt}
            nowMs={now}
            statsAvailable={players?.stats_available ?? false}
            onClose={() => setSheetXuid(null)}
          />
        )}
      </main>
      {worlds.pages.map((p) =>
        p.kind === "worlds" ? (
          <WorldsLayer key="worlds" />
        ) : p.kind === "world" ? (
          <WorldLayer key={`world-${p.slot}`} slot={p.slot} />
        ) : (
          <RulesLayer key={`rules-${p.slot}`} slot={p.slot} />
        ),
      )}
      <WorldModals />
      {toast && <output className="toast mc-toast">{toast}</output>}
    </WorldsProvider>
  );
}

// ---- banner ----

function Banner({
  failed,
  behind,
  latest,
  running,
  onDetails,
}: {
  failed: MinecraftUpdate | null;
  behind: boolean;
  latest: string | null;
  running: string | null;
  onDetails: () => void;
}) {
  if (failed) {
    return (
      <div className="mc-banner mc-banner-bad">
        <AlertTriangleIcon size={16} />
        <span>
          {failed.state === "rolled_back"
            ? `${failed.to} wouldn't start — back on ${failed.from ?? running}.`
            : `Update failed — still on ${failed.from ?? running}.`}{" "}
          Newer clients still can&apos;t join.
        </span>
        <button type="button" className="mc-link" onClick={onDetails}>
          Details
        </button>
      </div>
    );
  }
  if (!behind || !latest) return null;
  return (
    <div className="mc-banner mc-banner-warn">
      <AlertTriangleIcon size={16} />
      <span>
        <b>{latest} is out.</b> Players who&apos;ve updated can&apos;t join until Minecraft is
        updated here.
      </span>
      <button type="button" className="mc-link" onClick={onDetails}>
        Update
      </button>
    </div>
  );
}

// ---- status block ----

function Spinner() {
  return (
    <span className="mc-spin" aria-hidden="true">
      <RefreshIcon size={18} />
    </span>
  );
}

function Hero({
  state,
  info,
  server,
  serverError,
  runningVersion,
  updating,
  busy,
  restartInFlight,
  waiting,
  worldName,
  jobWhat,
  onStart,
  onStop,
  onRestart,
  onRetry,
}: {
  state: McState;
  info: { level: string; word: string; sub: string };
  server: MinecraftServer | null;
  serverError: string | null;
  runningVersion: string | null;
  updating: boolean;
  busy: boolean;
  restartInFlight: boolean;
  waiting: boolean;
  worldName: string | null;
  jobWhat: string | null;
  onStart: () => void;
  onStop: () => void;
  onRestart: () => void;
  onRetry: () => void;
}) {
  let body: ReactNode = null;
  if (state === "installing") {
    body = (
      <>
        <div className="mc-meter" aria-hidden="true">
          <i />
        </div>
        <p className="mc-note">
          Once the download lands, the world is generated and the server starts on its own.
        </p>
      </>
    );
  } else if (state === "install_failed") {
    body = (
      <>
        <div className="mc-notice mc-notice-bad">
          <AlertTriangleIcon size={16} />
          <div>
            <b>Couldn&apos;t download the server.</b> Nothing was installed and no world was made.
            {server?.install_error && <div className="mc-errtext">{server.install_error}</div>}
          </div>
        </div>
        <button
          type="button"
          className="mc-btn mc-btn-primary"
          onClick={onRetry}
          disabled={waiting}
        >
          <RefreshIcon size={18} />
          Retry install
        </button>
      </>
    );
  } else if (state === "absent") {
    body = (
      <p className="mc-note">
        Minecraft isn&apos;t set up on this box yet — its container doesn&apos;t exist.
      </p>
    );
  } else {
    const lifecycleOk =
      (state === "running" || state === "unreachable") && !updating && !waiting && !jobWhat;
    const startable = state === "stopped" || state === "starting" || state === "down";
    const restarting = state === "restarting" || restartInFlight;
    body = (
      <>
        {state === "unreachable" && serverError && (
          <div className="mc-notice mc-notice-bad">
            <AlertTriangleIcon size={16} />
            <div className="mc-errtext">{serverError}</div>
          </div>
        )}
        {worldName && (
          <div className="mc-fact mc-fact-world">
            <div className="mc-fact-v">{worldName}</div>
            <div className="mc-fact-k">world loaded</div>
          </div>
        )}
        <div className="mc-facts">
          <div className="mc-fact">
            <div className="mc-fact-v">
              {state === "running" ? (server?.players.length ?? 0) : "—"}
            </div>
            <div className="mc-fact-k">online now</div>
          </div>
          <div className="mc-fact">
            <div className="mc-fact-v">{runningVersion ?? "—"}</div>
            <div className="mc-fact-k">version</div>
          </div>
        </div>
        <div className="mc-btnrow">
          {startable ? (
            <button
              type="button"
              className="mc-btn mc-btn-go mc-grow"
              onClick={onStart}
              disabled={state === "starting" || updating || waiting || !!jobWhat}
            >
              {state === "starting" ? <Spinner /> : <PlayIcon size={18} />}
              {state === "starting" ? "Starting…" : "Start server"}
            </button>
          ) : (
            <>
              <button
                type="button"
                className="mc-btn mc-grow"
                onClick={onRestart}
                disabled={!lifecycleOk}
              >
                {restarting ? <Spinner /> : <RefreshIcon size={18} />}
                {restarting ? "Restarting…" : "Restart"}
              </button>
              <button
                type="button"
                className="mc-btn mc-grow"
                onClick={onStop}
                disabled={!lifecycleOk}
              >
                {state === "stopping" ? <Spinner /> : <StopIcon size={18} />}
                {state === "stopping" ? "Stopping…" : "Stop"}
              </button>
            </>
          )}
        </div>
        {updating && (
          <p className="mc-note">
            Busy updating — Start, Stop and Restart come back when it&apos;s done.
          </p>
        )}
        {jobWhat && !updating && (
          <p className="mc-note">Busy {jobWhat} — the server is handled for you.</p>
        )}
        {busy && !updating && !jobWhat && <p className="mc-note">Wait for it to finish {state}.</p>}
        {waiting && !busy && !updating && (
          <p className="mc-note">Waiting for the server to answer…</p>
        )}
      </>
    );
  }
  return (
    <div className="mc-hero">
      <div className="mc-state">
        <span className={`mc-dot mc-dot-${info.level}`} aria-hidden="true" />
        <span className="mc-state-word">{info.word}</span>
        <span className="mc-state-sub">{info.sub}</span>
      </div>
      {body}
    </div>
  );
}

// ---- update ----

const STEP_OF: Record<string, number> = {
  backing_up: 0,
  downloading: 1,
  restarting: 2,
  done: 3,
};

function Steps({ update, fromStopped }: { update: MinecraftUpdate; fromStopped: boolean }) {
  const rolledBack = update.state === "rolled_back";
  // Where a failure stopped: no backup means the backup itself failed; "failed" after it is
  // the download or install; a rollback is the new build failing to start.
  const failedAt = rolledBack ? 2 : update.state === "failed" ? (update.backup ? 1 : 0) : null;
  const step = failedAt ?? STEP_OF[update.state] ?? 0;
  const done = update.state === "done";
  const labels = [
    `Backing up — pre-update-${update.from ?? "current"}`,
    `Downloading ${update.to ?? ""}`.trim(),
    rolledBack
      ? `Restarting — ${update.to} wouldn't start, rolled back`
      : fromStopped
        ? "Installing — server stays stopped"
        : "Restarting — players disconnected",
    "Done",
  ];
  // The API stamps only the start and the finish, so only those steps carry a time.
  const at = [
    clockOf(update.started_at),
    "",
    "",
    update.finished_at ? clockOf(update.finished_at) : "",
  ];
  return (
    <ol className="mc-steps">
      {labels.map((label, i) => {
        const cls =
          done || i < step ? "done" : i === step ? (failedAt !== null ? "fail" : "now") : "";
        return (
          <li key={label} className={cls}>
            <span className="mc-step-dot" aria-hidden="true">
              {cls === "done" && <CheckIcon size={11} />}
              {cls === "fail" && <XIcon size={11} />}
            </span>
            <span>{label}</span>
            {cls === "now" && <span className="mc-sr-only"> (in progress)</span>}
            {cls === "fail" && <span className="mc-sr-only"> (failed)</span>}
            {at[i] && (cls === "done" || cls === "now") && (
              <span className="mc-step-at">{at[i]}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function updateStatusText(update: MinecraftUpdate, fromStopped: boolean): string {
  if (update.state === "done") {
    return fromStopped
      ? `Installed ${update.to} — the server is still stopped.`
      : "Done — players can rejoin.";
  }
  if (fromStopped) return "Installing while stopped — the server stays stopped afterwards.";
  if (update.state === "backing_up" || update.state === "downloading") {
    return "Players stay on while it backs up and downloads — they're disconnected at the restart, for about a minute.";
  }
  return "Restarting — players are disconnected, back in about a minute.";
}

function Changelog({ version, latest }: { version: MinecraftVersion; latest: string }) {
  const notes = version.notes;
  if (!notes) {
    return (
      <p className="mc-note">
        Release notes not published yet — Mojang hasn&apos;t posted the {marketingNumber(latest)}{" "}
        article.{" "}
        <a
          className="mc-link"
          href={version.notes_fallback_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          Minecraft update page <ExternalLinkIcon size={14} />
        </a>
      </p>
    );
  }
  const n = notes.lines.length;
  const posted = Date.parse(notes.published_at);
  return (
    <>
      <div className="mc-clog">
        {n > 0 && (
          <div className="mc-clog-src">
            From Mojang&apos;s changelog · first {plural(n, "line")}, verbatim
          </div>
        )}
        <div className="mc-clog-title">
          “{notes.title}”{Number.isNaN(posted) ? "" : ` · posted ${shortDate(posted / 1000)}`}
        </div>
        {n > 0 && (
          <ul>
            {notes.lines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
      </div>
      <a className="mc-link" href={notes.url} target="_blank" rel="noopener noreferrer">
        Release notes <ExternalLinkIcon size={14} />
      </a>
    </>
  );
}

function UpdateSection({
  state,
  installed,
  version,
  update,
  failed,
  behind,
  busy,
  waiting,
  runningVersion,
  fromStopped,
  nowMs,
  checking,
  autoUpdate,
  onUpdate,
  onDismiss,
  onCheck,
  onToggleAuto,
}: {
  state: McState;
  installed: boolean;
  version: MinecraftVersion | null;
  update: MinecraftUpdate | null;
  failed: MinecraftUpdate | null;
  behind: boolean;
  busy: boolean;
  waiting: boolean;
  runningVersion: string | null;
  fromStopped: boolean;
  nowMs: number;
  checking: boolean;
  autoUpdate: boolean | null;
  onUpdate: () => void;
  onDismiss: () => void;
  onCheck: () => void;
  onToggleAuto: () => void;
}) {
  if (!installed) {
    return (
      <>
        <h3 className="mc-sect" id="mc-update">
          Update
        </h3>
        <div className="mc-card">
          <div className="mc-pad">
            <p className="mc-note">
              {state === "absent"
                ? "Updates are checked once the server is set up."
                : "Not installed yet — updates are checked once the server is installed."}
            </p>
          </div>
        </div>
      </>
    );
  }
  const latest = version?.latest ?? update?.to ?? null;
  const why = waiting
    ? "Waiting for the server to answer…"
    : busy
      ? `Available once the server has finished ${state}.`
      : "";
  const recentlyDone =
    update?.state === "done" &&
    update.finished_at !== null &&
    nowMs / 1000 - update.finished_at < DONE_SHOWN_S &&
    !behind;
  let inner: ReactNode;
  if (update && (updateRunning(update) || recentlyDone)) {
    inner = (
      <>
        <div className="mc-ver">
          <span className="mc-ver-big">{update.from}</span>
          <span className="mc-ver-arrow" aria-hidden="true">
            →
          </span>
          <span className={`mc-ver-new${update.state === "done" ? " ok" : ""}`}>{update.to}</span>
        </div>
        <p className="mc-note" aria-live="polite">
          {updateStatusText(update, fromStopped)}
        </p>
        <Steps update={update} fromStopped={fromStopped} />
      </>
    );
  } else if (failed) {
    inner = (
      <>
        <div className="mc-notice mc-notice-bad">
          <AlertTriangleIcon size={16} />
          <div>
            {failed.state === "rolled_back" ? (
              <>
                <b>
                  {`${failed.to} wouldn't start — rolled back to ${failed.from ?? runningVersion}; world restored from the backup (kept).`}
                </b>{" "}
                {failed.backup && (
                  <>
                    <span className="mc-mono">{failed.backup}</span>.
                  </>
                )}
              </>
            ) : (
              <>
                <b>Update failed — still on {failed.from ?? runningVersion}.</b>{" "}
                {failed.backup ? (
                  <>
                    Nothing was installed; the backup{" "}
                    <span className="mc-mono">{failed.backup}</span> is kept.
                  </>
                ) : (
                  "No backup was taken, so nothing was installed."
                )}
              </>
            )}{" "}
            Players who&apos;ve updated to {failed.to} still can&apos;t join.
            {failed.error && <div className="mc-errtext">{failed.error}</div>}
          </div>
        </div>
        <Steps update={failed} fromStopped={fromStopped} />
        <div className="mc-btnrow">
          <button type="button" className="mc-btn" onClick={onDismiss}>
            Dismiss
          </button>
          {behind && (
            <button
              type="button"
              className="mc-btn mc-btn-warn mc-grow"
              onClick={onUpdate}
              disabled={busy || waiting}
            >
              <RefreshIcon size={18} />
              Try again
            </button>
          )}
        </div>
        {why && <p className="mc-note">{why}</p>}
      </>
    );
  } else if (behind && latest && version) {
    inner = (
      <>
        <div className="mc-ver">
          <span className="mc-ver-big">{runningVersion}</span>
          <span className="mc-ver-arrow" aria-hidden="true">
            →
          </span>
          <span className="mc-ver-new">{latest}</span>
        </div>
        <p className="mc-note">
          Update available. Clients update themselves and can only join a server on exactly their
          version.
        </p>
        <Changelog version={version} latest={latest} />
        <button
          type="button"
          className="mc-btn mc-btn-warn"
          onClick={onUpdate}
          disabled={busy || waiting}
        >
          <DownloadIcon size={18} />
          Update Minecraft
        </button>
        {why && <p className="mc-note">{why}</p>}
      </>
    );
  } else {
    inner = (
      <>
        <div className="mc-ver">
          <span className="mc-ver-big">{runningVersion ?? "—"}</span>
          {version && !version.update_available && version.latest && (
            <span className="badge ok mc-badge">up to date</span>
          )}
        </div>
        {version?.notes && (
          <a className="mc-link" href={version.notes.url} target="_blank" rel="noopener noreferrer">
            Release notes for {version.notes.version} <ExternalLinkIcon size={14} />
          </a>
        )}
      </>
    );
  }
  const checkedText = checking
    ? "checking Mojang…"
    : version?.checked_at
      ? `last checked ${agoOf(version.checked_at, nowMs)}`
      : "not checked yet";
  return (
    <>
      <h3 className="mc-sect" id="mc-update">
        Update
        {runningVersion && <span className="mc-sect-r">running {runningVersion}</span>}
      </h3>
      <div className="mc-card">
        <div className="mc-pad">
          {inner}
          {version?.check_error && (
            <p className="mc-note">Couldn&apos;t reach Mojang — {version.check_error}</p>
          )}
          {autoUpdate !== null && (
            <div className="mc-sw-row">
              <span className="mc-sw-l" id="mc-auto-l">
                <b>Update automatically on restart</b>
                <br />
                {autoUpdate
                  ? "on — each restart installs the newest version, after a backup"
                  : "off — stays on its version until you update"}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={autoUpdate}
                aria-labelledby="mc-auto-l"
                className={`mc-switch${autoUpdate ? " on" : ""}`}
                onClick={onToggleAuto}
              >
                <span className="mc-knob" />
              </button>
            </div>
          )}
        </div>
        <div className="mc-foot">
          <span>{checkedText}</span>
          <button
            type="button"
            className="mc-link"
            onClick={onCheck}
            disabled={checking || updateRunning(update)}
          >
            {checking ? <Spinner /> : <RefreshIcon size={14} />}
            Check for updates
          </button>
        </div>
      </div>
    </>
  );
}

// ---- online now ----

function OnlineSection({
  state,
  server,
  nowMs,
}: {
  state: McState;
  server: MinecraftServer | null;
  nowMs: number;
}) {
  const online = state === "running" ? (server?.players ?? []) : [];
  let rows: ReactNode;
  if (state !== "running") {
    rows = (
      <div className="mc-empty">
        {state === "stopped"
          ? "The server is stopped."
          : state === "down"
            ? "The container is down."
            : "Not running yet."}
      </div>
    );
  } else if (online.length === 0) {
    rows = <div className="mc-empty">Nobody on right now.</div>;
  } else {
    rows = online.map((p) => (
      <div className="mc-row" key={p.xuid}>
        <span className="mc-pav on" aria-hidden="true">
          {initial(p.name)}
        </span>
        <span className="mc-rn">
          <span className="mc-nm" title={p.name}>
            {p.name}
          </span>
          <span className="mc-sm">this session</span>
        </span>
        <span className="mc-rt">{fmtDur(nowMs / 1000 - p.joined_at)}</span>
      </div>
    ));
  }
  return (
    <>
      <h3 className="mc-sect">
        Online now
        {online.length > 0 && <span className="mc-sect-r">{online.length} on now</span>}
      </h3>
      <div className="mc-card">{rows}</div>
    </>
  );
}

// ---- players (variant C's time table, opening a Sheet) ----

function liveTotal(p: MinecraftPlayer, playersAt: number, nowMs: number): number {
  return p.total_seconds + (p.online ? Math.max(0, (nowMs - playersAt) / 1000) : 0);
}

function lastSeenText(p: MinecraftPlayer, nowMs: number): string {
  if (p.online) return "on now";
  const day = dayOf(p.last_seen, nowMs);
  return day === "today" || day === "yesterday" ? `${day}, ${clockOf(p.last_seen)}` : day;
}

function PlayersSection({
  players,
  playersAt,
  nowMs,
  onOpen,
}: {
  players: MinecraftPlayers | null;
  playersAt: number;
  nowMs: number;
  onOpen: (xuid: string) => void;
}) {
  let body: ReactNode;
  if (players === null) body = <div className="mc-empty">Loading…</div>;
  else if (players.players.length === 0)
    body = <div className="mc-empty">No one has played yet.</div>;
  else {
    const sorted = players.players
      .slice()
      .sort(
        (a, b) =>
          Number(b.online) - Number(a.online) ||
          liveTotal(b, playersAt, nowMs) - liveTotal(a, playersAt, nowMs),
      );
    body = (
      <table className="mc-ptable">
        <caption className="mc-sr-only">Time played</caption>
        <thead>
          <tr className="mc-pth">
            <th scope="col">Player</th>
            <th scope="col">Total</th>
            <th scope="col">Last seen</th>
            <td />
          </tr>
        </thead>
        <tbody>
          {sorted.map((p) => (
            <tr className="mc-ptr" key={p.xuid}>
              <td className="mc-pname">
                <span className={`mc-pav${p.online ? " on" : ""}`} aria-hidden="true">
                  {initial(p.gamertag)}
                </span>
                <span className="mc-pn">
                  {/* The button's overlay covers the whole row, so a tap anywhere opens it. */}
                  <button
                    type="button"
                    className="mc-prow-btn"
                    title={p.gamertag}
                    onClick={() => onOpen(p.xuid)}
                  >
                    {p.gamertag}
                  </button>
                  <span className="mc-sm">since {shortDate(p.first_seen)}</span>
                </span>
              </td>
              <td>
                {fmtDur(liveTotal(p, playersAt, nowMs))}
                <span className="mc-sm">{plural(p.sessions, "session")}</span>
              </td>
              <td>
                {p.online ? (
                  <>
                    <span className="mc-on">on now</span>
                    <span className="mc-sm">
                      {p.session_started_at !== null
                        ? `${fmtDur(nowMs / 1000 - p.session_started_at)} in`
                        : ""}
                    </span>
                  </>
                ) : (
                  <>
                    {dayOf(p.last_seen, nowMs)}
                    <span className="mc-sm">{clockOf(p.last_seen)}</span>
                  </>
                )}
              </td>
              <td aria-hidden="true">
                <ChevronRightIcon size={16} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return (
    <>
      <h3 className="mc-sect">
        Players<span className="mc-sect-r">time on this server</span>
      </h3>
      <div className="mc-card mc-card-pad">{body}</div>
    </>
  );
}

function AddonNote({ available }: { available: boolean }) {
  return (
    <div className="mc-addon">
      <PuzzleIcon size={18} />
      <p className="mc-note">
        {available ? (
          "The companion add-on is counting — this screen doesn't show its numbers yet."
        ) : (
          <>
            <b>{ADDON_HEAD}</b> {ADDON_REST}
          </>
        )}
      </p>
    </div>
  );
}

function PlayerSheet({
  player,
  playersAt,
  nowMs,
  statsAvailable,
  onClose,
}: {
  player: MinecraftPlayer;
  playersAt: number;
  nowMs: number;
  statsAvailable: boolean;
  onClose: () => void;
}) {
  const session =
    player.online && player.session_started_at !== null
      ? fmtDur(nowMs / 1000 - player.session_started_at)
      : null;
  return (
    <Sheet title={player.gamertag} onClose={onClose}>
      <p className="mc-note mc-sheet-sub">
        {player.online
          ? `on now${session ? ` · ${session} this session` : ""}`
          : `last seen ${lastSeenText(player, nowMs)}`}
      </p>
      <div className="mc-sheet-grid">
        <div className="mc-stat">
          <div className="mc-stat-v">{fmtDur(liveTotal(player, playersAt, nowMs))}</div>
          <div className="mc-stat-k">time on server</div>
        </div>
        <div className="mc-stat">
          <div className="mc-stat-v">{player.sessions}</div>
          <div className="mc-stat-k">sessions</div>
        </div>
        <div className="mc-stat">
          <div className="mc-stat-v">{shortDate(player.first_seen)}</div>
          <div className="mc-stat-k">first seen</div>
        </div>
        <div className="mc-stat">
          <div className="mc-stat-v">{lastSeenText(player, nowMs)}</div>
          <div className="mc-stat-k">last seen</div>
        </div>
      </div>
      <h3 className="mc-sect">Lifetime stats</h3>
      <div className="mc-card">
        <AddonNote available={statsAvailable} />
      </div>
      <button type="button" className="sheet-primary mc-sheet-close" onClick={onClose}>
        <XIcon size={16} />
        Close
      </button>
    </Sheet>
  );
}

// ---- lifetime stats ----

function StatsSection({ available }: { available: boolean }) {
  return (
    <>
      <h3 className="mc-sect">
        Lifetime stats
        {!available && <span className="mc-sect-r">not available yet</span>}
      </h3>
      <div className="mc-card">
        <AddonNote available={available} />
        {!available &&
          LEADER_ROWS.map((label) => (
            <div className="mc-lead" key={label}>
              <span className="mc-lead-k">{label}</span>
              <span className="mc-lead-v">—</span>
            </div>
          ))}
      </div>
    </>
  );
}

// ---- how to join ----

function JoinSection({
  installed,
  server,
  onCopy,
}: {
  installed: boolean;
  server: MinecraftServer | null;
  onCopy: (address: string) => void;
}) {
  const address = installed && server?.lan_ip ? `${server.lan_ip}:${server.port}` : null;
  let join: ReactNode;
  if (!installed) join = "Not installed yet — join details appear once the server is up.";
  else if (!server) join = "Join details show once the container is back up.";
  else {
    join = (
      <>
        On your LAN: <b>Friends → LAN Games → {server.server_name}</b>
        <br />
        Or add a server:{" "}
        {server.lan_ip ? (
          <b className="mc-mono">{server.lan_ip}</b>
        ) : (
          "this box's LAN address (not known yet)"
        )}{" "}
        · port <b className="mc-mono">{server.port}</b>
      </>
    );
  }
  return (
    <>
      <h3 className="mc-sect">Join</h3>
      <div className="mc-card">
        <div className="mc-pad mc-join">
          <div className="mc-label">How to join</div>
          <p className="mc-note">{join}</p>
          {address && (
            <button type="button" className="mc-link" onClick={() => onCopy(address)}>
              Copy address
            </button>
          )}
        </div>
      </div>
    </>
  );
}
