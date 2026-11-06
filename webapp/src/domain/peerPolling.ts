import type { NodeClient } from "./nodeClient";
import { startPoller, type Poller } from "./polling";
import type { Peer } from "./types";

export const PEERS_POLL_MS = 5000;
export const PEERS_FULL_POLL_MS = 15000;
export const PEERS_QUICK_TIMEOUT_MS = 5000;
export const PEERS_FULL_TIMEOUT_MS = 20000;
export const PEERS_HIDDEN_POLL_MS = 30000;

/**
 * The shell's device list. Quick (cached) and full discovery share one poller,
 * so they never overlap or pile up in the webview's connection pool. Full
 * discovery runs at most every 15 s and only while the window is visible.
 * `apply(items, merge)`: quick results merge into the list, full ones replace it.
 */
export function startPeerPolling(
  client: Pick<NodeClient, "listPeers">,
  apply: (items: Peer[], merge: boolean) => void,
): Poller {
  let lastFull = Number.NEGATIVE_INFINITY;
  let fullRun = false;
  return startPoller({
    intervalMs: PEERS_POLL_MS,
    hiddenIntervalMs: PEERS_HIDDEN_POLL_MS,
    timeoutMs: () => {
      // Decided once per run; the poller reads the timeout right before it calls run.
      fullRun = document.visibilityState !== "hidden" && Date.now() - lastFull >= PEERS_FULL_POLL_MS;
      return fullRun ? PEERS_FULL_TIMEOUT_MS : PEERS_QUICK_TIMEOUT_MS;
    },
    run: async (signal) => {
      const full = fullRun;
      if (full) lastFull = Date.now();
      // A failure keeps the latest local or cached discovery; the poller backs off.
      const items = await client.listPeers(full ? undefined : { quick: true }, { signal });
      if (!signal.aborted) apply(items, !full);
    },
  });
}
