import { KeyRound } from "lucide-react";
import { useCallback, useState } from "react";

// Shows the device token so the owner can pair a phone or a second machine
// against a tunnelled node. Reachable only from an already-authorized session,
// which in practice means someone sitting at the machine.

function baseUrl(): string {
  const explicit = import.meta.env.VITE_RYN_NODE_BASE_URL;
  if (explicit) return explicit;
  const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  return isTauri ? "http://127.0.0.1:8791/api/local" : "/api/local";
}

export default function AccessPanel() {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${baseUrl()}/auth/token`, { credentials: "include" });
      if (!response.ok) throw new Error(String(response.status));
      setToken(((await response.json()) as { token: string }).token);
    } catch {
      setError("Could not read the token from the local node.");
    } finally {
      setBusy(false);
    }
  }, []);

  const rotate = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${baseUrl()}/auth/rotate`, {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) throw new Error(String(response.status));
      setToken(((await response.json()) as { token: string }).token);
    } catch {
      setError("Could not rotate the token.");
    } finally {
      setBusy(false);
    }
  }, []);

  const copy = useCallback(async () => {
    await navigator.clipboard.writeText(token);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }, [token]);

  return (
    <div className="access-panel">
      <div className="setting-row">
        <span>
          <b>Device token</b>
          <small>
            Pair another device when you reach this node over a tunnel. Anyone
            with this token can control the node — treat it like a password.
          </small>
        </span>
        <div className="access-actions">
          {token ? (
            <>
              <code className="access-token">{token}</code>
              <button type="button" onClick={copy} disabled={busy}>
                {copied ? "Copied" : "Copy"}
              </button>
              <button type="button" onClick={rotate} disabled={busy}>
                Rotate
              </button>
            </>
          ) : (
            <button type="button" onClick={load} disabled={busy}>
              <KeyRound size={14} /> {busy ? "Reading…" : "Show token"}
            </button>
          )}
        </div>
      </div>
      {error ? <p className="service-status error">⚠ {error}</p> : null}
      <p className="access-hint">
        Rotating invalidates every paired device immediately.
      </p>
    </div>
  );
}
