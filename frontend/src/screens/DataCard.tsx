import { useCallback, useEffect, useRef, useState } from "react";
import { type ExportStatus, type UpdateStatus, api, exportFileUrl } from "../api/client";

/** Local copy of the byte formatter — DataCard is self-contained so it can be
 * dropped into the Data launcher screen without depending on Ops. */
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
 * SPA. `location.assign()` navigates the app itself: the SPA remounts (the
 * screen and the card's state vanish — "returns to the main screen") and
 * in standalone PWA display-mode the navigation can swallow the download
 * outright on mobile. A transient same-origin anchor + `download` leaves the
 * app untouched; the Content-Disposition: attachment response does the rest.
 * iOS home-screen web apps remain flaky even with this pattern (the click may
 * silently no-op or open an undismissable share/preview sheet — long-standing
 * WebKit limitation, e.g. webkit.org/b/167341), so the done state also keeps
 * a visible link to the same URL for a manual long-press/share fallback. */
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

/** Ops "Data" card (docs/DESIGN.md): export downloads one archive of the
 * database dump + attachment files; import replaces everything with an
 * uploaded archive via a supervisor one-shot that restarts the stack;
 * reset erases all content data (a testing convenience) while auth,
 * domains, and llm_usage survive — also a one-shot, since the api role
 * deliberately cannot TRUNCATE.
 *
 * Lifted out of the Ops screen in the B3 redesign: Data is its own
 * launcher destination now, so the card lives standalone and Ops no longer
 * carries it. */
export function DataCard() {
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
      // The browser download carries the session cookie like any request.
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
      // api/worker stop mid-import; an unreachable api is the expected
      // shape of progress, not an error.
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
      // The worker restarts mid-reset; the api stays up, but tolerate
      // blips the same way import does.
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

  return (
    <section className="ops-update">
      <h3>Data</h3>
      {exportPhase.step !== "running" &&
        importPhase.step === "idle" &&
        resetPhase.step === "idle" && (
          <div className="ops-actions">
            <button type="button" onClick={() => void startExport()}>
              Export backup
            </button>
            <button type="button" onClick={() => fileRef.current?.click()}>
              Import backup…
            </button>
            <button
              type="button"
              className="danger"
              onClick={tapReset}
              onBlur={() => setResetPhase({ step: "idle", armed: false })}
            >
              {resetPhase.armed ? "Tap again — erases ALL notes and data" : "Reset DB"}
            </button>
          </div>
        )}
      {exportPhase.step === "idle" && exportPhase.latest !== null && (
        <>
          <p className="muted">{exportPhase.latest} downloaded.</p>
          <a
            className="export-link"
            href={exportFileUrl(exportPhase.latest)}
            download={exportPhase.latest}
          >
            download {exportPhase.latest}
          </a>
        </>
      )}
      {exportPhase.step === "idle" &&
        exportPhase.latest === null &&
        importPhase.step === "idle" &&
        resetPhase.step === "idle" && (
          <p className="muted data-hint">
            export bundles the database + attachment files into one archive; import replaces
            everything with an archive and restarts the stack; reset erases all notes and derived
            data (a safety backup is taken first).
          </p>
        )}
      {exportPhase.step === "running" && <p className="muted">Building export archive…</p>}
      {exportPhase.step === "failed" && (
        <>
          <p className="ops-error">Export failed — see log.</p>
          <pre className="ops-update-log">{exportPhase.log}</pre>
        </>
      )}

      <input
        ref={fileRef}
        type="file"
        accept=".tar,.jbrain.tar"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) setImportPhase({ step: "picked", file, armed: false });
          e.target.value = "";
        }}
      />
      {importPhase.step === "picked" && (
        <>
          <p className="muted">
            {importPhase.file.name} · {fmtBytes(importPhase.file.size)}
          </p>
          <button
            type="button"
            className="danger"
            onClick={() => {
              if (!importPhase.armed) {
                setImportPhase({ ...importPhase, armed: true });
                return;
              }
              void startImport(importPhase.file);
            }}
            onBlur={() => setImportPhase({ ...importPhase, armed: false })}
          >
            {importPhase.armed
              ? "Tap again — current data is overwritten"
              : "Import — replaces ALL current data"}
          </button>
          <p className="muted data-hint">
            a safety backup of the current data is taken first; the stack restarts during import.
          </p>
        </>
      )}
      {importPhase.step === "uploading" && <p className="muted">Uploading archive…</p>}
      {importPhase.step === "running" && (
        <>
          <p className="muted">
            {importPhase.unreachable ? "Stack restarting — hold on…" : "Importing…"}
          </p>
          <pre className="ops-update-log">{importPhase.log}</pre>
        </>
      )}
      {importPhase.step === "done" && (
        <>
          <p className={importPhase.ok ? "muted" : "ops-error"}>
            {importPhase.ok ? "Import complete." : "Import failed — see log."}
          </p>
          <pre className="ops-update-log">{importPhase.log}</pre>
          {importPhase.ok && (
            <button type="button" onClick={() => window.location.reload()}>
              Reload app
            </button>
          )}
        </>
      )}
      {resetPhase.step === "running" && (
        <>
          <p className="muted">
            {resetPhase.unreachable ? "Worker restarting — hold on…" : "Resetting…"}
          </p>
          <pre className="ops-update-log">{resetPhase.log}</pre>
        </>
      )}
      {resetPhase.step === "done" && (
        <>
          <p className={resetPhase.ok ? "muted" : "ops-error"}>
            {resetPhase.ok ? "Reset complete." : "Reset failed — see log."}
          </p>
          <pre className="ops-update-log">{resetPhase.log}</pre>
          {resetPhase.ok && (
            // The stream and caches still hold pre-reset data; reload clears them.
            <button type="button" onClick={() => window.location.reload()}>
              Reload app
            </button>
          )}
        </>
      )}
    </section>
  );
}
