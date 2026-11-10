import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, Copy, Ellipsis, Eye, Info, MessageCircle, Pencil, Send, Share2, ShieldAlert, WifiOff } from "lucide-react";
import type { Service } from "../model";
import { personalHref, usePersonal } from "../model";
import { personalLabelKeys } from "../labels";
import type { MapAnchor } from "../DeviceTopology";
import { useDrop } from "../dropStore";
import type { Transfer } from "../../domain/dropClient";
import { canSendTo, deviceSummary, relationshipLabel, routeLabel, shortId, type DropDevice } from "./dropModel";
import { TransferStrip, type TransferHandlers } from "./TransferCard";
import {
  Callout, DeviceLine, DeviceTile, Pill, ServiceTile, StatusPill, connectionOf, deviceHue, hueOfService, routeKind, sendLabel,
} from "./DevicesParts";

type DetailTab = "overview" | "services";

/** This device, the route between the two, and the selected device. */
function ConnectionStrip({ device, self }: { device: DropDevice; self?: DropDevice }) {
  const { t } = useTranslation();
  const kind = routeKind(device);
  return (
    <div className="rd-strip">
      <div className="rd-end rd-h-indigo">
        <span className="rd-dg"><DeviceLine kind={self?.kind || "desktop"} /></span>
        <div><b>{self?.name || t("drop.thisDevice")}</b><small>{t("drop.thisDevice")}</small></div>
      </div>
      <div className={`rd-wire rd-k-${kind}`} aria-hidden="true" />
      <div className={`rd-end rd-h-${deviceHue(device)}`}>
        <span className="rd-dg"><DeviceLine kind={device.kind} /></span>
        <div><b>{device.name}</b><small>{relationshipLabel(t, device)}</small></div>
      </div>
      <span className={`rd-rt rd-k-${kind}`}>
        {kind !== "off" && kind !== "unknown" && <svg className="rd-lock" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={t("drop.encryptedLink")}><path d="M5 11h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM7 11V7a5 5 0 0 1 10 0v4" /></svg>}
        {deviceSummary(t, device)}
      </span>
    </div>
  );
}

const serviceState = (service: Service) => (service.availability === "needs_connection" ? "warn" : service.online ? "ok" : "off");

export function DeviceDetail({ device, self, offered, transfer, handlers, onSend, onMessage, onDetails, onEdit, onShare, onMenu }: {
  device: DropDevice;
  self?: DropDevice;
  offered: Service[];
  transfer?: Transfer;
  handlers: TransferHandlers;
  onSend: (device: DropDevice) => void;
  onMessage: (id: string) => void;
  onDetails: (id: string) => void;
  onEdit: (device: DropDevice) => void;
  onShare: (device: DropDevice) => void;
  onMenu: (device: DropDevice, anchor: MapAnchor) => void;
}) {
  const { t } = useTranslation();
  const { demo } = usePersonal();
  const drop = useDrop();
  const [tab, setTab] = useState<DetailTab>("overview");
  const deviceId = device.meta?.device_peer_id || device.id || "";
  const hardware = device.device?.hardware;
  const relation = [relationshipLabel(t, device), hardware].filter(Boolean).join(" · ");
  const sendable = canSendTo(device);
  const needsVerify = device.meta?.send_mode === "verify" && !device.meta.verification.verified;
  const network = device.device?.network;
  const networkText = [network?.label ? (personalLabelKeys[network.label] ? t(`personal.${personalLabelKeys[network.label]}`) : network.label) : "", network?.address].filter(Boolean).join(" · ");
  const copyId = async () => {
    try { await navigator.clipboard.writeText(deviceId); drop.notify("ok", t("personal.deviceIDCopied")); }
    catch { drop.notify("warn", t("personal.couldNotCopyTheDeviceID")); }
  };
  const facts: [string, string][] = [
    [t("personal.deviceName"), device.name],
    [t("personal.privateNoteLabel"), device.device?.note || t("personal.notSet")],
  ];
  if (!device.self) facts.push([t("drop.route"), device.online === false ? t("drop.offline") : routeLabel(t, device.route)]);
  const tabs: [DetailTab, string, number | undefined][] = [["overview", t("personal.overview"), undefined], ["services", t("nav.services"), offered.length]];
  return (
    <aside className={`rd-detail rd-h-${deviceHue(device)}`} aria-label={device.name}>
      <div className="rd-detail-top">
        <DeviceTile device={device} size="xl" />
        <div className="rd-detail-id">
          <h2>{device.name}</h2>
          <div className="rd-detail-sub"><StatusPill device={device} /><span>{relation}</span></div>
          <div className="rd-idrow">
            <span>{t("drop.deviceId")}</span>
            <code title={deviceId}>{shortId(deviceId)}</code>
            <button type="button" className="rd-ib xs" aria-label={t("personal.copyDeviceID")} onClick={() => void copyId()}><Copy size={13} strokeWidth={1.8} /></button>
          </div>
        </div>
        <button type="button" className="rd-ib" aria-haspopup="menu" aria-label={t("drop.deviceActions", { name: device.name })}
          onClick={event => onMenu(device, event.currentTarget)}><Ellipsis size={18} /></button>
      </div>

      <div className="rd-dtabs" role="tablist" aria-label={t("drop.detailTabs")}>
        {tabs.map(([id, text, count]) => (
          <button key={id} type="button" role="tab" id={`rd-dtab-${id}`} aria-controls={`rd-dpanel-${id}`} aria-selected={tab === id} onClick={() => setTab(id)}>
            {text}
            {count !== undefined && <em aria-hidden="true">{count}</em>}
          </button>
        ))}
      </div>

      {tab === "overview" ? (
        <div className="rd-detail-body" role="tabpanel" id="rd-dpanel-overview" aria-labelledby="rd-dtab-overview">
          {!device.self && <ConnectionStrip device={device} self={self} />}
          {device.self && (
            <div className="rd-selfbox">
              <span className="rd-dg"><DeviceLine kind={device.kind} /></span>
              <div><b>{t("drop.thisDeviceSub")}</b>{networkText && <small>{networkText}</small>}</div>
            </div>
          )}
          {!device.self && device.online === false && <Callout icon={<WifiOff size={15} strokeWidth={1.7} />}>{t("drop.footerQueue")}</Callout>}
          {needsVerify && (
            <Callout tone="warn" icon={<ShieldAlert size={15} strokeWidth={1.7} />}
              action={<button type="button" className="rd-btn sm" onClick={() => onDetails(device.id)}>{t("drop.verifyNearby")}</button>}>
              {t("drop.verifyRequired")}
            </Callout>
          )}
          <div className="rd-actions">
            {!device.self && (
              <>
                <button type="button" className="rd-btn primary" disabled={!sendable} onClick={() => onSend(device)}><Send size={15} strokeWidth={1.7} aria-hidden="true" />{sendLabel(t, device)}</button>
                <button type="button" className="rd-btn" disabled={!device.meta || device.relationship === "other"} onClick={() => onMessage(device.id)}><MessageCircle size={15} strokeWidth={1.7} aria-hidden="true" />{t("drop.message")}</button>
              </>
            )}
            {device.device && <button type="button" className="rd-btn" onClick={() => onEdit(device)}><Pencil size={15} strokeWidth={1.7} aria-hidden="true" />{t("personal.editDetails")}</button>}
            {!device.self && <button type="button" className="rd-btn icon" onClick={() => onDetails(device.id)}><Info size={16} strokeWidth={1.7} aria-hidden="true" /><span className="rd-lab">{t("drop.connectionDetails")}</span></button>}
          </div>
          {transfer && (
            <section className="rd-active" aria-label={t("drop.activeTransferFor", { name: device.name })}>
              <div className="rd-active-head">
                <h3>{t("drop.activeTransferFor", { name: device.name })}</h3>
                <Link className="rd-link" to={personalHref(`/devices?tab=transfers&device=${encodeURIComponent(device.id)}`, demo)}>{t("drop.allTransfers")}</Link>
              </div>
              <TransferStrip transfer={transfer} handlers={handlers} />
            </section>
          )}
          <dl className="rd-facts">
            {facts.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}
          </dl>
          <p className="rd-note"><Eye size={13} strokeWidth={1.8} aria-hidden="true" />{t("personal.onlyYouCanSeePrivateNotes")}</p>
        </div>
      ) : (
        <div className="rd-detail-body" role="tabpanel" id="rd-dpanel-services" aria-labelledby="rd-dtab-services">
          <div className="rd-svclist">
            {offered.map(service => (
              <div key={service.id} className={`rd-svc rd-h-${hueOfService(service.kind)}`}>
                <ServiceTile kind={service.kind} />
                <div className="rd-svc-text">
                  <span className="rd-svc-title">{service.title}{service.accessLabel && <span className="rd-tag">{service.accessLabel}</span>}</span>
                  <small>{device.relationship === "own" ? t("personal.providedByThisDevice") : t("personal.advertisedByThisDevice")}</small>
                </div>
                <Pill tone={serviceState(service)}>
                  {service.availability === "needs_connection" ? t("personal.notConnected") : service.online ? t("personal.available") : t("personal.offline")}
                </Pill>
              </div>
            ))}
            {!offered.length && <p className="rd-svc-none">{t("personal.noServicesAdvertisedOnThisDevice")}</p>}
            <Link className="rd-svc-more" to={personalHref("/services", demo)}>{t("personal.viewServices")}<ArrowRight size={15} strokeWidth={1.7} aria-hidden="true" /></Link>
          </div>
          {device.device && device.relationship === "own" ? (
            <div className="rd-share">
              <div>
                <h3>{t("personal.shareServices")}</h3>
                <p>{t("personal.onlySelectedServicesWillBeSharedThisDoesNotGrantDesktopOrFullComputerControl")}</p>
              </div>
              <button type="button" className="rd-btn sm" onClick={() => onShare(device)}><Share2 size={14} strokeWidth={1.7} aria-hidden="true" />{t("personal.chooseServices")}</button>
            </div>
          ) : (
            <Callout icon={<Info size={15} strokeWidth={1.7} />}>{t("personal.serviceAccessIsManagedByTheDeviceOwner")}</Callout>
          )}
        </div>
      )}
    </aside>
  );
}
