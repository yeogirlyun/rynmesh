import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { NodeSiteError, nodeSites, type OpenedSiteStatus } from "../domain/nodeSites";
import { isPinned, recentShared, sharedApps, togglePin, type Pin, type SharedAppProvider } from "../domain/apps";
import "./nodeSites.css";

const POLL_MS = 10_000;
const ORIGIN = /^http:\/\/[0-9a-f]{32}\.site\.localhost:\d{4,5}$/;

/** The site's origin-relative entry path; anything else falls back to "/". */
function safeEntry(value: string | null): string {
  return value && value.startsWith("/") && !value.startsWith("//") && !/[\\\s#]/.test(value) ? value : "/";
}

/**
 * A page served by another device. The bar above the page is drawn by Ryn, so
 * the page cannot fake which computer it comes from or whether it is online.
 */
export default function NodeSiteWindow() {
  const { key = "" } = useParams();
  return <SharedWindow key={key} siteKey={key} />;
}

function SharedWindow({ siteKey: key }: { siteKey: string }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const entry = safeEntry(params.get("entry"));
  const [state, setState] = useState<(OpenedSiteStatus & { provider_resource?: string }) | null>(null);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    let alive = true;
    let wasOnline: boolean | null = null;
    let inFlight = false;
    const check = async (fresh: boolean) => {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await nodeSites.status(key, fresh);
        if (!alive) return;
        setError("");
        setState(value);
        // Coming back online reloads the page instead of leaving a stale error inside the frame.
        if (value.online && wasOnline === false) setGeneration(n => n + 1);
        wasOnline = value.online;
      } catch (err) {
        if (!alive) return;
        setError(err instanceof NodeSiteError ? err.code : "site_unavailable");
      } finally {
        inFlight = false;
      }
    };
    void check(true);
    const timer = window.setInterval(() => void check(true), POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [key]);

  const src = useMemo(() => {
    if (!state || !ORIGIN.test(state.origin)) return "";
    // Tell the page which language Ryn is showing; the page may ignore it.
    const url = new URL(entry, state.origin);
    url.searchParams.set("lang", i18n.language || "zh-CN");
    return url.href;
  }, [state, entry, i18n.language]);
  const owner = state?.member_name ? t("sites.deviceOf", { name: state.member_name }) : state?.device_name || "";
  const title = params.get("title") || state?.site_id || "";

  const siteId = state?.site_id;
  const peerId = state?.peer_id;
  const resource = state?.provider_resource;
  const memberName = state?.member_name || "";
  const pin = useMemo<Extract<Pin, { kind: "shared" }> | null>(() => {
    if (!siteId || !peerId || !resource || resource === "local") return null;
    return { key: `shared:${resource}|${peerId}|${siteId}`, kind: "shared", app_id: siteId, title,
      provider: { resource, peer_id: peerId, member_name: memberName } };
  }, [siteId, peerId, resource, title, memberName]);
  const [pinned, setPinned] = useState(false);
  const [action, setAction] = useState<"pin" | "switch" | null>(null);
  const [actionError, setActionError] = useState("");
  const [others, setOthers] = useState<SharedAppProvider[]>([]);

  useEffect(() => {
    if (!pin) return;
    let active = true;
    isPinned(pin.key).then(value => { if (active) setPinned(value); }).catch(() => {});
    return () => { active = false; };
  }, [pin]);

  useEffect(() => {
    if (pin && state?.online && src && !error) {
      recentShared.add({ key: pin.key, site_id: pin.app_id, title: pin.title, provider: pin.provider });
    }
  }, [pin, state?.online, src, error]);

  useEffect(() => {
    if (!siteId || !peerId) return;
    let active = true;
    sharedApps().then(result => {
      const app = result.apps.find(item => item.site_id === siteId);
      if (active) setOthers(app?.providers.filter(p => p.online && p.peer_id !== peerId) ?? []);
    }).catch(() => { if (active) setOthers([]); });
    return () => { active = false; };
  }, [siteId, peerId, state?.online]);

  const changePin = async () => {
    if (!pin) return;
    setAction("pin");
    setActionError("");
    try {
      const result = await togglePin(pin);
      setPinned(result.pins.some(item => item.key === pin.key));
    } catch {
      setActionError(t("sites.pinFailed"));
    } finally {
      setAction(null);
    }
  };

  const switchDevice = async (provider: SharedAppProvider) => {
    if (!siteId) return;
    setAction("switch");
    setActionError("");
    try {
      const opened = await nodeSites.open(provider, siteId);
      const search = new URLSearchParams({ entry: opened.entry, title: opened.title });
      navigate(`/apps/sites/open/${encodeURIComponent(opened.key)}?${search}`);
    } catch {
      setActionError(t("sites.switchFailed"));
    } finally {
      setAction(null);
    }
  };
  const picker = others.length > 0 && <select className="ns-button" style={{width: "auto", maxWidth: "100%"}} value="" disabled={action !== null}
    aria-label={t("sites.switchDevice")} onChange={e => void switchDevice(others[Number(e.target.value)])}>
    <option value="" disabled>{t("sites.switchDevice")} ({others.length})</option>
    {others.map((device, index) => <option key={device.provider_resource + "|" + device.peer_id} value={index}>
      {[device.member_name, device.device_name].filter(Boolean).join(" · ")}
    </option>)}
  </select>;

  const reconnect = async () => {
    setChecking(true);
    try {
      const value = await nodeSites.status(key, true);
      setState(value);
      if (value.online) setGeneration(n => n + 1);
    } catch (err) {
      setError(err instanceof NodeSiteError ? err.code : "site_unavailable");
    } finally {
      setChecking(false);
    }
  };

  const lastSeen = state?.last_seen ? new Date(state.last_seen).toLocaleString() : "";
  return <section className="ns-window">
    <div className="ns-frame-bar">
      <button className="ns-button" onClick={() => navigate("/apps?tab=shared")}>{t("sites.back")}</button>
      <div className="ns-frame-title">
        <strong>{[owner, title].filter(Boolean).join(" · ")}</strong>
        {owner && <small>{t("sites.frameNote", { name: owner })}</small>}
      </div>
      {state && <span className={`ns-pill${state.online ? " is-online" : ""}`}>
        {state.online ? t("sites.online") : t("sites.offline")}
      </span>}
      {pin && <button className="ns-button" aria-pressed={pinned} disabled={action !== null}
        onClick={() => void changePin()}><span aria-hidden="true" style={{color: pinned ? "var(--accent-amber, #b77908)" : undefined}}>
          {pinned ? "★" : "☆"}</span> {t(pinned ? "sites.pinned" : "sites.pin")}</button>}
      {picker}
    </div>
    {actionError && <p className="ns-error" role="alert">{actionError}</p>}
    {error ? <div className="ns-offline" role="alert">
      <p>{t(`sites.errors.${["site_denied", "site_not_found"].includes(error) ? error : "site_unavailable"}`)}</p>
    </div> : state && !state.online ? <div className="ns-offline" role="status">
      <h2>{t("sites.offlineTitle", { name: owner })}</h2>
      <p>{t("sites.offlineBody", { name: owner })}</p>
      {lastSeen && <p>{t("sites.lastSeen", { time: lastSeen })}</p>}
      <button className="ns-button" disabled={checking} onClick={() => void reconnect()}>{t("sites.reconnect")}</button>
      {picker}
    </div> : src ? <iframe key={generation} className="ns-frame" title={title || owner} src={src}
      sandbox="allow-scripts allow-forms allow-same-origin allow-downloads" referrerPolicy="no-referrer"
      allow="" /> : <p className="ns-muted" role="status">{t("sites.loading")}</p>}
  </section>;
}
