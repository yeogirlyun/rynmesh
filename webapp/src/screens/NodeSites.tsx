import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  NodeSiteError,
  nodeSites,
  type HostedSite,
  type RemoteSiteDevice,
  type SiteMember,
} from "../domain/nodeSites";
import "./nodeSites.css";

function errorText(t: (key: string) => string, error: unknown): string {
  const code = error instanceof NodeSiteError ? error.code : "site_unavailable";
  const known = ["site_denied", "site_not_found", "site_offline", "site_rate_limited"];
  return t(`sites.errors.${known.includes(code) ? code : "site_unavailable"}`);
}

function initial(name: string): string {
  return Array.from(name.trim())[0] || "?";
}

/** Pages colleagues opened to this device, and the pages this device can open to them. */
export default function NodeSites() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [devices, setDevices] = useState<RemoteSiteDevice[] | null>(null);
  const [mine, setMine] = useState<HostedSite[]>([]);
  const [members, setMembers] = useState<SiteMember[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const loadRemote = useCallback(async (refresh: boolean) => {
    try {
      setDevices(await nodeSites.remote(refresh));
    } catch (err) {
      setDevices([]);
      setError(errorText(t, err));
    }
  }, [t]);

  useEffect(() => {
    void loadRemote(false);
    nodeSites.mine().then(value => { setMine(value.sites); setMembers(value.members); }).catch(() => {});
  }, [loadRemote]);

  const open = async (device: RemoteSiteDevice, siteId: string) => {
    setBusy(`${device.peer_id}/${siteId}`);
    setError("");
    try {
      const opened = await nodeSites.open(device, siteId);
      navigate(`/apps/sites/open/${opened.key}?entry=${encodeURIComponent(opened.entry)}`
        + `&title=${encodeURIComponent(opened.title)}`);
    } catch (err) {
      setError(errorText(t, err));
    } finally {
      setBusy("");
    }
  };

  const save = async (site: HostedSite, changes: Partial<Pick<HostedSite, "enabled" | "audience">>) => {
    setBusy(`mine/${site.site_id}`);
    setError("");
    try {
      const saved = await nodeSites.update(site.site_id, changes);
      setMine(current => current.map(row => row.site_id === saved.site_id ? saved : row));
    } catch (err) {
      setError(errorText(t, err));
    } finally {
      setBusy("");
    }
  };

  const deviceName = (device: RemoteSiteDevice) =>
    device.member_name ? t("sites.deviceOf", { name: device.member_name }) : device.device_name;

  return <section className="ns-page">
    <header>
      <div>
        <h1>{t("sites.title")}</h1>
        <p className="ns-muted">{t("sites.subtitle")}</p>
      </div>
      <button className="ns-button" onClick={() => { setDevices(null); void loadRemote(true); }}>{t("sites.refresh")}</button>
    </header>
    {error && <p className="ns-error" role="alert">{error}</p>}

    <div>
      <h2>{t("sites.remoteHeading")}</h2>
      {devices === null ? <p className="ns-muted" role="status">{t("sites.loading")}</p>
        : devices.length === 0 ? <p className="ns-muted">{t("sites.noDevices")}</p>
          : <div className="ns-card">
            {devices.map(device => <div key={`${device.provider_resource}/${device.peer_id}`}>
              <div className="ns-device">
                <div className="ns-avatar" aria-hidden="true">{initial(device.member_name || device.device_name)}</div>
                <div className="ns-device-main">
                  <div className="ns-device-name">{deviceName(device)}
                    <span className={`ns-pill${device.online ? " is-online" : ""}`}>
                      {device.online ? t("sites.online") : t("sites.offline")}
                    </span>
                  </div>
                  <p className="ns-muted">{device.online && device.sites.length
                    ? t("sites.siteCount", { count: device.sites.length }) : t("sites.noSites")}</p>
                </div>
              </div>
              {device.online && device.sites.map(site => <div className="ns-site" key={site.site_id}>
                <span className="ns-site-title">{site.title}</span>
                <button className="ns-button is-primary" disabled={Boolean(busy)}
                  onClick={() => void open(device, site.site_id)}>
                  {busy === `${device.peer_id}/${site.site_id}` ? t("sites.opening") : t("sites.open")}
                </button>
              </div>)}
            </div>)}
          </div>}
    </div>

    <div>
      <h2>{t("sites.mineHeading")}</h2>
      {mine.length === 0 ? <p className="ns-muted">{t("sites.mineEmpty")}</p> : <div className="ns-card">
        {mine.map(site => <div className="ns-mine" key={site.site_id}>
          <div className="ns-mine-row">
            <strong style={{ flex: "1 1 200px" }}>{site.title}</strong>
            <label>
              <input type="checkbox" checked={site.enabled} disabled={Boolean(busy)}
                onChange={event => void save(site, { enabled: event.target.checked })} />
              {t("sites.openToColleagues")}
            </label>
            <span className={`ns-pill${site.enabled ? " is-online" : ""}`}>
              {site.enabled ? t("sites.openState") : t("sites.closedState")}
            </span>
            {site.site_id === "image-studio" && <button className="ns-button"
              onClick={() => navigate("/apps/sites/image-studio")}>{t("sites.manage")}</button>}
          </div>
          <div className="ns-mine-row">
            <label>
              <input type="radio" name={`audience-${site.site_id}`} checked={site.audience.mode === "all"}
                disabled={Boolean(busy)} onChange={() => void save(site, { audience: { mode: "all", members: [] } })} />
              {t("sites.audienceAll")}
            </label>
            <label>
              <input type="radio" name={`audience-${site.site_id}`} checked={site.audience.mode === "members"}
                disabled={Boolean(busy)}
                onChange={() => void save(site, { audience: { mode: "members", members: site.audience.members } })} />
              {t("sites.audienceMembers")}
            </label>
          </div>
          {site.audience.mode === "members" && <fieldset className="ns-members" disabled={Boolean(busy)}>
            <legend className="ns-muted">{t("sites.chooseMembers")}</legend>
            {members.map(member => <label key={`${member.provider_resource}/${member.member_id}`}>
              <input type="checkbox" checked={site.audience.members.includes(member.member_id)}
                onChange={event => {
                  const next = new Set(site.audience.members);
                  if (event.target.checked) next.add(member.member_id); else next.delete(member.member_id);
                  void save(site, { audience: { mode: "members", members: [...next] } });
                }} />
              {member.member_name || member.member_id}
            </label>)}
          </fieldset>}
          {busy === `mine/${site.site_id}` && <p className="ns-muted" role="status">{t("sites.saving")}</p>}
        </div>)}
      </div>}
    </div>
  </section>;
}
