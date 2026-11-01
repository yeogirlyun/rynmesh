import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AudioLines, Cloud, FileText, Film, Laptop, Monitor, Network, Pencil, Share2, Sparkles } from "lucide-react";
import { Arrow, Empty, Modal, ServiceArt } from "./components";
import { routeLabel } from "./deviceNetwork";
import { personalLabelKeys } from "./labels";
import { personalHref, usePersonal, type Device, type Service } from "./model";
import { serviceTone, serviceUrl, type DeviceGroup, type HomeView, type Tone } from "./homeModel";
import "./home-refresh.css";

// Application-owned labels (network names, route names, status words) are translated;
// device names, notes and provider content never are.
function useLabel() {
  const { t } = useTranslation();
  return (value: string) => (personalLabelKeys[value] ? t(`personal.${personalLabelKeys[value]}`) : value);
}
const deviceIcon = (device: Device) =>
  device.network?.scope === "public" ? Cloud : device.kind === "laptop" ? Laptop : Monitor;
const serviceIcon = { ai: Sparkles, document: FileText, audio: AudioLines, network: Network, video: Film } as const;
const dotClass = (online: boolean | null) => (online === true ? "ok" : online === false ? "off" : "unk");

export function HomeStats({ owned, services }: { owned: Device[]; services: Service[] }) {
  const { t } = useTranslation();
  const online = owned.filter((device) => device.online === true).length;
  // Unknown status is neither online nor offline.
  const offline = owned.filter((device) => device.online === false).length;
  return (
    <div className="rh-stats">
      {owned.length > 0 && (
        <span className="rh-chip ok">
          <i className="rh-dot ok" />
          {t("personal.homeOnlineCount", { count: online })}
        </span>
      )}
      {offline > 0 && (
        <span className="rh-chip">
          <i className="rh-dot off" />
          {t("personal.homeOfflineCount", { count: offline })}
        </span>
      )}
      {services.length > 0 && <span className="rh-chip info">{t("personal.homeServiceCount", { count: services.length })}</span>}
    </div>
  );
}

export function ViewSwitch({ view, onChange }: { view: HomeView; onChange: (view: HomeView) => void }) {
  const { t } = useTranslation();
  return (
    <div className="rh-seg" role="group" aria-label={t("personal.homeViewLabel")}>
      {(["list", "map"] as const).map((value) => (
        <button
          key={value}
          type="button"
          aria-pressed={view === value}
          className={view === value ? "on" : ""}
          onClick={() => onChange(value)}
        >
          {t(value === "list" ? "personal.homeViewList" : "personal.homeViewMap")}
        </button>
      ))}
    </div>
  );
}

export function DeviceRail({
  groups,
  selectedId,
  loading,
  onSelect,
  onShowMap,
}: {
  groups: DeviceGroup[];
  selectedId?: string;
  loading?: boolean;
  onSelect: (id: string) => void;
  onShowMap: () => void;
}) {
  const { t } = useTranslation();
  const label = useLabel();
  const summary = (group: DeviceGroup) =>
    group.current
      ? t("personal.youAreHereCountDevices", { count: group.devices.length })
      : `${
          group.scope === "unknown"
            ? t("personal.lanMembershipNotAvailable")
            : group.scope === "public"
              ? t("personal.advertisedPublicAddresses")
              : t("personal.remoteNetwork")
        } · ${t("personal.countDevices", { count: group.devices.length })}`;
  return (
    <section className="rh-rail rh-panel" aria-label={t("personal.deviceNetwork")}>
      {groups.length === 0 ? (
        <p role="status" className="rh-note">
          {loading ? t("personal.findingDevices") : t("personal.noDevicesYetAddADeviceToGetStarted")}
        </p>
      ) : (
        groups.map((group) => (
          <div key={group.id} className={`rh-group rh-t-${group.tone}`} role="group" aria-label={label(group.label)}>
            <header>
              <strong>{label(group.label)}</strong>
              <small>{summary(group)}</small>
            </header>
            {group.devices.map((device) => {
              const Icon = deviceIcon(device);
              const on = selectedId === device.id;
              const status =
                device.online === true ? t("personal.online") : device.online === false ? t("personal.offline") : t("personal.statusUnknown");
              const sub = device.self
                ? t("personal.thisDevice")
                : device.online === true
                  ? label(routeLabel(device.network?.route))
                  : status;
              return (
                <button
                  key={device.id}
                  type="button"
                  className={`rh-dev${on ? " on" : ""}`}
                  aria-pressed={on}
                  aria-label={`${device.name}, ${device.self ? t("personal.thisDevicePrefix") : ""}${status}`}
                  onClick={() => onSelect(device.id)}
                >
                  <span className={`rh-mini${device.self ? " solid" : ""}`}>
                    <Icon size={20} strokeWidth={1.7} aria-hidden="true" />
                  </span>
                  <span className="rh-dev-name">
                    <b>{device.name}</b>
                    <small>{sub}</small>
                  </span>
                  <i className={`rh-dot ${dotClass(device.online)}`} />
                </button>
              );
            })}
          </div>
        ))
      )}
      {groups.length > 0 && (
        <button type="button" className="rh-link rh-rail-map" onClick={onShowMap}>
          <Network size={14} aria-hidden="true" />
          {t("personal.connectionView")}
        </button>
      )}
    </section>
  );
}

export function DeviceHero({
  device,
  tone,
  serviceTotal,
  serviceReady,
  onEdit,
  onShare,
}: {
  device: Device;
  tone: Tone;
  serviceTotal: number;
  serviceReady: number;
  onEdit: () => void;
  onShare: () => void;
}) {
  const { t } = useTranslation();
  const label = useLabel();
  const Icon = deviceIcon(device);
  const status =
    device.online === true ? t("personal.online") : device.online === false ? t("personal.offline") : t("personal.statusUnknown");
  // Same wording the connection map uses for the selected device.
  const detail = device.self
    ? t("personal.thisDevice")
    : device.online === false
      ? t("personal.offlineLastKnownNetwork")
      : device.online === null
        ? t("personal.statusUnknownRouteUnconfirmed")
        : label(routeLabel(device.network?.route));
  return (
    <section className={`rh-hero rh-panel rh-t-${tone}`} aria-label={device.name} aria-live="polite">
      <span className="rh-tile xl">
        <Icon size={28} strokeWidth={1.6} aria-hidden="true" />
      </span>
      <div className="rh-hero-main">
        <div className="rh-hero-title">
          <h3>{device.name}</h3>
          <span className={`rh-chip${device.online === true ? " ok" : ""}`}>
            <i className={`rh-dot ${dotClass(device.online)}`} />
            {status}
          </span>
          {device.online === true && <span className="rh-chip info">{detail}</span>}
        </div>
        {device.online !== true && <small>{detail}</small>}
        {device.network?.address && <small>{t("personal.advertisedDiscoveryAddress", { address: device.network.address })}</small>}
        {device.note && <small>{device.note}</small>}
        <small>{t("personal.homeServiceSummary", { count: serviceTotal, ready: serviceReady })}</small>
      </div>
      <button type="button" className="rh-icon-btn" aria-label={t("personal.editName", { name: device.name })} onClick={onEdit}>
        <Pencil size={16} aria-hidden="true" />
      </button>
      <button type="button" className="rh-btn" onClick={onShare}>
        <Share2 size={16} aria-hidden="true" />
        {t("personal.shareService")}
      </button>
    </section>
  );
}

function HomeServiceCard({ service }: { service: Service }) {
  const { t } = useTranslation();
  const label = useLabel();
  const { resolveName, devices, demo } = usePersonal();
  const navigate = useNavigate();
  const [preview, setPreview] = useState(false);
  const href = (path: string) => personalHref(path, demo);
  // An advertisement from a device this node has no relationship with is not "ready":
  // the provider would refuse the request, so the action is to connect the device first.
  const needsConnection = service.availability === "needs_connection";
  const status = label(needsConnection ? "Not connected" : service.online ? "Ready" : "Offline");
  const Icon = serviceIcon[service.kind];
  const branded = service.kind === "ai" && !!service.brand;
  const open = () => (service.preview ? setPreview(true) : navigate(href(serviceUrl(service))));
  return (
    <article className={`rh-svc rh-t-${service.online ? serviceTone(service) : "slate"}`}>
      <div className="rh-svc-top">
        <span className={`rh-tile${branded || !service.online ? " soft" : ""}`}>
          {branded ? <span className={`pf-provider-logo ${service.brand}`} aria-hidden="true" /> : <Icon size={22} strokeWidth={1.7} aria-hidden="true" />}
        </span>
        <div className="rh-svc-text">
          <h3>
            {service.title}
            {service.accessLabel && <span className="rh-badge">{service.accessLabel}</span>}
          </h3>
          <p>
            {resolveName(service.deviceId)} ·{" "}
            {devices.find((device) => device.id === service.deviceId)?.own
              ? t("personal.myDevice")
              : demo
                ? t("personal.sharedWithMe")
                : t("personal.discoveredDevice")}
          </p>
        </div>
        <span className={`rh-chip${service.online ? " ok" : ""}`}>
          <i className={`rh-dot ${service.online ? "ok" : "off"}`} />
          {status}
        </span>
      </div>
      {service.description && <small className="rh-svc-desc">{service.description}</small>}
      <div className="rh-svc-foot">
        {needsConnection ? (
          <Link className="rh-btn" to={href(`/devices?device=${encodeURIComponent(service.deviceId)}`)}>
            {t("personal.connectDevice")}
          </Link>
        ) : (
          <button type="button" className={`rh-btn${service.kind === "ai" ? " primary" : ""}`} disabled={!service.online} onClick={open}>
            {service.kind === "ai" ? t("personal.openChat") : t("personal.open")}
          </button>
        )}
      </div>
      {preview && (
        <Modal title={service.title} onClose={() => setPreview(false)}>
          <ServiceArt kind={service.kind} />
          <p>{t("personal.thisServiceIsShownAsSampleDataInTheDesignPreviewItIsNotInstalledOrRunning")}</p>
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={() => setPreview(false)}>
              {t("personal.close")}
            </button>
          </footer>
        </Modal>
      )}
    </article>
  );
}

export function HomeServices({
  device,
  offered,
  loading,
  columns,
}: {
  device: Device;
  offered: Service[];
  loading?: boolean;
  columns: 2 | 3;
}) {
  const { t } = useTranslation();
  const { demo } = usePersonal();
  const href = (path: string) => personalHref(path, demo);
  return (
    <div className="rh-services">
      <div className="rh-sec">
        <h2>{t("personal.servicesOnName", { name: device.name })}</h2>
        <p>{t("personal.fromYourName", { name: device.name })}</p>
      </div>
      {offered.length > 0 ? (
        <div className={`rh-cards cols${columns}`}>
          {offered.map((service) => (
            <HomeServiceCard service={service} key={service.id} />
          ))}
        </div>
      ) : (
        <div className="rh-panel">
          <Empty title={loading ? t("personal.findingServices") : t("personal.noServicesOnThisDevice")}>
            {loading ? (
              t("personal.checkingYourConnectedDevices")
            ) : (
              <Link to={href("/services/manage")}>{t("personal.setUpAServiceOnThisDevice")}</Link>
            )}
          </Empty>
        </div>
      )}
      <Link className="rh-browse" to={href("/services")}>
        <span className="rh-tile soft small">
          <Share2 size={18} aria-hidden="true" />
        </span>
        <div>
          <h3>{t("personal.fromOtherDevices")}</h3>
          <small>{t("personal.browseServicesFromYourOtherDevices")}</small>
        </div>
        <span className="rh-link">
          {t("personal.browseServices")} <Arrow />
        </span>
      </Link>
    </div>
  );
}

// Seen on the network (peer record or service advertisement) but not owned: shown on
// Home so a device whose services appear in Services is never invisible here.
export function NearbyPanel({ nearby, services }: { nearby: Device[]; services: Service[] }) {
  const { t } = useTranslation();
  const { demo } = usePersonal();
  const href = (path: string) => personalHref(path, demo);
  if (!nearby.length) return null;
  return (
    <section className="rh-nearby rh-panel" aria-label={t("personal.discoveredNearbyCount", { count: nearby.length })}>
      <div className="rh-nearby-head">
        <div>
          <h2>{t("personal.discoveredNearbyCount", { count: nearby.length })}</h2>
          <p>{t("personal.discoveredNearbyHint")}</p>
        </div>
        <Link className="rh-link" to={href("/devices")}>
          {t("personal.reviewDiscoveredDevices")}
        </Link>
      </div>
      <ul className="rh-nearby-list">
        {nearby.slice(0, 5).map((device) => {
          const advertised = services.filter((service) => service.deviceId === device.id).length;
          const Icon = deviceIcon(device);
          return (
            <li key={device.id}>
              <span className="rh-mini">
                <Icon size={20} strokeWidth={1.7} aria-hidden="true" />
              </span>
              <span className="rh-nearby-name">
                <strong>{device.name}</strong>
                <small>
                  {t("personal.notConnected")}
                  {advertised > 0 && ` · ${t("personal.advertisedServiceCount", { count: advertised })}`}
                </small>
              </span>
              <span className={`rh-chip${device.online === true ? " ok" : ""}`}>
                <i className={`rh-dot ${dotClass(device.online)}`} />
                {device.online === true ? t("personal.online") : device.online === false ? t("personal.offline") : t("personal.unknown")}
              </span>
              <Link className="rh-btn sm" to={href(`/devices?device=${encodeURIComponent(device.id)}`)}>
                {t("personal.connectDevice")}
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
