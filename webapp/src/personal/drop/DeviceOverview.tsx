import { useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Copy, Ellipsis, Info, MessageCircle, Pencil, Send, Share2 } from "lucide-react";
import { Arrow, DeviceArt, Empty, Note, ServiceArt, Status } from "../components";
import { EditDevice, ShareServices } from "../Dialogs";
import { personalHref, usePersonal } from "../model";
import type { MapAnchor } from "../DeviceTopology";
import { useDrop } from "../dropStore";
import { canSendTo, deviceSummary, relationshipLabel, shortId, type DropDevice } from "./dropModel";
import { RouteChip, TransferStrip, type TransferHandlers } from "./TransferCard";
import type { Transfer } from "../../domain/dropClient";

/** The existing list/detail device manager, extended with device-scoped actions. */
export function DeviceOverview({ devices, selected, onSelect, onMenu, onSend, onMessage, onDetails, onDropFiles, dropTargetId, transfer, handlers }: {
  devices: DropDevice[]; selected?: DropDevice; onSelect: (id: string) => void;
  onMenu: (device: DropDevice, anchor: MapAnchor) => void;
  onSend: (device: DropDevice) => void; onMessage: (id: string) => void; onDetails: (id: string) => void;
  onDropFiles: (device: DropDevice, event: DragEvent<HTMLElement>) => void;
  dropTargetId: string; transfer?: Transfer; handlers: TransferHandlers;
}) {
  const { t } = useTranslation();
  const { services, demo } = usePersonal();
  const drop = useDrop();
  const [edit, setEdit] = useState<DropDevice | null>(null);
  const [share, setShare] = useState<DropDevice | null>(null);
  const [dragId, setDragId] = useState("");
  const offered = (services || []).filter(service => service.deviceId === selected?.id);
  const selectedDeviceId = selected?.meta?.device_peer_id || selected?.id || "";
  const openMenu = (device: DropDevice, anchor: MapAnchor) => { onSelect(device.id); onMenu(device, anchor); };
  return <div className="pf-devices-layout pf-drop-overview">
    <div className="pf-device-list" aria-label={t("drop.tabDevices")}>
      {devices.map(device => <div key={device.id} data-drop-peer={device.id}
        className={`pf-drop-device-item${device.id === selected?.id ? " selected" : ""}${dragId === device.id || dropTargetId === device.id ? " drop-target" : ""}`}
        onDragOver={event => { if (canSendTo(device) && event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragId(device.id); } }}
        onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragId(""); }}
        onDrop={event => { event.preventDefault(); setDragId(""); if (canSendTo(device)) { onSelect(device.id); onDropFiles(device, event); } }}>
        <button type="button" className={`pf-device-list-item${device.id === selected?.id ? " selected" : ""}`} aria-pressed={device.id === selected?.id}
          onClick={() => onSelect(device.id)}
          onContextMenu={event => { event.preventDefault(); event.currentTarget.focus(); openMenu(device, event.clientX || event.clientY ? { x: event.clientX, y: event.clientY } : event.currentTarget); }}
          onKeyDown={event => { if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") { event.preventDefault(); openMenu(device, event.currentTarget); } }}>
          <DeviceArt kind={device.kind} small />
          <div className="pf-drop-grow"><div><strong>{device.name}</strong><Status online={device.online} self={device.self} /></div>
            <small>{relationshipLabel(t, device)}</small>
            <small className="pf-ellipsis">{device.self ? `${t("personal.privateNote")} ${device.device?.note || t("personal.notSet")}` : deviceSummary(t, device)}</small>
          </div><Arrow />
        </button>
        <button type="button" className="pf-icon-button pf-drop-device-more" aria-haspopup="menu" aria-label={t("drop.moreActions", { name: device.name })}
          onClick={event => openMenu(device, event.currentTarget)}><Ellipsis size={16} /></button>
      </div>)}
      {!devices.length && <Empty title={t("personal.noDevicesHereYet")}>{t("personal.addADeviceToGetStarted")}</Empty>}
    </div>
    {selected && <div className="pf-drop-device-details">
      <section className="pf-panel pf-device-detail">
        <div className="pf-detail-lockup"><DeviceArt kind={selected.kind} />
          <div className="pf-drop-grow"><div className="pf-inline"><h2>{selected.name}</h2><Status online={selected.online} self={selected.self} /></div>
            <p>{relationshipLabel(t, selected)}</p>
            <div className="pf-device-id">{t("personal.deviceID")} <code title={selectedDeviceId}>{shortId(selectedDeviceId)}</code>
              <button type="button" className="pf-icon-button" aria-label={t("personal.copyDeviceID")} onClick={async () => {
                try { await navigator.clipboard.writeText(selectedDeviceId); drop.notify("ok", t("personal.deviceIDCopied")); }
                catch { drop.notify("warn", t("personal.couldNotCopyTheDeviceID")); }
              }}><Copy size={14} /></button>
            </div>
          </div>
          <button type="button" className="pf-icon-button" aria-haspopup="menu" aria-label={t("drop.deviceActions", { name: selected.name })}
            onClick={event => openMenu(selected, event.currentTarget)}><Ellipsis size={18} /></button>
        </div>
        <dl className="pf-properties">
          <div><dt>{t("personal.deviceName")}</dt><dd>{selected.name}</dd></div>
          <div><dt>{t("personal.privateNoteLabel")}</dt><dd>{selected.device?.note || t("personal.notSet")}</dd></div>
          {!selected.self && <div><dt>{t("drop.route")}</dt><dd>{selected.online === false ? t("drop.offline") : <RouteChip route={selected.route} muted />}</dd></div>}
        </dl>
        <div className="pf-drop-detail-actions">
          {!selected.self && <><button type="button" className="pf-button primary" disabled={!canSendTo(selected)} onClick={() => onSend(selected)}><Send size={15} />{t("drop.sendFile")}</button>
            <button type="button" className="pf-button" disabled={!selected.meta || selected.relationship === "other"} onClick={() => onMessage(selected.id)}><MessageCircle size={15} />{t("drop.message")}</button></>}
          {selected.device && <button type="button" className="pf-button" onClick={() => setEdit(selected)}><Pencil size={15} />{t("personal.editDetails")}</button>}
          {!selected.self && <button type="button" className="pf-icon-button" aria-label={t("drop.connectionDetails")} onClick={() => onDetails(selected.id)}><Info size={17} /></button>}
        </div>
        <Note>{t("personal.onlyYouCanSeePrivateNotes")}</Note>
      </section>
      {transfer && <section className="pf-drop-active"><div className="pf-drop-between"><h3>{t("drop.activeTransferFor", { name: selected.name })}</h3>
        <Link className="pf-drop-link" to={personalHref(`/devices?tab=transfers&device=${encodeURIComponent(selected.id)}`, demo)}>{t("drop.allTransfers")}</Link></div>
        <TransferStrip transfer={transfer} handlers={handlers} />
      </section>}
      <div className="pf-section-heading"><h2>{t("personal.access")}</h2></div>
      <div className="pf-panel pf-access-list">
        {offered.map(service => <div className="pf-access-row" key={service.id}><ServiceArt kind={service.kind} brand={service.brand} small />
          <div><h3>{service.title}</h3><small>{selected.relationship === "own" ? t("personal.providedByThisDevice") : t("personal.advertisedByThisDevice")}</small></div>
          <Status online={service.online}
            label={service.availability === "needs_connection" ? "Not connected" : service.online ? "Available" : "Offline"} />
        </div>)}
        {!offered.length && <Note>{t("personal.noServicesAdvertisedOnThisDevice")}</Note>}
        <Link className="pf-browse-row" to={personalHref("/services", demo)}>{t("personal.viewServices")}<Arrow /></Link>
      </div>
      <div className="pf-device-actions">
        {selected.device && selected.relationship === "own" ? <button type="button" className="pf-button" onClick={() => setShare(selected)}><Share2 size={16} />{t("personal.shareServices")}</button>
          : <Note>{t("personal.serviceAccessIsManagedByTheDeviceOwner")}</Note>}
        <button type="button" className="pf-drop-link" onClick={() => onDetails(selected.id)}>{t("personal.connectionDetails")}<Arrow /></button>
      </div>
    </div>}
    {edit?.device && <EditDevice device={edit.device} onClose={() => setEdit(null)} />}
    {share?.device && <ShareServices device={share.device} onClose={() => setShare(null)} />}
  </div>;
}
