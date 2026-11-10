import { useTranslation } from "react-i18next";
import { useLayoutEffect, useRef, useState, type DependencyList, type DragEvent, type KeyboardEvent, type MouseEvent, type RefObject } from "react";
import { Cloud, Ellipsis, Globe2, Laptop, Monitor, Network, Pencil, Share2 } from "lucide-react";
import type { Device } from "./model";
import { personalLabelKeys } from "./labels";
import { routeLabel } from "./deviceNetwork";
import type { DropDevice } from "./drop/dropModel";
import { deviceSummary, statusLabel } from "./drop/dropModel";
import "./device-topology.css";

type Props = {
  devices: Device[];
  selected?: Device;
  loading?: boolean;
  demo?: boolean;
  onSelect: (id: string) => void;
  onEdit: (device: Device) => void;
  onShare: () => void;
  /** Home draws its own "My devices" heading and selected-device card around the map. */
  hideHeading?: boolean;
  hideDetail?: boolean;
};
type Wire = { id: string; path: string; kind: string; selected: boolean };

export function DeviceTopology({ devices, selected, loading, demo, onSelect, onEdit, onShare, hideHeading, hideDetail }: Props) {
  const { t } = useTranslation();
  const label = (value: string) => personalLabelKeys[value] ? t(`personal.${personalLabelKeys[value]}`) : value;
  const canvas = useRef<HTMLDivElement>(null);
  const [wires, setWires] = useState<Wire[]>([]);
  const self = devices.find(d => d.self);
  const local = devices.filter(d => !d.self && d.network?.scope === "local");
  const other = devices.filter(d => !d.self && d.network?.scope !== "local");
  const groups = new Map<string, { label: string; scope: string; devices: Device[] }>();
  for (const device of other) {
    const key = device.network?.id || "unconfirmed";
    if (!groups.has(key)) groups.set(key, {
      label: label(device.network?.label || "Network unconfirmed"), scope: device.network?.scope || "unknown", devices: [],
    });
    groups.get(key)!.devices.push(device);
  }
  useMeasuredWires(canvas, ".pf-topology-group, .pf-topology-node", (container) => {
      const bounds = container.getBoundingClientRect();
      const anchors = new Map(Array.from(container.querySelectorAll<HTMLElement>("[data-network-anchor]"))
        .map(el => [el.dataset.networkAnchor!, el.getBoundingClientRect()]));
      const origin = self && anchors.get(self.id);
      if (!origin || !bounds.width) { setWires([]); return; }
      const mobile = getComputedStyle(container).flexDirection === "column";
      const path = (a: DOMRect, b: DOMRect, vertical = mobile) => {
        if (vertical) {
          const x1 = a.x + a.width / 2 - bounds.x, y1 = a.bottom - bounds.y + 42;
          const x2 = b.x + b.width / 2 - bounds.x, y2 = b.top - bounds.y - 4;
          const mid = (y1 + y2) / 2;
          return `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`;
        }
        const x1 = a.right - bounds.x + 4, y1 = a.y + a.height / 2 - bounds.y;
        const x2 = b.left - bounds.x - 4, y2 = b.y + b.height / 2 - bounds.y;
        const mid = (x1 + x2) / 2;
        const radius = Math.min(9, Math.abs(y2 - y1) / 2, Math.abs(x2 - x1) / 4);
        const sign = y2 >= y1 ? 1 : -1;
        return `M${x1},${y1} H${mid-radius} Q${mid},${y1} ${mid},${y1+sign*radius} V${y2-sign*radius} Q${mid},${y2} ${mid+radius},${y2} H${x2}`;
      };
      const next: Wire[] = [];
      const hub = container.querySelector<HTMLElement>(".pf-topology-hub-icon")?.getBoundingClientRect();
      if (other.length && hub) next.push({ id: "hub", path: path(origin, hub),
        kind: other.some(d => d.online === true && ["public", "relay"].includes(d.network?.route || "")) ? "public" : "unknown", selected: false });
      for (const device of devices) {
        if (device.self) continue;
        const target = anchors.get(device.id);
        const start = device.network?.scope === "local" ? origin : hub;
        let connection = start && target ? path(start, target, false) : "";
        if (mobile && start && target && device.network?.scope !== "local") {
          // Route down the outside gutter, never through group headings or other devices.
          const x1 = start.x + start.width / 2 - bounds.x, y1 = start.bottom - bounds.y + 28;
          const x2 = target.left - bounds.x - 5, y2 = target.y + target.height / 2 - bounds.y;
          connection = `M${x1},${y1} H18 Q10,${y1} 10,${y1+8} V${y2-8} Q10,${y2} 18,${y2} H${x2}`;
        }
        if (start && target) next.push({ id: device.id, path: connection,
          kind: device.online !== true ? "inactive" : device.network?.route || "unknown", selected: selected?.id === device.id });
      }
      setWires(next);
  }, [devices, selected?.id, self?.id, t]);

  const node = (device: Device) => {
    const Icon = device.network?.scope === "public" ? Cloud : device.kind === "laptop" ? Laptop : Monitor;
    const status = device.online === true ? t("personal.online") : device.online === false ? t("personal.offline") : t("personal.statusUnknown");
    return <div key={device.id} className={`pf-topology-node${device.self ? " self" : ""}`}>
      <button type="button" className="pf-topology-select"
        aria-pressed={selected?.id === device.id} aria-label={`${device.name}, ${device.self ? t("personal.thisDevicePrefix") : ""}${status}`}
        onClick={() => onSelect(device.id)}>
        <span className="pf-topology-icon" data-network-anchor={device.id}>
          <Icon size={20} strokeWidth={1.4} aria-hidden="true" />
          <i className={device.online === true ? "online" : device.online === false ? "offline" : "unknown"} />
        </span>
      </button>
      <button type="button" className="pf-topology-name pf-topology-rename"
        aria-label={t("personal.editName", { name: device.name })}
        title={t("personal.editName", { name: device.name })}
        onClick={() => { onSelect(device.id); onEdit(device); }}>
        <span>{device.name}</span><Pencil size={12} aria-hidden="true" />
      </button>
      {(device.self || device.online !== true) && <small>{device.self ? t("personal.thisDevice") : status}</small>}
    </div>;
  };
  return <section className="pf-topology" aria-label={t("personal.deviceNetwork")}>
    {!hideHeading && <div className="pf-topology-heading"><h2>{t("personal.myDevices")} <span>({devices.length})</span></h2><span><Network size={14} /> {t("personal.connectionView")}</span></div>}
    {devices.length === 0 ? <p role="status">{loading ? t("personal.findingDevices") : t("personal.noDevicesYetAddADeviceToGetStarted")}</p> : <>
      <div ref={canvas} className={`pf-topology-canvas${other.length ? "" : " local-only"}`}>
        <svg className="pf-topology-wires" aria-hidden="true">{wires.map(w => <path key={w.id} d={w.path} className={`${w.kind}${w.selected ? " selected" : ""}`} />)}</svg>
        <section className="pf-topology-group local" aria-label={label(self?.network?.label || "Current network")}>
          <header><strong>{label(self?.network?.label || "Current network")}</strong><small>{t("personal.youAreHereCountDevices", { count: local.length + (self ? 1 : 0) })}</small></header>
          <div className="pf-topology-local-nodes">
            {self && <div className="pf-topology-origin">{node(self)}</div>}
            {local.length > 0 && <div className="pf-topology-local-peers">{local.map(node)}</div>}
          </div>
        </section>
        {other.length > 0 && <>
          <div className="pf-topology-hub"><span className="pf-topology-hub-icon"><Globe2 size={18} strokeWidth={1.3} /></span><small>{t("personal.networks")}</small></div>
          <div className="pf-topology-remote">{Array.from(groups, ([id, group]) => <section key={id} className={`pf-topology-group ${group.scope}`} aria-label={group.label}>
            <header><strong>{group.label}</strong><small>{group.scope === "unknown" ? t("personal.lanMembershipNotAvailable") : group.scope === "public" ? t("personal.advertisedPublicAddresses") : t("personal.remoteNetwork")} · {t("personal.countDevices", { count: group.devices.length })}</small></header>
            <div className="pf-topology-remote-nodes">{group.devices.map(node)}</div>
          </section>)}</div>
        </>}
      </div>
      {devices.length > 1 && <div className="pf-topology-legend"><span className="lan">{t("personal.lanDirect")}</span><span className="public">{t("personal.publicRelay")}</span><span className="unknown">{t("personal.unconfirmedOffline")}</span></div>}
      {selected && !hideDetail && <div className="pf-topology-detail" aria-live="polite">
        <div><strong>{selected.name}</strong><small>{selected.self ? t("personal.thisDevice") : selected.online === false ? t("personal.offlineLastKnownNetwork") : selected.online === null ? t("personal.statusUnknownRouteUnconfirmed") : label(routeLabel(selected.network?.route))}</small>{selected.network?.address && <small>{t("personal.advertisedDiscoveryAddress", { address: selected.network.address })}</small>}{selected.note && <small>{selected.note}</small>}</div>
        <div className="pf-topology-actions"><button className="pf-icon-button" aria-label={t("personal.editName", { name: selected.name })} onClick={() => onEdit(selected)}><Pencil size={14} /></button><button className="pf-button" onClick={onShare}><Share2 size={14} />{t("personal.shareService")}</button></div>
      </div>}
      {demo && <p className="pf-topology-preview">{t("personal.designPreviewSampleNetwork")}</p>}
    </>}
  </section>;
}

/** Recomputes wires from real element positions whenever layout changes. */
export function useMeasuredWires(
  ref: RefObject<HTMLElement | null>,
  observeSelector: string,
  draw: (container: HTMLElement) => void,
  deps: DependencyList,
) {
  const drawRef = useRef(draw);
  drawRef.current = draw;
  useLayoutEffect(() => {
    const container = ref.current;
    if (!container) return;
    const run = () => drawRef.current(container);
    run();
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(run);
    observer?.observe(container);
    container.querySelectorAll<HTMLElement>(observeSelector).forEach(el => observer?.observe(el));
    window.addEventListener("resize", run);
    return () => { observer?.disconnect(); window.removeEventListener("resize", run); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export type MapAnchor = { x: number; y: number } | HTMLElement;
type MapProps = {
  self?: DropDevice;
  devices: DropDevice[];
  selectedId?: string;
  dropTargetId?: string;
  onSelect: (id: string) => void;
  onMenu: (device: DropDevice, anchor: MapAnchor) => void;
  onDropFiles: (device: DropDevice, event: DragEvent<HTMLElement>) => void;
};
type MapWire = { id: string; path: string; kind: string; selected: boolean };

const wireKind = (device: DropDevice) => device.online === true || device.route
  ? device.route || "unknown" : "unknown";

/**
 * Devices workspace map. Self is on the left; LAN devices are wired directly,
 * remote devices go through the internet hub (a cross-network path, not a
 * server). Only devices that are not known to be offline are drawn here.
 */
export function DeviceNetworkMap({ self, devices, selectedId, dropTargetId, onSelect, onMenu, onDropFiles }: MapProps) {
  const { t } = useTranslation();
  const canvas = useRef<HTMLDivElement>(null);
  const [wires, setWires] = useState<MapWire[]>([]);
  const [dragOver, setDragOver] = useState("");
  const lan = devices.filter(d => d.scope === "lan");
  const remote = devices.filter(d => d.scope === "remote");
  useMeasuredWires(canvas, ".pf-netmap-node, .pf-netmap-column", (container) => {
    const bounds = container.getBoundingClientRect();
    const anchor = (id: string) => container.querySelector<HTMLElement>(`[data-netmap-anchor="${CSS.escape(id)}"]`)?.getBoundingClientRect();
    const origin = self ? anchor(self.id) : undefined;
    if (!origin || !bounds.width) { setWires([]); return; }
    const hub = container.querySelector<HTMLElement>("[data-netmap-hub]")?.getBoundingClientRect();
    const across = (a: DOMRect, b: DOMRect) => {
      const x1 = a.right - bounds.x + 2, y1 = a.y + a.height / 2 - bounds.y;
      const x2 = b.left - bounds.x - 2, y2 = b.y + b.height / 2 - bounds.y;
      const dx = Math.max(24, (x2 - x1) / 2);
      return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    };
    const down = (a: DOMRect, b: DOMRect) => {
      const x1 = a.x + a.width / 2 - bounds.x, y1 = a.bottom - bounds.y + 2;
      const x2 = b.x + b.width / 2 - bounds.x, y2 = b.top - bounds.y - 2;
      const dy = (y2 - y1) / 2;
      return `M${x1},${y1} C${x1},${y1 + dy} ${x2},${y2 - dy} ${x2},${y2}`;
    };
    const next: MapWire[] = [];
    if (hub && remote.length) {
      const vertical = hub.top >= origin.bottom - 1 && hub.left < origin.right;
      next.push({ id: "hub", path: vertical ? down(origin, hub) : across(origin, hub),
        kind: remote.some(d => d.route) ? "public" : "unknown", selected: false });
    }
    for (const device of devices) {
      const target = anchor(device.id);
      const start = device.scope === "lan" ? origin : hub;
      if (!target || !start) continue;
      next.push({ id: device.id, path: across(start, target), kind: wireKind(device), selected: device.id === selectedId });
    }
    setWires(next);
  }, [devices, selectedId, self?.id, t]);

  const menuKey = (device: DropDevice) => (event: KeyboardEvent<HTMLElement>) => {
    if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") {
      event.preventDefault();
      onSelect(device.id);
      onMenu(device, event.currentTarget);
    }
  };
  const contextMenu = (device: DropDevice) => (event: MouseEvent<HTMLElement>) => {
    event.preventDefault();
    onSelect(device.id);
    onMenu(device, event.clientX || event.clientY ? { x: event.clientX, y: event.clientY } : event.currentTarget);
  };
  const Icon = (device: DropDevice) => (device.kind === "laptop" ? Laptop : Monitor);
  const node = (device: DropDevice) => {
    const DeviceIcon = Icon(device);
    const selected = device.id === selectedId;
    const status = statusLabel(t, device.online);
    const summary = deviceSummary(t, device);
    return <div key={device.id}
      className={`pf-netmap-node${selected ? " selected" : ""}${dragOver === device.id || dropTargetId === device.id ? " drop-target" : ""}`}
      data-drop-peer={device.id}
      onDragEnter={event => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragOver(device.id); } }}
      onDragOver={event => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } }}
      onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragOver(""); }}
      onDrop={event => { event.preventDefault(); setDragOver(""); onSelect(device.id); onDropFiles(device, event); }}>
      <button type="button" className="pf-netmap-select" aria-pressed={selected}
        aria-label={t("drop.selectDevice", { name: device.name, status: device.online === true ? summary : status })}
        onClick={() => onSelect(device.id)} onContextMenu={contextMenu(device)} onKeyDown={menuKey(device)}>
        <span className="pf-netmap-visual" data-netmap-anchor={device.id}><DeviceIcon size={30} strokeWidth={1.3} aria-hidden="true" /></span>
        <span className="pf-netmap-name">{device.name}</span>
        <span className="pf-netmap-detail">
          <i className={`pf-netmap-dot ${device.online === true ? "online" : device.online === false ? "offline" : "unknown"}`} aria-hidden="true" />
          {summary}
        </span>
      </button>
      <button type="button" className="pf-netmap-more pf-icon-button" aria-haspopup="menu"
        aria-label={t("drop.moreActions", { name: device.name })}
        onClick={event => { onSelect(device.id); onMenu(device, event.currentTarget); }} onKeyDown={menuKey(device)}>
        <Ellipsis size={16} aria-hidden="true" />
      </button>
      {(dragOver === device.id || dropTargetId === device.id) && <span className="pf-netmap-drophint" role="status">{t("drop.dropHere", { name: device.name })}</span>}
    </div>;
  };
  const SelfIcon = self?.kind === "laptop" ? Laptop : Monitor;
  return <div ref={canvas} className={`pf-netmap${remote.length ? "" : " no-remote"}${lan.length ? "" : " no-lan"}`}>
    <svg className="pf-netmap-wires" aria-hidden="true">{wires.map(w => <path key={w.id} d={w.path} className={`${w.kind}${w.selected ? " selected" : ""}`} />)}</svg>
    {self && <div className="pf-netmap-self pf-netmap-column">
      <div className="pf-netmap-node self" aria-label={`${self.name}, ${t("drop.thisDevice")}`} role="img">
        <span className="pf-netmap-visual" data-netmap-anchor={self.id}><SelfIcon size={32} strokeWidth={1.3} aria-hidden="true" /></span>
        <span className="pf-netmap-name">{self.name}</span>
        <span className="pf-netmap-detail">{t("drop.thisDeviceSub")}</span>
      </div>
    </div>}
    <div className="pf-netmap-lan pf-netmap-column">{lan.map(node)}</div>
    {remote.length > 0 && <div className="pf-netmap-hub pf-netmap-column">
      <span className="pf-netmap-hub-icon" data-netmap-hub title={t("drop.internetHint")}><Globe2 size={17} strokeWidth={1.4} aria-hidden="true" /></span>
      <small>{t("drop.internet")}</small>
    </div>}
    <div className="pf-netmap-remote pf-netmap-column">{remote.map(node)}</div>
  </div>;
}
