import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  Box,
  CheckSquare,
  ChevronDown,
  Compass,
  Home,
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
export function PersonalShell({ children }: { children: ReactNode }) {
  const { demo, appearance, setAppearance, devices, services, space } = usePersonal();
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const input = useRef<HTMLInputElement>(null);
  const primary = [
    { to: "/", label: "Home", icon: Home },
    { to: "/services", label: "Services", icon: Box },
    { to: "/devices", label: "Devices", icon: Network },
    { to: "/tasks", label: "Tasks", icon: CheckSquare },
  ];
  const more = [
    { to: "/explore", label: "Explore", icon: Compass },
    { to: "/chat", label: "Messages", icon: MessageCircle },
  ];
  const explorePaths = ["/explore", "/digest", "/search-ask", "/publish", "/items/"];
  const inExplore = explorePaths.some((path) => location.pathname.startsWith(path));
  const sublinks = inExplore
    ? [["/explore", "Library"], ["/digest", "For you"], ["/search-ask", "Search & ask"], ["/publish", "Publish"]]
    : location.pathname.startsWith("/services/") && !location.pathname.endsWith("/chat")
      ? [["/services", "My services"], ["/services/catalog", "Browse services"], ["/services/manage", "Service setup"], ["/services/api", "API access"]]
      : location.pathname === "/peers"
        ? [["/devices", "My devices"], ["/peers", "Connection details"]]
        : location.pathname.startsWith("/settings/")
          ? [["/settings", "Preferences"], ["/settings/space", "Personal space"], ["/settings/advanced", "Advanced settings"]]
          : [];
  const title =
    (inExplore ? "Explore" : location.pathname === "/peers" ? "Devices" : [...primary, ...more, { to: "/settings", label: "Settings" }].find(
      (item) => item.to !== "/" && location.pathname.startsWith(item.to),
    )?.label) || "Home";
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
      <aside className="pf-sidebar" aria-label="Main navigation">
        <NavLink className="pf-brand" to={href("/")} aria-label="Ryn home">
          <svg viewBox="0 0 30 34" aria-hidden="true">
            <path
              d="M2 3h15c15 0 16 18 3 21l9 10H18L8 22v12H2V15h14c5 0 5-5 0-5H2V3Z"
              fill="currentColor"
            />
            <path d="M2 15h14c3 0 5-1 6-3 2 4-1 8-6 8H2v-5Z" fill="#98a6bf" />
          </svg>
          <span>Ryn</span>
        </NavLink>
        <button className="pf-workspace" onClick={() => navigate(href("/settings/space"))} aria-label="Manage personal space">
          <UserRound size={20} />
          <span>{space?.space?.name || "Personal"}</span>
          <ChevronDown size={15} />
        </button>
        <nav aria-label="Primary">{primary.map(link)}</nav>
        <div className="pf-more-label">More</div>
        <nav aria-label="More">{more.map(link)}</nav>
        <div className="pf-sidebar-bottom">
          {demo && (
            <span className="pf-preview-label">
              Design preview · Sample data
            </span>
          )}
          <nav>
            {link({ to: "/settings", label: "Settings", icon: Settings })}
          </nav>
        </div>
      </aside>
      {menuOpen && (
        <button
          className="pf-menu-backdrop"
          aria-label="Close navigation"
          onClick={() => setMenuOpen(false)}
        />
      )}
      <div className="pf-main-column">
        <header className="pf-topbar">
          <button
            className="pf-icon-button pf-menu-toggle"
            aria-label="Toggle navigation"
            onClick={() => setMenuOpen(!menuOpen)}
          >
            {menuOpen ? <X size={21} /> : <Menu size={21} />}
          </button>
          <div className="pf-breadcrumb">
            <Home size={17} />
            <span>/</span>
            <span>{title}</span>
            {location.pathname.includes("/chat") && title === "Services" && (
              <>
                <span>/</span>
                <span>AI chat</span>
              </>
            )}
          </div>
          <div className="pf-top-tools">
            <button
              className="pf-search-trigger"
              aria-label="Search devices or services"
              onClick={() => setSearchOpen(true)}
            >
              <Search size={18} />
              <span>Search devices or services</span>
              <kbd>{/Mac/i.test(navigator.platform) ? "⌘ K" : "Ctrl + K"}</kbd>
            </button>
            <button
              className="pf-icon-button"
              aria-label="Toggle light and dark appearance"
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
          {sublinks.length > 0 && <nav className="pf-subnav" aria-label={`${title} pages`}>
            {sublinks.map(([path, label]) => <NavLink key={path} end to={href(path)}>{label}</NavLink>)}
          </nav>}
          {children}
        </main>
      </div>
      {searchOpen && (
        <Modal title="Search your space" onClose={() => setSearchOpen(false)}>
          <label className="pf-search">
            <Search size={18} />
            <input
              ref={input}
              aria-label="Search devices and services"
              placeholder="Find a device or service…"
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
                    <small>Device</small>
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
                <Empty title="No matches">
                  Try another device or service name.
                </Empty>
              )}
          </div>
        </Modal>
      )}
    </div>
  );
}
