import { useCallback, useEffect, useRef, useState } from "react";
import {
  type ExportStatus,
  type OpsMetrics,
  type UpdateStatus,
  api,
  exportFileUrl,
} from "../api/client";
import { DatabaseIcon, RefreshIcon } from "../components/icons";

/** The Data launcher screen (docs/DESIGN.md "Data screen"): export / import /
 * reset as one focused task at a time behind a Backup · Restore · Reset
 * segmented switch (variant C). The flows are the settled Ops Data card —
 * lifted onto their own screen and re-laid-out — so the supervisor one-shots,
 * tap-again confirms, and reload-on-done behave exactly as before. */

type Task = "backup" | "restore" | "reset";

// The active segment tints itself with the task's accent (steel/amber/rose) via
// the shared .seg-on rule, which fills with --mode-tint / --mode.
const TASK_ACCENT: Record<Task, { mode: string; tint: string }> = {
  backup: { mode: "var(--steel)", tint: "var(--steel-tint)" },
  restore: { mode: "var(--amber)", tint: "var(--amber-tint)" },
  reset: { mode: "var(--rose)", tint: "var(--rose-tint)" },
};

function fmtBytes(n: number): string {
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(0)} MB`;
  return `${(n / 1024).toFixed(0)} KB`;
}

type ExportPhase =
  | { step: "idle"; latest: string | null }
  | { step: "running" }
  | { step: "failed"; log: string };

/** Hand the export to the browser's download manager without navigating the
 * SPA (a navigation remounts the app and can swallow the download in
 * standalone PWAs). A transient same-origin anchor + `download` leaves the app
 * untouched; the done state also keeps a visible link as an iOS fallback. */
function triggerExportDownload(name: string) {
  const a = document.createElement("a");
  a.href = exportFileUrl(name);
  a.download = name;
  a.hidden = true;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

type ImportPhase =
  | { step: "idle" }
  | { step: "picked"; file: File; armed: boolean }
  | { step: "uploading" }
  | { step: "running"; log: string; unreachable: boolean }
  | { step: "done"; ok: boolean; log: string };

type ResetPhase =
  | { step: "idle"; armed: boolean }
  | { step: "running"; log: string; unreachable: boolean }
  | { step: "done"; ok: boolean; log: string };

const DATA_POLL_MS = 3000;
const RESET_DISARM_MS = 3000;

export function DataScreen() {
  const [task, setTask] = useState<Task>("backup");
  const [db, setDb] = useState<OpsMetrics["db"] | null>(null);
  const [blobs, setBlobs] = useState<OpsMetrics["blobs"]>(null);
  const [exportPhase, setExportPhase] = useState<ExportPhase>({ step: "idle", latest: null });
  const [importPhase, setImportPhase] = useState<ImportPhase>({ step: "idle" });
  const [resetPhase, setResetPhase] = useState<ResetPhase>({ step: "idle", armed: false });
  const fileRef = useRef<HTMLInputElement>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const disarmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => stopPolling, [stopPolling]);
  useEffect(
    () => () => {
      if (disarmTimer.current !== null) clearTimeout(disarmTimer.current);
    },
    [],
  );

  // The Backup panel's at-a-glance summary — real figures from the ops metrics
  // (db size + blob footprint); quiet on failure, the summary just hides.
  useEffect(() => {
    let stale = false;
    api
      .opsMetrics()
      .then((m) => {
        if (stale) return;
        setDb(m.db);
        setBlobs(m.blobs);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  const pollExport = useCallback(async () => {
    let status: ExportStatus;
    try {
      status = await api.opsExportStatus();
    } catch {
      return;
    }
    if (status.state !== "exited") return;
    stopPolling();
    if (status.exit_code === 0 && status.filename !== null) {
      triggerExportDownload(status.filename);
      setExportPhase({ step: "idle", latest: status.filename });
    } else {
      setExportPhase({ step: "failed", log: status.log_tail });
    }
  }, [stopPolling]);

  async function startExport() {
    try {
      await api.opsExportStart();
    } catch {
      setExportPhase({ step: "failed", log: "could not start — is another operation running?" });
      return;
    }
    setExportPhase({ step: "running" });
    timer.current = setInterval(() => void pollExport(), DATA_POLL_MS);
  }

  const pollImport = useCallback(async () => {
    let status: UpdateStatus;
    try {
      status = await api.opsImportStatus();
    } catch {
      setImportPhase((p) => (p.step === "running" ? { ...p, unreachable: true } : p));
      return;
    }
    if (status.state === "running") {
      setImportPhase({ step: "running", log: status.log_tail, unreachable: false });
    } else if (status.state === "exited") {
      stopPolling();
      setImportPhase({ step: "done", ok: status.exit_code === 0, log: status.log_tail });
    }
  }, [stopPolling]);

  async function startImport(file: File) {
    setImportPhase({ step: "uploading" });
    try {
      const { archive } = await api.opsImportUpload(file);
      await api.opsImportStart(archive);
    } catch {
      setImportPhase({ step: "done", ok: false, log: "upload or start failed" });
      return;
    }
    setImportPhase({ step: "running", log: "[import] starting", unreachable: false });
    timer.current = setInterval(() => void pollImport(), DATA_POLL_MS);
  }

  const pollReset = useCallback(async () => {
    let status: UpdateStatus;
    try {
      status = await api.opsResetStatus();
    } catch {
      setResetPhase((p) => (p.step === "running" ? { ...p, unreachable: true } : p));
      return;
    }
    if (status.state === "running") {
      setResetPhase({ step: "running", log: status.log_tail, unreachable: false });
    } else if (status.state === "exited") {
      stopPolling();
      setResetPhase({ step: "done", ok: status.exit_code === 0, log: status.log_tail });
    }
  }, [stopPolling]);

  async function startReset() {
    try {
      await api.opsResetStart();
    } catch {
      setResetPhase({
        step: "done",
        ok: false,
        log: "could not start — is another operation running?",
      });
      return;
    }
    setResetPhase({ step: "running", log: "[reset] starting", unreachable: false });
    timer.current = setInterval(() => void pollReset(), DATA_POLL_MS);
  }

  function tapReset() {
    if (disarmTimer.current !== null) clearTimeout(disarmTimer.current);
    disarmTimer.current = null;
    if (resetPhase.step === "idle" && resetPhase.armed) {
      void startReset();
      return;
    }
    setResetPhase({ step: "idle", armed: true });
    disarmTimer.current = setTimeout(
      () => setResetPhase((p) => (p.step === "idle" ? { step: "idle", armed: false } : p)),
      RESET_DISARM_MS,
    );
  }

  const accent = TASK_ACCENT[task];

  return (
    <main className="screen-body data-screen">
      <fieldset
        className="seg-row data-seg"
        aria-label="Data task"
        style={{ "--mode": accent.mode, "--mode-tint": accent.tint } as React.CSSProperties}
      >
        {(["backup", "restore", "reset"] as Task[]).map((t) => (
          <button
            key={t}
            type="button"
            className={`seg${task === t ? " seg-on" : ""}`}
            aria-pressed={task === t}
            onClick={() => setTask(t)}
          >
            {t === "backup" ? "Backup" : t === "restore" ? "Restore" : "Reset"}
          </button>
        ))}
      </fieldset>

      {task === "backup" && (
        <BackupPanel
          db={db}
          blobs={blobs}
          phase={exportPhase}
          onExport={() => void startExport()}
        />
      )}
      {task === "restore" && (
        <RestorePanel
          phase={importPhase}
          fileRef={fileRef}
          onPick={() => fileRef.current?.click()}
          onFile={(file) => setImportPhase({ step: "picked", file, armed: false })}
          onArm={(p) => setImportPhase({ ...p, armed: true })}
          onDisarm={(p) => setImportPhase({ ...p, armed: false })}
          onConfirm={(file) => void startImport(file)}
        />
      )}
      {task === "reset" && (
        <ResetPanel
          phase={resetPhase}
          onTap={tapReset}
          onBlur={() => setResetPhase({ step: "idle", armed: false })}
        />
      )}
    </main>
  );
}

function TaskLead({
  accent,
  icon,
  title,
  desc,
}: {
  accent: Task;
  icon: React.ReactNode;
  title: string;
  desc: string;
}) {
  return (
    <div className={`data-lead data-lead-${accent}`}>
      <span className="data-lead-ic">{icon}</span>
      <div>
        <h2>{title}</h2>
        <p>{desc}</p>
      </div>
    </div>
  );
}

function BackupPanel({
  db,
  blobs,
  phase,
  onExport,
}: {
  db: OpsMetrics["db"];
  blobs: OpsMetrics["blobs"];
  phase: ExportPhase;
  onExport: () => void;
}) {
  return (
    <section>
      <TaskLead
        accent="backup"
        icon={<DatabaseIcon size={20} />}
        title="Back up everything"
        desc="One portable archive of your whole knowledge base, downloaded to this device."
      />
      {db && (
        <div className="data-summary">
          <div className="data-summary-row">
            <span>database</span>
            <span>{fmtBytes(db.db_size_bytes)}</span>
          </div>
          <div className="data-summary-row">
            <span>notes · files</span>
            <span>
              {db.note_count} · {db.attachment_count}
            </span>
          </div>
          {blobs && (
            <div className="data-summary-row">
              <span>attachment blobs</span>
              <span>{fmtBytes(blobs.total_bytes)}</span>
            </div>
          )}
        </div>
      )}

      {phase.step !== "running" && (
        <button type="button" className="data-btn" onClick={onExport}>
          Export backup
        </button>
      )}
      {phase.step === "running" && <p className="muted data-status">Building export archive…</p>}
      {phase.step === "idle" && phase.latest !== null && (
        <>
          <p className="muted data-status">{phase.latest} downloaded.</p>
          <a className="export-link" href={exportFileUrl(phase.latest)} download={phase.latest}>
            download {phase.latest}
          </a>
        </>
      )}
      {phase.step === "idle" && phase.latest === null && (
        <p className="data-note">
          Produces <code>export-….jbrain.tar</code> · nothing is changed on the server.
        </p>
      )}
      {phase.step === "failed" && (
        <>
          <p className="ops-error">Export failed — see log.</p>
          <pre className="ops-update-log">{phase.log}</pre>
        </>
      )}
    </section>
  );
}

function RestorePanel({
  phase,
  fileRef,
  onPick,
  onFile,
  onArm,
  onDisarm,
  onConfirm,
}: {
  phase: ImportPhase;
  fileRef: React.RefObject<HTMLInputElement>;
  onPick: () => void;
  onFile: (file: File) => void;
  onArm: (p: Extract<ImportPhase, { step: "picked" }>) => void;
  onDisarm: (p: Extract<ImportPhase, { step: "picked" }>) => void;
  onConfirm: (file: File) => void;
}) {
  return (
    <section>
      <TaskLead
        accent="restore"
        icon={<DatabaseIcon size={20} />}
        title="Restore from a backup"
        desc="Replace the current knowledge base with an archive. This overwrites everything."
      />
      <ol className="data-steps">
        <li>
          <span className="data-step-n">1</span>Pick a <code>.jbrain.tar</code> you exported
          earlier.
        </li>
        <li>
          <span className="data-step-n">2</span>A safety backup of current data is taken
          automatically.
        </li>
        <li>
          <span className="data-step-n">3</span>The stack restarts while the archive is restored.
        </li>
      </ol>

      <input
        ref={fileRef}
        type="file"
        accept=".tar,.jbrain.tar"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFile(file);
          e.target.value = "";
        }}
      />

      {phase.step === "idle" && (
        <button type="button" className="data-btn data-btn-sec" onClick={onPick}>
          Choose a backup file…
        </button>
      )}
      {phase.step === "picked" && (
        <>
          <div className="data-filechip">
            <span className="data-filechip-name">{phase.file.name}</span>
            <span className="data-filechip-size">{fmtBytes(phase.file.size)}</span>
          </div>
          <button
            type="button"
            className="data-btn data-btn-danger"
            onClick={() => (phase.armed ? onConfirm(phase.file) : onArm(phase))}
            onBlur={() => onDisarm(phase)}
          >
            {phase.armed
              ? "Tap again — current data is overwritten"
              : "Import — replaces ALL current data"}
          </button>
        </>
      )}
      {phase.step === "uploading" && <p className="muted data-status">Uploading archive…</p>}
      {phase.step === "running" && (
        <>
          <p className="muted data-status">
            {phase.unreachable ? "Stack restarting — hold on…" : "Importing…"}
          </p>
          <pre className="ops-update-log">{phase.log}</pre>
        </>
      )}
      {phase.step === "done" && (
        <>
          <p className={phase.ok ? "muted data-status" : "ops-error"}>
            {phase.ok ? "Import complete." : "Import failed — see log."}
          </p>
          <pre className="ops-update-log">{phase.log}</pre>
          {phase.ok && (
            <button type="button" className="data-btn" onClick={() => window.location.reload()}>
              Reload app
            </button>
          )}
        </>
      )}
    </section>
  );
}

function ResetPanel({
  phase,
  onTap,
  onBlur,
}: {
  phase: ResetPhase;
  onTap: () => void;
  onBlur: () => void;
}) {
  return (
    <section>
      <TaskLead
        accent="reset"
        icon={<RefreshIcon size={20} />}
        title="Reset the database"
        desc="Erase all notes and derived data and start clean. A testing convenience."
      />
      <div className="data-summary">
        <div className="data-summary-row">
          <span>erases</span>
          <span>notes · attachments · graph · facts</span>
        </div>
        <div className="data-summary-row">
          <span>keeps</span>
          <span>auth · domains · usage</span>
        </div>
        <div className="data-summary-row">
          <span>safety backup</span>
          <span>taken first</span>
        </div>
      </div>

      {phase.step === "idle" && (
        <button type="button" className="data-btn data-btn-danger" onClick={onTap} onBlur={onBlur}>
          {phase.armed ? "Tap again — erases ALL notes and data" : "Reset DB"}
        </button>
      )}
      {phase.step === "idle" && !phase.armed && (
        <p className="data-note">The worker restarts; you'll reload the app when it's done.</p>
      )}
      {phase.step === "running" && (
        <>
          <p className="muted data-status">
            {phase.unreachable ? "Worker restarting — hold on…" : "Resetting…"}
          </p>
          <pre className="ops-update-log">{phase.log}</pre>
        </>
      )}
      {phase.step === "done" && (
        <>
          <p className={phase.ok ? "muted data-status" : "ops-error"}>
            {phase.ok ? "Reset complete." : "Reset failed — see log."}
          </p>
          <pre className="ops-update-log">{phase.log}</pre>
          {phase.ok && (
            <button type="button" className="data-btn" onClick={() => window.location.reload()}>
              Reload app
            </button>
          )}
        </>
      )}
    </section>
  );
}
