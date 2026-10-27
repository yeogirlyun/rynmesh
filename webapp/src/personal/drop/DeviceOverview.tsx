import { useState, type DragEvent } from "react";
import { useTranslation } from "react-i18next";
import { Ellipsis, Plus, Send, Wifi } from "lucide-react";
import { EditDevice, ShareServices } from "../Dialogs";
import { usePersonal } from "../model";
import type { MapAnchor } from "../DeviceTopology";
import { canSendTo, relationshipLabel, type DropDevice } from "./dropModel";
import { type TransferHandlers } from "./TransferCard";
import type { Transfer } from "../../domain/dropClient";
import { DeviceDetail } from "./DeviceDetail";
import { DeviceTile, EmptyState, RouteIcon, StatusPill, connectionOf, deviceHue, sendLabel } from "./DevicesParts";
import { useDropDevices } from "./useDropDevices";

/** The device list with a detail panel beside it, extended with device-scoped actions. */
export function DeviceOverview({ devices, selected, onSelect, onMenu, onSend, onMessage, onDetails, onDropFiles, dropTargetId, transfer, handlers, onAdd, loading, searching }: {
  devices: DropDevice[]; selected?: DropDevice; onSelect: (id: string) => void;
  onMenu: (device: DropDevice, anchor: MapAnchor) => void;
  onSend: (device: DropDevice) => void; onMessage: (id: string) => void; onDetails: (id: string) => void;
  onDropFiles: (device: DropDevice, event: DragEvent<HTMLElement>) => void;
  dropTargetId: string; transfer?: Transfer; handlers: TransferHandlers;
  onAdd: () => void; loading?: boolean; searching?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { services } = usePersonal();
  const { self } = useDropDevices();
  const [edit, setEdit] = useState<DropDevice | null>(null);
  const [share, setShare] = useState<DropDevice | null>(null);
  const [dragId, setDragId] = useState("");
  const offered = (services || []).filter(service => service.deviceId === selected?.id);
  const openMenu = (device: DropDevice, anchor: MapAnchor) => { onSelect(device.id); onMenu(device, anchor); };
  return <div className="rd-colswrap">
    <div className="rd-cols">
      <div className="rd-list" aria-label={t("drop.tabDevices")}>
        {devices.length > 0 && <div className="rd-lh" aria-hidden="true">
          <span>{t("drop.colDevice")}</span><span>{t("drop.colConnection")}</span><span>{t("drop.colStatus")}</span><span />
        </div>}
        {devices.map(device => {
          const on = device.id === selected?.id;
          const conn = connectionOf(t, i18n.language, device);
          const note = device.device?.note;
          const sub = (device.self ? [note] : [relationshipLabel(t, device), note]).filter(Boolean).join(" · ") || relationshipLabel(t, device);
          return <div key={device.id} data-drop-peer={device.id}
            className={`rd-row rd-h-${deviceHue(device)}${on ? " selected" : ""}${device.online === false && !device.self ? " off" : ""}${dragId === device.id || dropTargetId === device.id ? " drop-target" : ""}`}
            onDragOver={event => { if (canSendTo(device) && event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragId(device.id); } }}
            onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragId(""); }}
            onDrop={event => { event.preventDefault(); setDragId(""); if (canSendTo(device)) { onSelect(device.id); onDropFiles(device, event); } }}>
            <button type="button" className="rd-select" aria-pressed={on}
              onClick={() => onSelect(device.id)}
              onContextMenu={event => { event.preventDefault(); event.currentTarget.focus(); openMenu(device, event.clientX || event.clientY ? { x: event.clientX, y: event.clientY } : event.currentTarget); }}
              onKeyDown={event => { if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") { event.preventDefault(); openMenu(device, event.currentTarget); } }}>
              <span className="rd-dev">
                <DeviceTile device={device} />
                <span className="rd-text"><span className="rd-name">{device.name}</span><span className="rd-sub">{sub}</span></span>
              </span>
              <span className={`rd-conn rd-k-${conn.kind}`}>
                <span className="rd-chip"><RouteIcon kind={conn.kind} /></span>
                <span className="rd-ct"><b>{conn.label}</b>{conn.sub && <small>{conn.sub}</small>}</span>
              </span>
              <span className="rd-st"><StatusPill device={device} /></span>
            </button>
            <span className="rd-act">
              {!device.self && <button type="button" className="rd-btn sm" disabled={!canSendTo(device)} onClick={() => onSend(device)}>
                <Send size={14} strokeWidth={1.7} aria-hidden="true" />{sendLabel(t, device)}</button>}
              <button type="button" className="rd-ib" aria-haspopup="menu" aria-label={t("drop.moreActions", { name: device.name })}
                onClick={event => openMenu(device, event.currentTarget)}><Ellipsis size={16} /></button>
            </span>
          </div>;
        })}
        {!devices.length && loading && <div className="rd-skel" role="status" aria-label={t("drop.loading")}>
          {[38, 30, 44].map(width => <div key={width}><span className="rd-sk tile" /><span className="rd-sk-lines"><span className="rd-sk" style={{ width: `${width}%` }} /><span className="rd-sk thin" style={{ width: `${width + 20}%` }} /></span><span className="rd-sk pill" /></div>)}
        </div>}
        {!devices.length && !loading && (searching
          ? <EmptyState icon={<Wifi size={24} strokeWidth={1.7} />} title={t("drop.noMatchingDevices")}>{t("drop.noMatchingDevicesHint")}</EmptyState>
          : <EmptyState icon={<Wifi size={24} strokeWidth={1.7} />} title={t("drop.noDevicesInFilter")}
            action={<button type="button" className="rd-btn primary" onClick={onAdd}><Plus size={15} strokeWidth={1.8} aria-hidden="true" />{t("personal.addDevice")}</button>}>
            {t("drop.noDevicesHint")}
          </EmptyState>)}
        {devices.length > 0 && <button type="button" className="rd-addrow" onClick={onAdd}>
          <span className="rd-addtile" aria-hidden="true"><Plus size={18} strokeWidth={1.8} /></span>
          <span className="rd-text"><b>{t("personal.addDevice")}</b><small>{t("drop.noDevicesHint")}</small></span>
        </button>}
      </div>
      {selected && <DeviceDetail device={selected} self={self} offered={offered} transfer={transfer} handlers={handlers}
        onSend={onSend} onMessage={onMessage} onDetails={onDetails} onEdit={setEdit} onShare={setShare} onMenu={openMenu} />}
    </div>
    {edit?.device && <EditDevice device={edit.device} onClose={() => setEdit(null)} />}
    {share?.device && <ShareServices device={share.device} onClose={() => setShare(null)} />}
  </div>;
}
