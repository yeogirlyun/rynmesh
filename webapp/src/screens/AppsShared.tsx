import { useEffect, useState } from "react";
import { RefreshCw, Search, Star } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  getPins, recentShared, sharedApps, togglePin,
  type Pin, type SharedApp, type SharedAppProvider, type SharedAppsResult,
} from "../domain/apps";
import { nodeSites } from "../domain/nodeSites";
import "./nodeSites.css";

const filters = ["all", "online", "pinned"] as const;
const providerName = (provider: SharedAppProvider) => provider.member_name || provider.device_name;
const providerKey = (siteId: string, provider: SharedAppProvider) =>
  `shared:${provider.provider_resource}|${provider.peer_id}|${siteId}`;
const filterButtonStyle = { borderRadius: 999 };

export default function AppsShared() {
  const { t, i18n } = useTranslation("apps");
  const navigate = useNavigate();
  const [data, setData] = useState<SharedAppsResult | null>(null);
  const [pins, setPins] = useState<Pin[] | null>(null);
  const [filter, setFilter] = useState<typeof filters[number]>("all");
  const [query, setQuery] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [pending, setPending] = useState("");
  const [error, setError] = useState<"load" | "pins" | "pin" | "open" | null>(null);

  useEffect(() => {
    let active = true;
    void sharedApps().then(result => { if (active) setData(result); })
      .catch(() => { if (active) setError("load"); });
    void getPins().then(result => { if (active) setPins(result.pins); })
      .catch(() => { if (active) setError(current => current ?? "pins"); });
    return () => { active = false; };
  }, []);

  const refresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      setData(await sharedApps(true));
      if (!pins) setPins((await getPins()).pins);
    } catch {
      setError("load");
    } finally {
      setRefreshing(false);
    }
  };

  const act = async (action: "pin" | "open", app: SharedApp, device: SharedAppProvider) => {
    const key = providerKey(app.site_id, device);
    const provider = { resource: device.provider_resource, peer_id: device.peer_id, member_name: providerName(device) };
    setPending(key);
    setError(null);
    try {
      if (action === "pin") {
        const result = await togglePin({ key, title: app.title, app_id: app.site_id, kind: "shared", provider });
        setPins(result.pins);
      } else {
        const opened = await nodeSites.open(device, app.site_id);
        recentShared.add({ key, site_id: app.site_id, title: app.title, provider });
        const params = new URLSearchParams({ entry: opened.entry, title: opened.title });
        navigate(`/apps/sites/open/${opened.key}?${params}`);
      }
    } catch {
      setError(action);
    } finally {
      setPending("");
    }
  };

  const pinned = new Set(pins?.map(pin => pin.key));
  const search = query.trim().toLocaleLowerCase();
  const matches = (value: string) => value.toLocaleLowerCase().includes(search);
  const groups = data?.apps.map(app => ({
    app,
    providers: app.providers.filter(device =>
      (filter !== "online" || device.online) &&
      (filter !== "pinned" || pinned.has(providerKey(app.site_id, device))) &&
      (matches(app.title) || matches(device.member_name) || matches(device.device_name))),
  })).filter(group => group.providers.length);

  const status = (device: SharedAppProvider) => {
    if (!device.online && device.last_seen) {
      const seen = new Date(device.last_seen).toLocaleString(i18n.resolvedLanguage, {
        dateStyle: "medium", timeStyle: "short",
      });
      return t("sharedList.lastSeen", { time: seen });
    }
    const summary = device.summary;
    if (!device.online || !summary) return null;
    if (summary.state === "idle") return t("sharedList.idle");
    const seconds = summary.eta_seconds;
    const wait = seconds >= 60
      ? t("sharedList.minutes", { count: Math.ceil(seconds / 60) })
      : t("sharedList.seconds", { count: seconds });
    return t("sharedList.queue", { length: summary.queue_length, wait });
  };

  return <section aria-label={t("shared")} className="apps-hub-mine">
    <header className="apps-hub-header">
      {data && <p>{t("sharedList.summary", { total: data.colleagues_total, people: data.sharing_total, apps: data.apps.length })}</p>}
      <div className="apps-hub-actions">
        {filters.map(item => <button className={`apps-hub-button${filter === item ? " apps-hub-primary" : ""}`}
          style={filterButtonStyle} key={item} type="button" aria-pressed={filter === item}
          disabled={item === "pinned" && pins === null} onClick={() => setFilter(item)}>
          {t(`sharedList.filters.${item}`)}
        </button>)}
        <label className="apps-hub-search">
          <Search size={16} aria-hidden="true" />
          <input type="search" aria-label={t("search")} placeholder={t("searchPlaceholder")}
            value={query} onChange={event => setQuery(event.target.value)} />
        </label>
        <button type="button" className="apps-hub-button" disabled={refreshing || (!data && error !== "load")}
          onClick={() => void refresh()}>
          <RefreshCw size={16} aria-hidden="true" />{t(refreshing ? "sharedList.refreshing" : "sharedList.refresh")}
        </button>
      </div>
    </header>
    {error && <p role="alert" className="ns-error">{t(`sharedList.errors.${error}`)}</p>}
    {!data && error !== "load" && <p role="status" className="apps-hub-muted">{t("sharedList.loading")}</p>}
    {data && !data.apps.length && <p role="status" className="apps-hub-muted">{t("sharedList.empty")}</p>}
    {data && data.apps.length > 0 && groups?.length === 0 &&
      <p role="status" className="apps-hub-muted">{t("noMatches")}</p>}
    {groups?.map(({ app, providers }) => {
      const multiple = app.providers.length > 1;
      const title = <><span className="apps-hub-icon" aria-hidden="true">{Array.from(app.title)[0]}</span>
        <h2 className="ns-site-title" style={{ margin: 0, fontSize: 15 }}>{app.title}</h2></>;
      return <section className="ns-card" aria-label={app.title} key={app.site_id}>
        {multiple && <div className="ns-device">
          {title}
          <span className="apps-hub-muted">{t("sharedList.devices", {
            total: app.providers.length, online: app.providers.filter(device => device.online).length,
          })}</span>
        </div>}
        {providers.map(device => {
          const key = providerKey(app.site_id, device);
          const name = providerName(device);
          const detail = status(device);
          const isPinned = pinned.has(key);
          return <div className={multiple ? "ns-site" : "ns-device"} key={key}>
            {!multiple && title}
            <div style={{ flex: "1 1 150px", minWidth: 0 }}>
              <strong>{t("sharedList.device", { name })}</strong>
              {device.device_name && device.member_name && device.device_name !== name &&
                <p className="apps-hub-muted">{device.device_name}</p>}
            </div>
            <div className="apps-hub-actions" style={{ flex: "1 1 165px" }}>
              <span className="ns-pill" style={device.online ? { color: "var(--pf-green)" } : undefined}>
                {t(device.online ? "online" : "offline")}
              </span>
              {detail && <span className="apps-hub-muted">{detail}</span>}
            </div>
            <button className={`ns-button${device.online ? " is-primary" : ""}`} type="button"
              disabled={!device.online || !!pending} onClick={() => void act("open", app, device)}>
              {t("open")}
            </button>
            <button className="apps-hub-star" type="button" disabled={pins === null || !!pending}
              aria-label={t(isPinned ? "sharedList.unpin" : "sharedList.pin", { name, title: app.title })}
              aria-pressed={isPinned} onClick={() => void act("pin", app, device)}>
              <Star size={18} aria-hidden="true" fill={isPinned ? "currentColor" : "none"} />
            </button>
          </div>;
        })}
      </section>;
    })}
  </section>;
}
