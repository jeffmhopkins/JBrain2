// The Math launcher's terminal WebSocket URL. The live-terminal bridge itself is shared
// with jcode (`attachTerminal` in ../jcode/terminal.ts) — this only points it at the
// jlaunch job PTY. The browser sends the owner session cookie on the same-origin
// handshake; the api authenticates the owner and proxies to the job's shell.

export { attachTerminal } from "../jcode/terminal";

/** The owner's terminal WebSocket for a job (proxied to the jlaunch PTY). */
export function jlaunchTerminalWsUrl(jid: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/jlaunch/jobs/${encodeURIComponent(jid)}/terminal`;
}
