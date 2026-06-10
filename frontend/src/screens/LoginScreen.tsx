import { type FormEvent, useState } from "react";
import { ApiError, type Principal, api } from "../api/client";

/** Best-effort "Chrome on Android"-style default so devices are tellable apart. */
function defaultDeviceLabel(): string {
  const ua = navigator.userAgent;
  const browser = ua.includes("Firefox")
    ? "Firefox"
    : ua.includes("Edg")
      ? "Edge"
      : ua.includes("Chrome")
        ? "Chrome"
        : ua.includes("Safari")
          ? "Safari"
          : "Browser";
  const platform = ua.includes("Android")
    ? "Android"
    : /iPhone|iPad/.test(ua)
      ? "iOS"
      : ua.includes("Mac")
        ? "macOS"
        : ua.includes("Win")
          ? "Windows"
          : ua.includes("Linux")
            ? "Linux"
            : "device";
  return `${browser} on ${platform}`;
}

interface LoginScreenProps {
  onLogin: (principal: Principal) => void;
}

export function LoginScreen({ onLogin }: LoginScreenProps) {
  const [ownerKey, setOwnerKey] = useState("");
  const [deviceLabel, setDeviceLabel] = useState(defaultDeviceLabel);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(ownerKey.trim(), deviceLabel.trim());
      onLogin(await api.me());
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "Invalid owner key."
          : "Login failed. Is the server reachable?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <h1>JBrain</h1>
      <form onSubmit={submit}>
        <label htmlFor="owner-key">Owner key</label>
        <textarea
          id="owner-key"
          value={ownerKey}
          onChange={(e) => setOwnerKey(e.target.value)}
          placeholder="Paste your owner key"
          rows={3}
          autoComplete="off"
          required
        />
        <label htmlFor="device-label">Device label</label>
        <input
          id="device-label"
          value={deviceLabel}
          onChange={(e) => setDeviceLabel(e.target.value)}
          required
        />
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy || ownerKey.trim() === ""}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
