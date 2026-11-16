import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { nodeSites, type HostedSite } from "../domain/nodeSites";
import { nodeApiUrl } from "../domain/nodeUrl";
import "./nodeSites.css";

/** Trusted installed site plugins provide their own owner page. */
export default function NodeSiteOwner() {
  const { siteId } = useParams();
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [site, setSite] = useState<HostedSite | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    setSite(null); setFailed(false);
    nodeSites.mine().then(({ sites }) => {
      if (!active) return;
      const found = sites.find(row => row.site_id === siteId);
      if (found?.owner_path?.startsWith(`/api/local/site-plugins/${encodeURIComponent(siteId || "")}/`)) setSite(found);
      else setFailed(true);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [siteId]);
  return <section className="ns-window">
    <header className="ns-frame-bar">
      <button className="ns-button" onClick={() => navigate("/apps/sites")}>{t("sites.back")}</button>
      <strong>{site?.title || t("sites.manage")}</strong>
    </header>
    {failed && <p className="ns-error" role="alert">{t("sites.errors.site_unavailable")}</p>}
    {site?.owner_path && <iframe className="ns-frame" title={site.title}
      src={`${nodeApiUrl(site.owner_path)}?lang=${encodeURIComponent(i18n.language)}`}
      sandbox="allow-scripts allow-same-origin allow-forms" referrerPolicy="no-referrer" />}
  </section>;
}
