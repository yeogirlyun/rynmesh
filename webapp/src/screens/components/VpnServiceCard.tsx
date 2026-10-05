import { tr, useUILanguage, uiLocale } from "../../uiI18n";
import { useCallback, useEffect, useReducer, useRef } from "react";
import type { NodeClient } from "../../domain/nodeClient";
import { egressReducer, initialEgressCardState } from "../../domain/egressCardState";

const REGION = "CN";
const POLL_MS = 5000;

export default function VpnServiceCard({ client }: { client: NodeClient }) {
  useUILanguage();
  const [state, dispatch] = useReducer(egressReducer, initialEgressCardState);
  const busy = state.kind === "connecting" || state.kind === "launching";
  const busyRef = useRef(busy);
  busyRef.current = busy;

  // Initial status + polling (skips polls while mid-action).
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (busyRef.current) return;
      try {
        const status = await client.egressStatus(REGION);
        if (alive) dispatch({ type: "STATUS", status });
      } catch {
        /* transient; next tick retries */
      }
    };
    void tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [client]);

  const onConnect = useCallback(async () => {
    dispatch({ type: "CONNECT" });
    try {
      const status = await client.egressConnect({ region: REGION });
      dispatch({ type: "RESOLVE", status }); // deriveFromStatus handles lastError/connected/available
    } catch (err) {
      dispatch({ type: "ERROR", message: (err as Error).message });
    }
  }, [client]);

  const onLaunch = useCallback(async () => {
    dispatch({ type: "LAUNCH" });
    try {
      const result = await client.egressLaunch({ region: REGION });
      // launch() returns HTTP 200 with a lastError body on failure (e.g. Chrome
      // never opened because avaryn-vpn is missing) — surface it instead of
      // optimistically reporting success.
      if (result?.lastError) {
        dispatch({ type: "ERROR", message: result.lastError });
        return;
      }
    } catch (err) {
      dispatch({ type: "ERROR", message: (err as Error).message });
      return;
    }
    dispatch({ type: "LAUNCHED" });
  }, [client]);

  const onDisconnect = useCallback(async () => {
    try {
      await client.egressDisconnect({ region: REGION });
    } finally {
      dispatch({ type: "DISCONNECT" });
    }
  }, [client]);

  return (
    <article className="service-card vpn-card">
      <header className="service-card-head">
        <span className="service-icon" aria-hidden>🛰️</span>
        <div>
          <h3>{tr("Shenzhen VPN")} <span className="service-tag">net.egress</span></h3>
          <p className="service-sub">{tr("Browse as if you're inside mainland China.")}</p>
        </div>
      </header>

      {state.kind === "available" && (
        <button className="btn primary" onClick={onConnect}>{tr("Connect · 1 credit")}</button>
      )}

      {state.kind === "connecting" && <p className="service-status">{tr("Setting up tunnel between nodes…")}</p>}

      {(state.kind === "connected" || state.kind === "launching") && (
        <div className="service-connected">
          <p className="service-status">
            {state.warning ? tr("⚠ Connected — exit not in CN") : tr("● Connected · Shenzhen")}
            {state.status.exitIp ? ` · ${state.status.exitIp}` : ""}
            {state.status.locVerified ? tr(" · loc=CN ✓") : ""}
            {state.status.uptimeSeconds != null ? tr(" · up {{count}}m", { count: Math.floor(state.status.uptimeSeconds / 60) }) : ""}
          </p>
          <p className="service-meta">
            {state.status.priceCredits ? `${tr("{{count}} credits", { count: state.status.priceCredits })} · ` : ""}
            {state.status.ttlExpiresAt ? tr("expires {{time}}", { time: new Date(state.status.ttlExpiresAt).toLocaleTimeString(uiLocale()) }) : ""}
          </p>
          <div className="service-actions">
            <button className="btn primary" disabled={state.kind === "launching"} onClick={onLaunch}>
              {state.kind === "launching" ? tr("Launching…") : tr("Watch CN TV")}
            </button>
            <button className="btn ghost" onClick={onDisconnect}>{tr("Disconnect")}</button>
          </div>
        </div>
      )}

      {state.kind === "error" && (
        <div className="service-error">
          <p className="service-status error">⚠ {state.message}</p>
          <button className="btn" onClick={onConnect}>{tr("Retry")}</button>
        </div>
      )}
    </article>
  );
}
