import { useState, type MouseEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Cloud, ExternalLink, Info, Link2, Share2 } from "lucide-react";
import { Modal } from "./components";
import { routeLabel } from "./deviceNetwork";
import { personalLabelKeys } from "./labels";
import { personalHref, usePersonal, type Device, type Service } from "./model";
import { serviceUrl } from "./homeModel";
import "./services-refresh.css";

// Application-owned labels (status words, route names, filter names) are translated;
// device names, service titles and provider content never are.
function useLabel() {
  const { t } = useTranslation();
  return (value: string) => (personalLabelKeys[value] ? t(`personal.${personalLabelKeys[value]}`) : value);
}

type Hue = "violet" | "teal" | "sky" | "rose" | "indigo" | "amber" | "slate";
const kindHue: Record<Service["kind"], Hue> = { ai: "violet", document: "teal", audio: "sky", video: "rose", network: "indigo" };
const deviceHues: Hue[] = ["teal", "sky", "amber", "indigo", "rose", "violet"];

type State = "ready" | "off" | "need";
const stateOf = (service: Service): State =>
  service.availability === "needs_connection" ? "need" : service.online ? "ready" : "off";
const stateWord = { ready: "Ready", off: "Offline", need: "Not connected" } as const;

/** Two-tone glyphs: a white body with details in the hue's deep shade. */
export function ServiceGlyph({ kind }: { kind: Service["kind"] }) {
  const body = {
    ai: (
      <>
        <path d="M10 4.5c.7 4.6 2.9 6.8 7.5 7.5-4.6.7-6.8 2.9-7.5 7.5-.7-4.6-2.9-6.8-7.5-7.5 4.6-.7 6.8-2.9 7.5-7.5z" fill="#fff" />
        <path d="M18.5 2.8c.3 2 1 2.7 3 3-2 .3-2.7 1-3 3-.3-2-1-2.7-3-3 2-.3 2.7-1 3-3z" fill="#fff" fillOpacity=".72" />
        <circle cx="19" cy="18.5" r="1.5" fill="#fff" fillOpacity=".55" />
      </>
    ),
    document: (
      <>
        <path d="M7 3h6.6a2 2 0 0 1 1.4.6l3.4 3.4a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" fill="#fff" />
        <path className="deep" d="M14 3.3V7a1 1 0 0 0 1 1h3.7z" fillOpacity=".28" />
        <rect className="deep" x="8" y="12" width="8" height="1.7" rx=".85" fillOpacity=".6" />
        <rect className="deep" x="8" y="15.5" width="5.4" height="1.7" rx=".85" fillOpacity=".36" />
      </>
    ),
    audio: (
      <>
        <rect x="3.2" y="9" width="2.6" height="6" rx="1.3" fill="#fff" fillOpacity=".7" />
        <rect x="7.2" y="6" width="2.6" height="12" rx="1.3" fill="#fff" fillOpacity=".9" />
        <rect x="11.2" y="3.5" width="2.6" height="17" rx="1.3" fill="#fff" />
        <rect x="15.2" y="7" width="2.6" height="10" rx="1.3" fill="#fff" fillOpacity=".9" />
        <rect x="19.2" y="9.5" width="2.6" height="5" rx="1.3" fill="#fff" fillOpacity=".7" />
      </>
    ),
    video: (
      <>
        <rect x="3" y="5" width="18" height="14" rx="3.2" fill="#fff" />
        <path className="deep" d="M10 9.2v5.6a.6.6 0 0 0 .9.5l4.6-2.8a.6.6 0 0 0 0-1l-4.6-2.8a.6.6 0 0 0-.9.5z" fillOpacity=".7" />
      </>
    ),
    network: (
      <>
        <circle cx="12" cy="12" r="9" fill="#fff" />
        <path className="deep-line" d="M3.2 12h17.6M12 3c2.4 2.5 3.6 5.5 3.6 9S14.4 18.5 12 21M12 3C9.6 5.5 8.4 8.5 8.4 12S9.6 18.5 12 21" fill="none" strokeWidth="1.4" strokeLinecap="round" strokeOpacity=".55" />
      </>
    ),
  }[kind];
  return (
    <svg className="rs-glyph" viewBox="0 0 24 24" aria-hidden="true">
      {body}
    </svg>
  );
}

function ServiceTile({ service, size }: { service: Service; size?: "xl" }) {
  const branded = service.kind === "ai" && !!service.brand;
  return (
    <span className={`rs-tile${size ? ` ${size}` : ""}${branded ? " branded" : ""}`} aria-hidden="true">
      {branded ? <span className={`pf-provider-logo ${service.brand}`} /> : <ServiceGlyph kind={service.kind} />}
    </span>
  );
}

export function DeviceGlyph({ device }: { device?: Device }) {
  if (device?.network?.scope === "public") return <Cloud className="rs-ico" size={17} strokeWidth={1.6} aria-hidden="true" />;
  const laptop = device?.kind === "laptop";
  return (
    <svg className="rs-ico" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      {laptop ? (
        <>
          <path d="M6 5h12a1.5 1.5 0 0 1 1.5 1.5V15h-15V6.5A1.5 1.5 0 0 1 6 5z" fill="currentColor" fillOpacity=".16" />
          <path d="M2.8 18.5h18.4" strokeWidth="1.8" />
        </>
      ) : (
        <>
          <rect x="3" y="4" width="18" height="12" rx="2.2" fill="currentColor" fillOpacity=".16" />
          <path d="M8.5 20h7M12 16v4" />
        </>
      )}
    </svg>
  );
}

/** What this node knows about reaching the device that provides a service. */
export function useDeviceLink() {
  const { t } = useTranslation();
  const label = useLabel();
  return (service: Service, device?: Device): { text: string; tone: "ok" | "warn" | "plain"; live: boolean } => {
    // The status pill already says "Not connected"; here the useful fact is that no route is confirmed yet.
    if (service.availability === "needs_connection") return { text: label(routeLabel(device?.network?.route)), tone: "warn", live: false };
    if (device?.self) return { text: t("personal.thisDevice"), tone: "ok", live: true };
    if (device?.online === false) return { text: t("personal.offline"), tone: "plain", live: false };
    const route = device?.network?.route;
    if (device?.online == null && (!route || route === "unknown")) return { text: t("personal.statusUnknown"), tone: "plain", live: false };
    const live = device?.online === true && (route === "lan" || route === "local" || route === "public");
    return { text: label(routeLabel(route)), tone: route === "lan" || route === "local" ? "ok" : "plain", live };
  };
}

function useRelation() {
  const { t } = useTranslation();
  const { devices, demo } = usePersonal();
  return (service: Service) =>
    devices.find((device) => device.id === service.deviceId)?.own
      ? t("personal.myDevice")
      : demo
        ? t("personal.sharedWithMe")
        : t("personal.discoveredDevice");
}

const initials = (name: string) => {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const text = parts.length > 1 ? parts[0][0] + parts[1][0] : Array.from(parts[0] || "?").slice(0, 2).join("");
  return text.toUpperCase();
};

/** One line that says how many services there are, where they live and how many are usable. */
export function ServicesBand({ services }: { services: Service[] }) {
  const { t } = useTranslation();
  const { resolveName } = usePersonal();
  const ids = [...new Set(services.map((service) => service.deviceId))];
  const count = (state: State) => services.filter((service) => stateOf(service) === state).length;
  const ready = count("ready");
  const off = count("off");
  const need = count("need");
  const summary = t("personal.servicesBandSummary", { ready, offline: off, notConnected: need });
  return (
    <section className="rs-band" aria-label={t("personal.servicesBandLabel")}>
      <div className="rs-band-left">
        <div>
          <div className="rs-band-num">{services.length}</div>
          <div className="rs-band-cap">
            {t("personal.servicesAcrossDevices", { count: services.length, devices: t("personal.countDevices", { count: ids.length }) })}
          </div>
        </div>
        <div className="rs-avs" aria-hidden="true">
          {ids.slice(0, 5).map((id, index) => (
            <span key={id} className={`rs-h-${deviceHues[index % deviceHues.length]}`}>
              {initials(resolveName(id) || id)}
            </span>
          ))}
        </div>
      </div>
      <div className="rs-band-right">
        <div className="rs-meter" role="img" aria-label={summary}>
          {ready > 0 && <i className="ok" style={{ flex: ready }} />}
          {off > 0 && <i className="off" style={{ flex: off }} />}
          {need > 0 && <i className="need" style={{ flex: need }} />}
        </div>
        <div className="rs-legend" aria-hidden="true">
          <span><i className="rs-dot ok" /><b>{ready}</b> {t("personal.ready")}</span>
          <span><i className="rs-dot off" /><b>{off}</b> {t("personal.offline")}</span>
          <span><i className="rs-dot need" /><b>{need}</b> {t("personal.notConnected")}</span>
        </div>
      </div>
    </section>
  );
}

/** Segmented filter. Counts are decoration, so the button names stay the plain filter names. */
export function SegTabs<T extends string>({
  options,
  value,
  onChange,
  counts,
  label,
}: {
  options: readonly T[];
  value: T;
  onChange: (value: T) => void;
  counts?: Partial<Record<T, number>>;
  label: string;
}) {
  const word = useLabel();
  return (
    <div className="rs-seg" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option} type="button" aria-pressed={value === option} className={value === option ? "on" : ""} onClick={() => onChange(option)}>
          {word(option)}
          {counts && counts[option] !== undefined && <em aria-hidden="true">{counts[option]}</em>}
        </button>
      ))}
    </div>
  );
}

export function StatusPill({ state }: { state: State }) {
  const word = useLabel();
  return (
    <span className={`rs-pill ${state}`}>
      <i />
      {word(stateWord[state])}
    </span>
  );
}

function ServiceItem({ service, selected, onSelect }: { service: Service; selected: boolean; onSelect: () => void }) {
  const { t } = useTranslation();
  const { resolveName, devices, demo } = usePersonal();
  const navigate = useNavigate();
  const relation = useRelation();
  const deviceLink = useDeviceLink();
  const [preview, setPreview] = useState(false);
  const href = (path: string) => personalHref(path, demo);
  const device = devices.find((item) => item.id === service.deviceId);
  const state = stateOf(service);
  const link = deviceLink(service, device);
  const hue = state === "off" ? "slate" : kindHue[service.kind];
  const open = () => (service.preview ? setPreview(true) : navigate(href(serviceUrl(service))));
  // Clicking plain row content selects the service. Anything interactive inside the row (its own select button,
  // Open / Connect device, the sample-data dialog and its text, close button or backdrop) handles itself and
  // never bubbles up into a selection.
  const rowClick = (event: MouseEvent<HTMLElement>) => {
    const inner = (event.target as HTMLElement).closest("a, button, dialog");
    if (inner && event.currentTarget.contains(inner)) return;
    onSelect();
  };
  return (
    <article className={`rs-row rs-h-${hue}${state === "off" ? " off" : ""}${selected ? " selected" : ""}`} onClick={rowClick}>
      <button type="button" className="rs-select" aria-pressed={selected} onClick={onSelect}>
        <ServiceTile service={service} />
        <span className="rs-text">
          <h3 className="rs-title">
            {service.title}
            {service.accessLabel && <span className="rs-tag">{service.accessLabel}</span>}
          </h3>
          {service.description && <small>{service.description}</small>}
        </span>
      </button>
      <div className={`rs-dev rs-h-${deviceHues[Math.abs(hash(service.deviceId)) % deviceHues.length]}`}>
        <span className="rs-dg">
          <DeviceGlyph device={device} />
        </span>
        <span className="rs-dn">
          <b>{resolveName(service.deviceId)}</b>
          <small title={`${relation(service)} · ${link.text}`}>
            {relation(service)} · <i className={link.tone}>{link.text}</i>
          </small>
        </span>
      </div>
      <div className="rs-st">
        <StatusPill state={state} />
      </div>
      <div className="rs-ac">
        {state === "need" ? (
          <Link className="rs-btn sm" to={href(`/devices?device=${encodeURIComponent(service.deviceId)}`)}>
            {t("personal.connectDevice")}
          </Link>
        ) : (
          <button type="button" className={`rs-btn sm${service.kind === "ai" ? " primary" : ""}`} disabled={!service.online} onClick={open}>
            {service.kind === "ai" ? t("personal.openChat") : t("personal.open")}
          </button>
        )}
      </div>
      {preview && (
        <Modal title={service.title} onClose={() => setPreview(false)}>
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

function hash(value: string) {
  let h = 0;
  for (const ch of value) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return h;
}

export function ServiceTable({
  services,
  selectedId,
  onSelect,
  empty,
}: {
  services: Service[];
  selectedId?: string;
  onSelect: (id: string) => void;
  empty: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="rs-list">
      <div className="rs-lh" aria-hidden="true">
        <span>{t("personal.service")}</span>
        <span>{t("personal.servicesProvidedBy")}</span>
        <span>{t("personal.servicesStatus")}</span>
        <span />
      </div>
      {services.map((service) => (
        <ServiceItem key={service.id} service={service} selected={selectedId === service.id} onSelect={() => onSelect(service.id)} />
      ))}
      {!services.length && empty}
    </div>
  );
}

/** This device, the route to the provider, and the provider: the same strip the Devices page draws. */
function ConnectionStrip({ service, device }: { service: Service; device?: Device }) {
  const { t } = useTranslation();
  const { devices, resolveName } = usePersonal();
  const deviceLink = useDeviceLink();
  const relation = useRelation();
  const self = devices.find((item) => item.self);
  const link = deviceLink(service, device);
  const wire = link.live ? "" : service.availability === "needs_connection" ? " dash warn" : " dash";
  return (
    <div className="rs-conn">
      <div className="rs-end rs-h-indigo">
        <span className="rs-dg">
          <DeviceGlyph device={self} />
        </span>
        <div>
          <b>{self?.name || t("personal.thisDevice")}</b>
          <small>{t("personal.thisDevice")}</small>
        </div>
      </div>
      <div className={`rs-wire${wire}`} />
      <div className="rs-end rs-h-teal">
        <span className="rs-dg">
          <DeviceGlyph device={device} />
        </span>
        <div>
          <b>{resolveName(service.deviceId)}</b>
          <small>{relation(service)}</small>
        </div>
      </div>
      <span className={`rs-rt ${link.tone}`}>{link.text}</span>
    </div>
  );
}

export function ServiceDetail({
  service,
  tab,
  onTab,
  onManage,
}: {
  service: Service;
  tab: "Overview" | "API access";
  onTab: (tab: "Overview" | "API access") => void;
  onManage: () => void;
}) {
  const { t } = useTranslation();
  const { resolveName, devices, demo } = usePersonal();
  const href = (path: string) => personalHref(path, demo);
  const owner = devices.find((device) => device.id === service.deviceId);
  const state = stateOf(service);
  const hue = state === "off" ? "slate" : kindHue[service.kind];
  const deviceUrl = href(`/devices?device=${encodeURIComponent(service.deviceId)}`);
  const accessText = demo
    ? owner?.own
      ? t("personal.onlyMyDevices")
      : t("personal.sharedWithMe")
    : state === "need"
      ? t("personal.serviceNeedsConnection")
      : t("personal.controlledByTheServiceProvider");
  return (
    <aside className={`rs-detail rs-h-${hue}`}>
      <div className="rs-detail-top">
        <ServiceTile service={service} size="xl" />
        <div>
          <h2>{service.title}</h2>
          <p>{t("personal.onName", { name: resolveName(service.deviceId) })}</p>
          <StatusPill state={state} />
        </div>
      </div>
      <SegTabs<"Overview" | "API access"> options={["Overview", "API access"]} value={tab} onChange={onTab} label={t("personal.filter")} />
      {tab === "Overview" ? (
        <div className="rs-detail-body">
          <p className="rs-description">
            {service.description}
            {service.accessLabel && (
              <>
                <br />
                {t("personal.accessMethodMethod", { method: service.accessLabel })}
              </>
            )}
          </p>
          <ConnectionStrip service={service} device={owner} />
          <h3 className="rs-h3">{t("personal.access")}</h3>
          <p className="rs-access">{accessText}</p>
          <div className="rs-actions">
            {state === "need" && (
              <Link className="rs-btn primary" to={deviceUrl}>
                {t("personal.connectDevice")}
              </Link>
            )}
            <button type="button" className="rs-btn" onClick={() => onTab("API access")}>
              <Link2 size={15} aria-hidden="true" />
              {t("personal.apiAccess")}
            </button>
            {owner?.own && (
              <button type="button" className="rs-btn" onClick={onManage}>
                <Share2 size={15} aria-hidden="true" />
                {t("personal.manageAccess")}
              </button>
            )}
          </div>
          <div className="rs-manage">
            <div>
              <h3>{t("personal.manageOnName", { name: resolveName(service.deviceId) })}</h3>
              <p>{t("personal.serviceSetupIsManagedOnTheDeviceRunningIt")}</p>
            </div>
            <Link className="rs-link" to={deviceUrl}>
              {t("personal.viewDevice")} <ExternalLink size={14} aria-hidden="true" />
            </Link>
          </div>
        </div>
      ) : (
        <div className="rs-detail-body">
          <p className="rs-description">{t("personal.connectYourAppsAndAgentsThroughTheLocalInferenceAPI")}</p>
          {service.kind === "ai" ? (
            <div className="rs-actions">
              <Link className="rs-btn primary" to={href("/services/api")}>
                <Link2 size={16} aria-hidden="true" />
                {t("personal.configureAPIAccess")}
              </Link>
            </div>
          ) : (
            <p className="rs-callout">
              <Info size={16} aria-hidden="true" />
              <span>
                {service.preview
                  ? t("personal.thisSampleServiceDoesNotProvideAWorkingAPI")
                  : t("personal.apiDetailsAreProvidedByTheServiceOwner")}
              </span>
            </p>
          )}
        </div>
      )}
    </aside>
  );
}
