import { useEffect, useState } from "react";
import { Plus, Search, Star } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  APP_PINS_CHANGE_EVENT, getPins, loadMyApps, recentShared, sharedApps, togglePin,
  type MyApp, type Pin, type RecentSharedApp, type SharedApp,
} from "../domain/apps";
import { nodeSites } from "../domain/nodeSites";
import { InstalledAppControls } from "./AppsLauncher";
import AppsDiscover from "./AppsDiscover";
import AppsShared from "./AppsShared";
import CraftApps from "./CraftApps";
import "./apps.css";

const tabs = ["mine", "shared", "discover"] as const;
type Tab = typeof tabs[number];
const tabPath = (tab: Tab) => `/apps?tab=${tab}`;

export default function AppsHub() {
  const { t } = useTranslation("apps");
  const [params] = useSearchParams();
  const selected = params.get("tab");
  const tab: Tab = selected === "shared" || selected === "discover" ? selected : "mine";
  const [query, setQuery] = useState("");
  return <main className="apps-hub">
    <header className="apps-hub-header">
      <div><h1>{t("title")}</h1><p>{t("subtitle")}</p></div>
      <div className="apps-hub-actions">
        <label className="apps-hub-search">
          <Search size={16} aria-hidden="true" />
          <input type="search" aria-label={t("search")} placeholder={t("searchPlaceholder")}
            value={query} disabled={tab === "discover"} onChange={event => setQuery(event.target.value)} />
        </label>
        <Link className="apps-hub-button apps-hub-primary" to={tabPath("discover")}>
          <Plus size={16} aria-hidden="true" />{t("add")}
        </Link>
      </div>
    </header>
    <nav className="apps-hub-tabs" aria-label={t("title")}>
      {tabs.map(item => <Link to={tabPath(item)} key={item} aria-current={tab === item ? "page" : undefined}>{t(item)}</Link>)}
    </nav>
    {tab === "mine" && <MyApps query={query.trim().toLocaleLowerCase()} />}
    {tab === "shared" && <AppsShared query={query} />}
    {tab === "discover" && <AppsDiscover />}
  </main>;
}

type Card = {
  key: string;
  title: string;
  subtitle: string;
  openPath?: string;
  sharedApp?: RecentSharedApp;
  online?: boolean;
  shared?: boolean;
  pin?: Pin;
  pinned?: boolean;
};

function MyApps({ query }: { query: string }) {
  const { t } = useTranslation("apps");
  const navigate = useNavigate();
  const [pins, setPins] = useState<Pin[]>([]);
  const [apps, setApps] = useState<MyApp[]>([]);
  const [remote, setRemote] = useState<SharedApp[]>([]);
  const [recent] = useState(() => recentShared.get());
  const [changing, setChanging] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<"loadPinsError" | "pinError" | "openError" | null>(null);

  useEffect(() => {
    let active = true;
    const loadPins = () => void getPins().then(result => {
      if (active) setPins(result.pins);
    }).catch(() => { if (active) setError("loadPinsError"); });
    window.addEventListener(APP_PINS_CHANGE_EVENT, loadPins);
    loadPins();
    void loadMyApps().then(result => { if (active) setApps(result); });
    void sharedApps().then(result => { if (active) setRemote(result.apps); }).catch(() => {});
    return () => {
      active = false;
      window.removeEventListener(APP_PINS_CHANGE_EVENT, loadPins);
    };
  }, []);

  const matches = (...values: string[]) => values.some(value => value.toLocaleLowerCase().includes(query));

  const changePin = async (pin: Pin) => {
    setError(null);
    setChanging(true);
    try {
      setPins((await togglePin(pin)).pins);
    } catch {
      setError("pinError");
    } finally {
      setChanging(false);
    }
  };

  const openShared = async (app: RecentSharedApp) => {
    setOpening(true);
    setError(null);
    try {
      const opened = await nodeSites.open({ provider_resource: app.provider.resource, peer_id: app.provider.peer_id }, app.site_id);
      recentShared.add(app);
      const params = new URLSearchParams({ entry: opened.entry, title: opened.title });
      navigate(`/apps/sites/open/${opened.key}?${params}`);
    } catch {
      setError("openError");
    } finally {
      setOpening(false);
    }
  };

  const sharedCard = (app: RecentSharedApp): Card => {
    const { resource, peer_id, member_name } = app.provider;
    const provider = remote.find(item => item.site_id === app.site_id)?.providers
      .find(item => item.provider_resource === resource && item.peer_id === peer_id);
    const online = provider?.online ?? false;
    return {
      key: app.key, title: app.title, sharedApp: app, online,
      subtitle: t("sharedStatus", { name: provider?.member_name || member_name, status: t(online ? "online" : "offline") }),
    };
  };

  const localCard = (app: MyApp, pin?: Extract<Pin, { kind: "local" }>): Card => {
    const openPath = pin?.open_path ?? app.openPath;
    return {
      key: app.key, title: pin?.title ?? app.title, openPath, shared: app.shared === true,
      subtitle: app.version ? t("installedVersion", { version: app.version }) : t("installed"),
      pin: pin ?? { key: app.key, title: app.title, kind: "local", app_id: app.id, open_path: openPath },
      pinned: Boolean(pin || pins.some(item => item.key === app.key)),
    };
  };

  const pinnedCards = pins.flatMap((pin): Card[] => {
    if (pin.kind === "shared") {
      return [{ ...sharedCard({ key: pin.key, title: pin.title, site_id: pin.app_id, provider: pin.provider }), pin, pinned: true }];
    }
    const app = apps.find(item => item.key === pin.key);
    return [app ? localCard(app, pin) : {
      key: pin.key, title: pin.title, openPath: pin.open_path, subtitle: t("installed"), pin, pinned: true,
    }];
  });

  const cards = (items: Card[], empty?: string) => {
    const filtered = items.filter(card => matches(card.title, card.sharedApp?.provider.member_name ?? ""));
    if (!filtered.length) return empty && <p className="apps-hub-muted">{empty}</p>;
    return <div className="apps-hub-grid">{filtered.map(card => <article className="apps-hub-card" key={card.key}>
      <div className="apps-hub-card-heading">
        <span className="apps-hub-icon" aria-hidden="true">{Array.from(card.title)[0]}</span>
        <div className="apps-hub-card-text">
          <h3>{card.title}</h3>
          <p className={card.online ? "apps-hub-online" : "apps-hub-muted"}>{card.subtitle}</p>
        </div>
        {card.pin && <button className="apps-hub-star" type="button" disabled={changing}
          aria-label={t(card.pinned ? "unpin" : "pin", { title: card.title })} aria-pressed={Boolean(card.pinned)}
          onClick={() => void changePin(card.pin!)}>
          <Star size={17} fill={card.pinned ? "currentColor" : "none"} aria-hidden="true" />
        </button>}
      </div>
      <div className="apps-hub-card-actions">
        {card.openPath && <Link className="apps-hub-button" to={card.openPath}>{t("open")}</Link>}
        {card.sharedApp && <button className="apps-hub-button" type="button" disabled={!card.online || opening}
          onClick={() => void openShared(card.sharedApp!)}>{t("open")}</button>}
        {card.shared && <span className="apps-hub-badge">{t("sharedBadge")}</span>}
      </div>
    </article>)}</div>;
  };

  const publicApps = apps.filter(app => app.kind === "public").map(app => localCard(app));

  return <div className="apps-hub-mine">
    {error && <p role="alert">{t(error)}</p>}
    <section aria-labelledby="apps-pinned-heading">
      <h2 id="apps-pinned-heading">{t("pinned")}</h2>
      {cards(pinnedCards, t(query ? "noMatches" : "noPins"))}
    </section>
    <section aria-labelledby="apps-installed-heading">
      <h2 id="apps-installed-heading">{t("installed")}</h2>
      {cards(publicApps)}
      <InstalledAppControls.Provider value={{ query, pins, changing, changePin }}>
        <CraftApps />
      </InstalledAppControls.Provider>
    </section>
    <section aria-labelledby="apps-recent-heading">
      <div className="apps-hub-section-heading">
        <h2 id="apps-recent-heading">{t("recent")}</h2>
        <Link to={tabPath("shared")}>{t("allShared")}</Link>
      </div>
      {cards(recent.map(sharedCard), t(query ? "noMatches" : "noRecent"))}
    </section>
  </div>;
}
