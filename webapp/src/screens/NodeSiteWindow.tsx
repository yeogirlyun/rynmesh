import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { NodeSiteError, nodeSites, type OpenedSiteStatus } from "../domain/nodeSites";
import "./nodeSites.css";

const POLL_MS = 15_000;
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
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { key = "" } = useParams();
  const [params] = useSearchParams();
  const entry = safeEntry(params.get("entry"));
  const [state, setState] = useState<OpenedSiteStatus | null>(null);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    let alive = true;
    let wasOnline: boolean | null = null;
    const check = async (fresh: boolean) => {
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
      }
    };
    void check(true);
    const timer = window.setInterval(() => void check(true), POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [key]);

  const src = useMemo(() => state && ORIGIN.test(state.origin) ? `${state.origin}${entry}` : "", [state, entry]);
  const owner = state?.member_name ? t("sites.deviceOf", { name: state.member_name }) : state?.device_name || "";
  const title = params.get("title") || state?.site_id || "";

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
      <button className="ns-button" onClick={() => navigate("/apps/sites")}>{t("sites.back")}</button>
      <div className="ns-frame-title">
        <strong>{[owner, title].filter(Boolean).join(" · ")}</strong>
        {owner && <small>{t("sites.frameNote", { name: owner })}</small>}
      </div>
      {state && <span className={`ns-pill${state.online ? " is-online" : ""}`}>
        {state.online ? t("sites.online") : t("sites.offline")}
      </span>}
    </div>
    {error ? <div className="ns-offline" role="alert">
      <p>{t(`sites.errors.${["site_denied", "site_not_found"].includes(error) ? error : "site_unavailable"}`)}</p>
    </div> : state && !state.online ? <div className="ns-offline" role="status">
      <h2>{t("sites.offlineTitle", { name: owner })}</h2>
      <p>{t("sites.offlineBody", { name: owner })}</p>
      {lastSeen && <p>{t("sites.lastSeen", { time: lastSeen })}</p>}
      <button className="ns-button" disabled={checking} onClick={() => void reconnect()}>{t("sites.reconnect")}</button>
    </div> : src ? <iframe key={generation} className="ns-frame" title={title || owner} src={src}
      sandbox="allow-scripts allow-forms allow-same-origin allow-downloads" referrerPolicy="no-referrer"
      allow="" /> : <p className="ns-muted" role="status">{t("sites.loading")}</p>}
  </section>;
}
