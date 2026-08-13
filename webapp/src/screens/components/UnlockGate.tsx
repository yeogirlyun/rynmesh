import { useCallback, useEffect, useState } from "react";
import { authApi } from "../../domain/authClient";

type Props = { children: React.ReactNode };

/**
 * Blocks the app until the node accepts this browser.
 *
 * Sitting at the machine this never appears — the node trusts a loopback
 * request that nothing proxied. Reached through a tunnel it asks once for the
 * device token, then a session cookie carries the access.
 */
export default function UnlockGate({ children }: Props) {
  const [checked, setChecked] = useState(false);
  const [authorized, setAuthorized] = useState(true);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    authApi
      .status()
      .then((status) => {
        if (!live) return;
        setAuthorized(status.authorized);
        setChecked(true);
      })
      .catch(() => {
        // The node is unreachable rather than locked — let the normal shell
        // render its offline state instead of showing a token prompt.
        if (!live) return;
        setAuthorized(true);
        setChecked(true);
      });
    return () => {
      live = false;
    };
  }, []);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      if (!token.trim() || busy) return;
      setBusy(true);
      const message = await authApi.unlock(token);
      setBusy(false);
      if (message) {
        setError(message);
        return;
      }
      setError("");
      setToken("");
      setAuthorized(true);
    },
    [token, busy],
  );

  if (!checked) return null;
  if (authorized) return <>{children}</>;

  return (
    <div className="unlock-gate">
      <form className="unlock-card" onSubmit={submit}>
        <h1>Unlock this node</h1>
        <p>
          You're reaching this node remotely. Paste its device token to pair this
          browser — you'll only need to do it once.
        </p>
        <label htmlFor="ryn-device-token">Device token</label>
        <input
          id="ryn-device-token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(event) => {
            setToken(event.target.value);
            setError("");
          }}
          placeholder="paste token"
        />
        {error ? (
          <p className="unlock-error" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" disabled={busy || !token.trim()}>
          {busy ? "Checking…" : "Unlock"}
        </button>
        <p className="unlock-hint">
          Find it on the node machine at <code>~/.rynmesh/control_token</code>, or
          in Settings → Access while you're sitting at it.
        </p>
      </form>
    </div>
  );
}
