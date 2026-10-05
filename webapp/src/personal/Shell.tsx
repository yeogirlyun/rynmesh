import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { tr } from "../uiI18n";
import {
  Box,
  CheckSquare,
  ChevronDown,
  Compass,
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
import { useNasStatus } from "../domain/nas";
export function PersonalShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { demo, appearance, setAppearance, devices, services, space } = usePersonal();
  const { status: nas } = useNasStatus(demo);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const input = useRef<HTMLInputElement>(null);
  const primary = [
    { to: "/", label: t("nav.home"), icon: Home },
    { to: "/services", label: t("nav.services"), icon: Box },
    { to: "/devices", label: t("nav.devices"), icon: Network },
    { to: "/tasks", label: t("nav.tasks"), icon: CheckSquare },
    ...(nas?.enabled ? [{ to: "/nas", label: t("nav.nas"), icon: HardDrive }] : []),
  ];
  const more = [
    { to: "/explore", label: t("nav.explore"), icon: Compass },
    { to: "/chat", label: t("nav.messages"), icon: MessageCircle },
    { to: "/ask", label: tr("Ask Ryn"), icon: MessageCircle },
    { to: "/friends", label: tr("Friends"), icon: UserRound },
    { to: "/exchange", label: tr("Exchange"), icon: Box },
  ];
  const explorePaths = ["/explore", "/digest", "/search-ask", "/publish", "/items/", "/reading", "/offline", "/search", "/shared-reading", "/friend-updates"];
  const inExplore = explorePaths.some((path) => location.pathname.startsWith(path));
  const inServices = location.pathname === "/services" || location.pathname.startsWith("/services/");
  // Keep every service entry available before configuration and across AI pages.
  const serviceLinks = [
    ["/services", t("nav.myServices")],
    ["/services/catalog", t("nav.browseServices")],
    ["/services/private-ai/chat", t("personal.aiWorkspace")],
    ["/services/sources", t("personal.aiSources")],
    ["/services/api", t("nav.apiAccess")],
    ["/services/manage", t("nav.serviceSetup")],
  ];
  const sublinks = inExplore
    ? [["/explore", t("nav.library")], ["/digest", t("nav.forYou")], ["/reading", tr("My reading")], ["/offline", tr("Offline reading")], ["/search", tr("Search")], ["/shared-reading", tr("Shared lists")], ["/friend-updates", tr("Friend updates")], ["/publish", t("nav.publish")]]
    : inServices
      ? serviceLinks
      : (location.pathname === "/peers" || location.pathname.startsWith("/devices"))
        ? [["/devices", t("nav.myDevices")], ["/devices/sync", tr("Device sync")], ["/peers", t("nav.connectionDetails")]]
        : location.pathname.startsWith("/settings")
          ? [["/settings", t("nav.preferences")], ["/settings/desktop", t("nav.desktop")], ["/settings/space", t("nav.personalSpace")], ["/settings/plugins", t("nav.plugins")], ["/settings/advanced", t("nav.advancedSettings")]]
          : [];
  const title =
    (inExplore ? t("nav.explore") : location.pathname === "/peers" ? t("nav.devices") : [...primary, ...more, { to: "/settings", label: t("nav.settings") }].find(
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
  const link = (item: (typeof primary)[number]) => (
    <NavLink
      end={item.to === "/"}
      key={item.to}
      to={href(item.to)}
      className={({ isActive }) => (isActive || (item.to === "/explore" && inExplore) || (item.to === "/devices" && location.pathname === "/peers") ? "active" : "")}
    >
      <item.icon size={21} strokeWidth={1.6} />
      <span>{item.label}</span>
    </NavLink>
  );
  return (
    <div className={`pf-shell${menuOpen ? " menu-open" : ""}`}>
      <aside className="pf-sidebar" aria-label={t("shell.mainNavigation")}>
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
        <button className="pf-workspace" onClick={() => navigate(href("/settings/space"))} aria-label={t("shell.manageSpace")}>
          <UserRound size={20} />
          <span>{space?.space?.name || t("shell.personal")}</span>
          <ChevronDown size={15} />
        </button>
        <nav aria-label={t("shell.primary")}>{primary.map(link)}</nav>
        <div className="pf-more-label">{t("nav.more")}</div>
        <nav aria-label={t("nav.more")}>{more.map(link)}</nav>
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
      <div className="pf-main-column">
        <header className="pf-topbar">
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
                <span>{serviceLinks.find(([path]) => path === location.pathname)?.[1]
                  || (location.pathname === "/services/model-mapping" ? tr("模型映射")
                    : location.pathname === "/services/agent-sharing" ? tr("管理共享")
                    : location.pathname === "/services/video-rendering" ? tr("Video rendering")
                    : location.pathname === "/services/secure-web-access" ? tr("Secure web access")
                    : t("nav.myServices"))}</span>
              </>
            )}
          </div>
          <div className="pf-top-tools">
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
        </header>
        <main className="pf-main" id="main-content">
          {sublinks.length > 0 && <nav className={`pf-subnav${inServices ? " pf-service-nav" : ""}`} aria-label={title}>
            {sublinks.map(([path, label]) => <NavLink key={path} end to={href(path)} className={path === "/services/manage" ? "pf-service-settings" : undefined}>
              {inServices && path === "/services/manage" && <Settings size={17} aria-hidden="true" />}{label}
            </NavLink>)}
          </nav>}
          {children}
        </main>
      </div>
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
  );
}
