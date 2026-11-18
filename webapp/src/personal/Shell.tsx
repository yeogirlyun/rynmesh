import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { tr } from "../uiI18n";
import {
  Box,
  Building2,
  CheckSquare,
  ChevronDown,
  Compass,
  Globe,
  Home,
  HardDrive,
  Menu,
  MessageCircle,
  Moon,
  Network,
  Search,
  Settings,
  Sun,
  UserRound,
  X,
} from "lucide-react";
import { DeviceArt, Empty, Modal, ServiceArt } from "./components";
import { personalHref, usePersonal } from "./model";
import { AgentNavigationContext } from "./agentNavigation";
import { DropActivity } from "./drop/DropActivity";
import { pinnedPluginWorkspaces, PIN_CHANGE_EVENT, pluginWorkspacePath } from "../screens/pluginWorkspaceNavigation";
import "../screens/rynAgentText";
import { craftPlugins } from "../domain/craftPlugins";
import { agentExtensions } from "../domain/agentExtensions";
import { APP_PINS_CHANGE_EVENT, getPins, putPins, type Pin } from "../domain/apps";
import { NodeSiteError, nodeSites } from "../domain/nodeSites";
import { publicPlugins } from "../domain/publicPlugins";
import { installedSidebarApps } from "./installedSidebarApps";
import { DesktopUpdateButton } from "../screens/UpdatesPanel";
import { isTauriDesktop } from "../domain/nodeUrl";
export function PersonalShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { t: agentText } = useTranslation("rynAgent");
  const { demo, appearance, setAppearance, devices, services, space } = usePersonal();
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [pinnedPlugins, setPinnedPlugins] = useState(pinnedPluginWorkspaces);
  const [sidebarPins, setSidebarPins] = useState<Pin[]>([]);
  const [openingPin, setOpeningPin] = useState(false);
  const [pinOpenError, setPinOpenError] = useState("");
  useEffect(() => {
    if (demo) return;
    let stopped = false, loading = false, refreshPending = false;
    const refresh = async () => {
      if (loading) { refreshPending = true; return; }
      loading = true;
      try {
        const [snapshot, craft, extensions, published] = await Promise.all([
          getPins(), craftPlugins.catalog().catch(() => null),
          agentExtensions.overview().catch(() => null), publicPlugins.overview().catch(() => null),
        ]);
        if (stopped) return;
        let { pins } = snapshot;
        if (!snapshot.seeded && extensions) {
          const initialPins: Pin[] = installedSidebarApps([], extensions.plugins).map(app => ({
            key: `local:${app.id}`, kind: "local", app_id: app.id, title: app.title, open_path: app.to,
          }));
          // The user may have pinned something while the catalogs were loading.
          const latest = await getPins();
          if (stopped) return;
          pins = latest.seeded ? latest.pins : (await putPins(initialPins)).pins;
        }
        const enabledLocalApps = new Set([
          ...(craft?.plugins ?? []).filter(app => app.installed && app.enabled).map(app => app.id),
          ...(extensions?.plugins ?? []).filter(app => app.enabled && (app.web_app || app.views?.length || app.peer_provider)).map(app => app.id),
          ...(published?.plugins ?? []).filter(app => app.installed_version && app.enabled).map(app => app.id),
        ]);
        if (!stopped) setSidebarPins(pins.filter(pin => pin.kind === "shared" || enabledLocalApps.has(pin.app_id)));
      } catch { /* Keep the last successful sidebar while the local node starts. */ }
      finally {
        loading = false;
        if (refreshPending && !stopped) {
          refreshPending = false;
          void refresh();
        }
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 10000);
    window.addEventListener("focus", refresh);
    window.addEventListener("ryn-installed-apps-changed", refresh);
    window.addEventListener(APP_PINS_CHANGE_EVENT, refresh);
    return () => {
      stopped = true;
      clearInterval(timer);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("ryn-installed-apps-changed", refresh);
      window.removeEventListener(APP_PINS_CHANGE_EVENT, refresh);
    };
  }, [demo]);
  const [agentNavCollapsed, setAgentNavCollapsed] = useState(() => {
    try { return sessionStorage.getItem("ryn.agent.navigation") === "closed" || (window.innerWidth <= 760 && sessionStorage.getItem("ryn.agent.navigation") !== "open"); }
    catch { return window.innerWidth <= 760; }
  });
  const navigate = useNavigate();
  const location = useLocation();
  const openSharedPin = async (pin: Extract<Pin, { kind: "shared" }>) => {
    if (openingPin) return;
    setOpeningPin(true);
    try {
      const opened = await nodeSites.open({ provider_resource: pin.provider.resource, peer_id: pin.provider.peer_id }, pin.app_id);
      navigate(`/apps/sites/open/${opened.key}?${new URLSearchParams({ entry: opened.entry, title: opened.title })}`);
    } catch (error) {
      const known = ["site_denied", "site_not_found", "site_offline", "site_rate_limited"];
      const code = error instanceof NodeSiteError && known.includes(error.code) ? error.code : "site_unavailable";
      setPinOpenError(t(`sites.errors.${code}`));
    } finally {
      setOpeningPin(false);
    }
  };
  const isStandalone = new URLSearchParams(location.search).get("standalone") === "1";
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const syncPin = () => setPinnedPlugins(pinnedPluginWorkspaces());
    window.addEventListener(PIN_CHANGE_EVENT, syncPin);
    window.addEventListener("storage", syncPin);
    return () => {
      window.removeEventListener(PIN_CHANGE_EVENT, syncPin);
      window.removeEventListener("storage", syncPin);
    };
  }, []);
  const primary = [
    { to: "/", label: t("nav.home"), icon: Home },
    { to: "/services", label: t("nav.services"), icon: Box },
    { to: "/agent", label: agentText("title"), icon: MessageCircle },
    { to: "/apps", label: tr("Apps"), icon: Box },
    { to: "/devices", label: t("nav.devices"), icon: Network },
    { to: "/tasks", label: t("nav.tasks"), icon: CheckSquare },
    { to: "/drives", label: t("drive.title"), icon: HardDrive },
  ];
  const more = [
    { to: "/explore", label: t("nav.explore"), icon: Compass },
    { to: "/chat", label: t("nav.messages"), icon: MessageCircle },
    { to: "/friends", label: tr("Friends"), icon: UserRound },
    { to: "/exchange", label: tr("Exchange"), icon: Box },
  ];
  const explorePaths = ["/library", "/explore", "/digest", "/search-ask", "/publish", "/items/", "/reading", "/offline", "/search", "/shared-reading", "/friend-updates"];
  const inExplore = explorePaths.some((path) => location.pathname.startsWith(path));
  const inAgent = location.pathname === "/agent";
  const workspaceQuery = new URLSearchParams(location.search);
  const inFullWorkspace = inAgent || location.pathname.startsWith("/apps/extensions/") ||
    (location.pathname.startsWith("/apps/plugins/") && workspaceQuery.get("tab") !== "peers" && workspaceQuery.get("mode") !== "declarative");
  const inServices = location.pathname === "/services" || location.pathname.startsWith("/services/");
  // Keep every service entry available before configuration and across AI pages.
  const serviceLinks = [
    ["/services", t("nav.myServices")],
    ["/services/catalog", t("nav.browseServices")],
    ["/services/private-ai/chat", t("personal.aiWorkspace")],
    ["/services/sources", agentText("models")],
    ["/services/api", t("nav.apiAccess")],
    ["/services/manage", t("nav.serviceSetup")],
  ];
  const sublinks = inExplore
    ? [["/explore", t("nav.library")], ["/library", tr("My Content")], ["/digest", t("nav.forYou")], ["/reading", tr("My reading")], ["/offline", tr("Offline reading")], ["/search", tr("Search")], ["/shared-reading", tr("Shared lists")], ["/friend-updates", tr("Friend updates")], ["/publish", t("nav.publish")]]
    : inServices
      ? serviceLinks
      : (location.pathname === "/peers" || location.pathname.startsWith("/devices/"))
        ? [["/devices", t("nav.myDevices")], ["/devices/sync", tr("Device sync")], ["/peers", t("nav.connectionDetails")]]
        : location.pathname.startsWith("/settings")
          ? [["/settings", t("nav.preferences")], ["/settings/desktop", t("nav.desktop")], ...(isTauriDesktop() ? [["/settings/updates", tr("Software updates")]] : []), ["/settings/space", t("nav.personalSpace")], ["/settings/plugins", t("nav.plugins")], ["/settings/extensions", t("nav.agentExtensions")], ["/settings/advanced", t("nav.advancedSettings")]]
          : [];
  const title =
    (location.pathname.startsWith("/apps/plugins/")
      ? sidebarPins.find(pin => pin.kind === "local" && pin.open_path === location.pathname)?.title
        || pinnedPlugins.find(plugin => location.pathname === pluginWorkspacePath(plugin.id))?.title || tr("Apps")
      : inServices ? t("nav.services") : inExplore ? t("nav.explore") : location.pathname === "/peers" ? t("nav.devices") : [...primary, ...more, { to: "/settings", label: t("nav.settings") }].find(
      (item) => item.to !== "/" && location.pathname.startsWith(item.to),
    )?.label) || t("nav.home");
  const href = (path: string) => personalHref(path, demo);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => {
    setMenuOpen(false);
    setSearchOpen(false);
  }, [location.pathname]);
  useEffect(() => {
    if (searchOpen) input.current?.focus();
  }, [searchOpen]);
  const link = (item: (typeof primary)[number], key = item.to) => (
    <NavLink
      end={item.to === "/" || item.to === "/apps"}
      key={key}
      to={href(item.to)}
      className={({ isActive }) => ((!item.to.includes("?open=") && isActive) || (item.to.startsWith("/apps/plugins/") && location.pathname === item.to.replace("/apps/plugins/", "/apps/extensions/")) || (item.to === "/explore" && inExplore) || (item.to === "/devices" && location.pathname === "/peers") ? "active" : "")}
    >
      <item.icon size={21} strokeWidth={1.6} />
      <span>{item.label}</span>
    </NavLink>
  );
  return (
    <AgentNavigationContext.Provider value={{ collapsed: agentNavCollapsed, toggle: () => {
      setAgentNavCollapsed(current => { const next = !current; try { sessionStorage.setItem("ryn.agent.navigation", next ? "closed" : "open"); } catch { /* Preferences are optional. */ } return next; });
    } }}>
    <div className={`pf-shell${menuOpen ? " menu-open" : ""}${inFullWorkspace ? " pf-agent-shell" : ""}${(inFullWorkspace && agentNavCollapsed) || isStandalone ? " pf-agent-nav-collapsed" : ""}`}>
      <aside className="pf-sidebar" aria-label={t("shell.mainNavigation")} hidden={(inFullWorkspace && agentNavCollapsed) || isStandalone}>
        <div className="pf-brand-row">
        <NavLink className="pf-brand" to={href("/")} aria-label={`Ryn ${t("nav.home")}`}>
          <svg viewBox="0 0 30 34" aria-hidden="true">
            <path
              d="M2 3h15c15 0 16 18 3 21l9 10H18L8 22v12H2V15h14c5 0 5-5 0-5H2V3Z"
              fill="currentColor"
            />
            <path d="M2 15h14c3 0 5-1 6-3 2 4-1 8-6 8H2v-5Z" fill="#98a6bf" />
          </svg>
          <span>Ryn</span>
        </NavLink>
        {isTauriDesktop() && <DesktopUpdateButton />}
        </div>
        <button className="pf-workspace" onClick={() => navigate(href("/settings/space"))} aria-label={t("shell.manageSpace")}>
          <UserRound size={20} />
          <span>{space?.space?.name || t("shell.personal")}</span>
          <ChevronDown size={15} />
        </button>
        <div className="pf-sidebar-content">
          <nav aria-label={t("shell.primary")}>
            {primary.map(item => <Fragment key={item.to}>
              {link(item)}
              {item.to === "/apps" && sidebarPins.length > 0 && <>
                <div className="pf-more-label">{t("nav.pinnedApps")}</div>
                {sidebarPins.map(pin => pin.kind === "local"
                  ? link({ to: pin.open_path, label: pin.title, icon: Building2 }, pin.key)
                  : <Link key={pin.key} to={href("/apps?tab=shared")} onClick={event => {
                      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                      event.preventDefault();
                      void openSharedPin(pin);
                    }}>
                    <Globe size={21} strokeWidth={1.6} />
                    <span>{pin.title} · {pin.provider.member_name}</span>
                  </Link>)}
              </>}
            </Fragment>)}
          </nav>
          <div className="pf-more-label">{t("nav.more")}</div>
          <nav aria-label={t("nav.more")}>{more.map(item => link(item))}</nav>
        </div>
        <div className="pf-sidebar-bottom">
          {demo && (
            <span className="pf-preview-label">
              {t("shell.preview")}
            </span>
          )}
          <nav>
            {link({ to: "/settings", label: t("nav.settings"), icon: Settings })}
          </nav>
        </div>
      </aside>
      {menuOpen && (
        <button
          className="pf-menu-backdrop"
          aria-label={t("shell.closeNavigation")}
          onClick={() => setMenuOpen(false)}
        />
      )}
      {isTauriDesktop() && inFullWorkspace && agentNavCollapsed && !isStandalone && <DesktopUpdateButton floating />}
      <div className="pf-main-column">
        {!inFullWorkspace && !isStandalone && <header className="pf-topbar">
          <button
            className="pf-icon-button pf-menu-toggle"
            aria-label={t("shell.toggleNavigation")}
            onClick={() => setMenuOpen(!menuOpen)}
          >
            {menuOpen ? <X size={21} /> : <Menu size={21} />}
          </button>
          <div className="pf-breadcrumb">
            <Home size={17} />
            <span>/</span>
            <span>{title}</span>
            {inServices && location.pathname !== "/services" && (
              <>
                <span>/</span>
                <span>{(location.pathname === "/ask" ? t("personal.aiWorkspace") : serviceLinks.find(([path]) => path === location.pathname)?.[1])
                  || (location.pathname === "/services/model-mapping" ? tr("模型映射")
                    : location.pathname === "/services/agent-sharing" ? tr("管理共享")
                    : location.pathname === "/services/video-rendering" ? tr("Video rendering")
                    : location.pathname === "/services/secure-web-access" ? tr("Secure web access")
                    : t("nav.myServices"))}</span>
              </>
            )}
          </div>
          <div className="pf-top-tools">
            <DropActivity />
            <button
              className="pf-search-trigger"
              aria-label={t("shell.search")}
              onClick={() => setSearchOpen(true)}
            >
              <Search size={18} />
              <span>{t("shell.search")}</span>
              <kbd>{/Mac/i.test(navigator.platform) ? "⌘ K" : "Ctrl + K"}</kbd>
            </button>
            <button
              className="pf-icon-button"
              aria-label={t("shell.toggleAppearance")}
              onClick={() =>
                setAppearance(
                  document.documentElement.dataset.theme === "dark"
                    ? "light"
                    : "dark",
                )
              }
            >
              {appearance === "dark" ||
              (appearance === "system" &&
                document.documentElement.dataset.theme === "dark") ? (
                <Moon size={21} />
              ) : (
                <Sun size={21} />
              )}
            </button>
          </div>
        </header>}
        <main className="pf-main" id="main-content">
          {sublinks.length > 0 && <nav className={`pf-subnav${inServices ? " pf-service-nav" : ""}`} aria-label={title}>
            {sublinks.map(([path, label]) => <NavLink key={path} end={path !== "/settings/extensions"} to={href(path)} className={path === "/services/manage" ? "pf-service-settings" : path === "/services/private-ai/chat" && location.pathname === "/ask" ? "active" : undefined}>
              {inServices && path === "/services/manage" && <Settings size={17} aria-hidden="true" />}{label}
            </NavLink>)}
          </nav>}
          {children}
        </main>
      </div>
      {pinOpenError && <Modal title={tr("Apps")} onClose={() => setPinOpenError("")}>{pinOpenError}</Modal>}
      {searchOpen && (
        <Modal title={t("shell.searchSpace")} onClose={() => setSearchOpen(false)}>
          <label className="pf-search">
            <Search size={18} />
            <input
              ref={input}
              aria-label={t("shell.searchInput")}
              placeholder={t("shell.searchPlaceholder")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="pf-search-results">
            {devices
              .filter((device) =>
                device.name.toLowerCase().includes(query.toLowerCase()),
              )
              .map((device) => (
                <button
                  key={device.id}
                  onClick={() => {
                    navigate(
                      href(`/devices?device=${encodeURIComponent(device.id)}`),
                    );
                    setSearchOpen(false);
                  }}
                >
                  <DeviceArt kind={device.kind} small />
                  <div>
                    <strong>{device.name}</strong>
                    <small>{t("shell.device")}</small>
                  </div>
                </button>
              ))}
            {services
              .filter((service) =>
                service.title.toLowerCase().includes(query.toLowerCase()),
              )
              .map((service) => (
                <button
                  key={service.id}
                  onClick={() => {
                    navigate(
                      href(
                        `/services?service=${encodeURIComponent(service.id)}`,
                      ),
                    );
                    setSearchOpen(false);
                  }}
                >
                  <ServiceArt kind={service.kind} small />
                  <div>
                    <strong>{service.title}</strong>
                    <small>
                      {
                        devices.find((device) => device.id === service.deviceId)
                          ?.name
                      }
                    </small>
                  </div>
                </button>
              ))}
            {!devices.some((device) =>
              device.name.toLowerCase().includes(query.toLowerCase()),
            ) &&
              !services.some((service) =>
                service.title.toLowerCase().includes(query.toLowerCase()),
              ) && (
                <Empty title={t("shell.noMatches")}>
                  {t("shell.noResults")}
                </Empty>
              )}
          </div>
        </Modal>
      )}
    </div>
    </AgentNavigationContext.Provider>
  );
}
