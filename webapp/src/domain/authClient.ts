// Control-surface auth. On the desktop the node trusts the loopback socket and
// nothing here ever renders; over a tunnel the node returns 401 and the owner
// pastes the device token once to get a session cookie.

export type AuthStatus = {
  authorized: boolean;
  via: "local" | "session" | "perimeter" | "";
  remote: boolean;
};

function baseUrl(): string {
  const explicit = import.meta.env.VITE_RYN_NODE_BASE_URL;
  if (explicit) return explicit;
  const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  return isTauri ? "http://127.0.0.1:8791/api/local" : "/api/local";
}

export const authApi = {
  async status(): Promise<AuthStatus> {
    const response = await fetch(`${baseUrl()}/auth/status`, { credentials: "include" });
    if (!response.ok) throw new Error(`auth status ${response.status}`);
    return (await response.json()) as AuthStatus;
  },

  /** Returns an error message, or "" on success. */
  async unlock(token: string): Promise<string> {
    const response = await fetch(`${baseUrl()}/auth/unlock`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token.trim() }),
    });
    if (response.ok) return "";
    if (response.status === 429) {
      return "Too many attempts. Wait five minutes and try again.";
    }
    return "That token didn't match. Check it and try again.";
  },
};
