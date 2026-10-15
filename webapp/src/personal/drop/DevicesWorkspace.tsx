import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Info, Plus } from "lucide-react";
import { PageHeading } from "../components";
import { PairDevice } from "../Dialogs";
import { type MapAnchor } from "../DeviceTopology";
import { personalHref, usePersonal } from "../model";
import { useDrop } from "../dropStore";
import { canPickPaths, chooseDropPaths, listenNativeFileDrop } from "../../domain/dropDesktop";
import { isActiveTransfer, type Transfer } from "../../domain/dropClient";
import { DeviceActionsMenu, useDeviceMenu, type DeviceMenuAction } from "./DeviceActionsMenu";
import { DeviceSendSheet, type SendInit } from "./DeviceSendSheet";
import { DeviceConversation, firstThreadId } from "./DeviceConversation";
import { TransferList, type TransferFilter } from "./TransferList";
import { type TransferHandlers } from "./TransferCard";
import { ConnectionDetails } from "./ConnectionDetails";
import { filesFromDataTransfer, pickBrowserFiles } from "./fileCollect";
import { canSendTo, type DropDevice } from "./dropModel";
import { useDropDevices } from "./useDropDevices";
import { DeviceOverview } from "./DeviceOverview";
import "./drop.css";

type Tab = "network" | "conversations" | "transfers";
const TABS: Tab[] = ["network", "conversations", "transfers"];

export function DevicesWorkspace() {
  const { t } = useTranslation();
  const { error, refresh, demo } = usePersonal();
  const drop = useDrop();
  const { list, byId } = useDropDevices();
  const [params, setParams] = useSearchParams();
  const tab: Tab = TABS.includes(params.get("tab") as Tab) ? (params.get("tab") as Tab) : "network";
  const requested = params.get("device") || "";
  const [filter, setFilter] = useState<"own" | "discovered">("own");
  const [selectedId, setSelectedId] = useState(requested);
  const [transferFilter, setTransferFilter] = useState<TransferFilter>("all");
  const [mobileChat, setMobileChat] = useState(Boolean(params.get("peer")));
  const [pair, setPair] = useState(false);
  const [details, setDetails] = useState<string | null>(null);
  const [send, setSend] = useState<{ deviceId: string; init: SendInit } | null>(null);
  const [nativeTarget, setNativeTarget] = useState("");
  const sheetAppend = useRef<((paths: string[]) => void) | null>(null);
  const menu = useDeviceMenu();
  const registerAppend = useCallback((fn: ((paths: string[]) => void) | null) => { sheetAppend.current = fn; }, []);

  const setParam = useCallback((changes: Record<string, string | null>) => {
    setParams(current => {
      const next = new URLSearchParams(current);
      for (const [key, value] of Object.entries(changes)) { if (value) next.set(key, value); else next.delete(key); }
      return next;
    }, { replace: true });
  }, [setParams]);

  // Shell search and deep links: ?device= selects it in the right filter.
  const requestedDevice = byId.get(requested);
  useEffect(() => {
    if (!requestedDevice || tab !== "network") return;
    setSelectedId(requestedDevice.id);
    setFilter(requestedDevice.relationship === "own" ? "own" : "discovered");
  }, [requestedDevice, tab]);

  const filtered = list.filter(device => filter === "own" ? device.relationship === "own" : device.relationship !== "own");
  const selected = filtered.find(device => device.id === selectedId) || filtered[0];
  const activeForSelected = selected ? drop.transfers.find(transfer => transfer.peer_id === selected.id && isActiveTransfer(transfer)) : undefined;
  const unreadTotal = Object.values(drop.data.peers).reduce((sum, peer) => sum + (peer.unread || 0), 0);
  const activeCount = drop.transfers.filter(isActiveTransfer).length;
  const conversationId = params.get("peer") || firstThreadId(list);

  const startSend = useCallback(async (device: DropDevice, kind: "files" | "folder") => {
    if (!canSendTo(device)) return;
    if (canPickPaths()) {
      const paths = await chooseDropPaths(kind).catch(() => []);
      if (paths.length) setSend({ deviceId: device.id, init: { kind, paths } });
    } else {
      const files = await pickBrowserFiles(kind);
      if (files.length) setSend({ deviceId: device.id, init: { kind, files } });
    }
  }, []);
  const onDropFiles = async (device: DropDevice, event: DragEvent<HTMLElement>) => {
    if (!canSendTo(device) || canPickPaths()) return;
    try {
      const files = await filesFromDataTransfer(event.dataTransfer);
      if (files.length) setSend({ deviceId: device.id, init: { kind: "files", files } });
    } catch (reason) {
      drop.notify("warn", t("drop.sendFailed", { message: reason instanceof Error ? reason.message : String(reason) }));
    }
  };
  const openConversation = useCallback((peerId: string) => {
    setParam({ tab: "conversations", peer: peerId, device: null });
    setMobileChat(true);
  }, [setParam]);
  const handlers: TransferHandlers = useMemo(() => ({
    onResend: (transfer: Transfer) => { const device = byId.get(transfer.peer_id); if (device) void startSend(device, "files"); },
    onOpenConversation: openConversation,
    onDetails: (peerId: string) => setDetails(peerId),
  }), [byId, startSend, openConversation]);

  const onMenuAction = (action: DeviceMenuAction, device: DropDevice) => {
    if (action === "send_files") void startSend(device, "files");
    else if (action === "send_folder") void startSend(device, "folder");
    else if (action === "message") openConversation(device.id);
    else if (action === "history") setParam({ tab: "transfers", device: device.id, peer: null });
    else setDetails(device.id);
  };

  // Desktop drag-drop delivers real paths; dropping opens the confirmation sheet.
  const sendRef = useRef(send);
  sendRef.current = send;
  const byIdRef = useRef(byId);
  byIdRef.current = byId;
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let disposed = false;
    const peerAt = (x: number, y: number) => (document.elementFromPoint(x, y)?.closest("[data-drop-peer]") as HTMLElement | null)?.dataset.dropPeer || "";
    void listenNativeFileDrop(event => {
      if (event.type === "leave") { setNativeTarget(""); return; }
      if (event.type === "over") { setNativeTarget(sendRef.current ? "" : peerAt(event.x, event.y)); return; }
      setNativeTarget("");
      if (!event.paths.length) return;
      if (sendRef.current) { sheetAppend.current?.(event.paths); return; }
      const device = byIdRef.current.get(peerAt(event.x, event.y));
      if (device && canSendTo(device)) { setSelectedId(device.id); setSend({ deviceId: device.id, init: { kind: "files", paths: event.paths } }); }
    }).then(stop => { if (disposed) stop(); else unlisten = stop; }).catch(() => undefined);
    return () => { disposed = true; unlisten?.(); };
  }, []);

  const tabLabel = (value: Tab) => value === "network" ? t("drop.tabDevices") : value === "conversations" ? t("drop.tabConversations") : t("drop.tabTransfers");
  const tabCount = (value: Tab) => value === "conversations" ? unreadTotal : value === "transfers" ? activeCount : 0;
  const sendDevice = send ? byId.get(send.deviceId) : undefined;
  const detailsDevice = details ? byId.get(details) : undefined;

  return <div className={`pf-page pf-drop-page pf-drop-workspace${tab === "conversations" ? " messaging" : ""}`}>
    <div className="pf-drop-workspace-toolbar">
      <PageHeading title={t("nav.devices")} description={tab === "conversations" ? "" : t("drop.deviceWorkspaceSubtitle")}>
        <Link className="pf-drop-textbutton pf-drop-sync-link" to={personalHref("/devices/sync", demo)}>{t("drop.deviceSync")}</Link>
        <button className="pf-button dark" onClick={() => setPair(true)}><Plus size={17} />{t("personal.addDevice")}</button>
      </PageHeading>
    <div className="pf-drop-tabs" role="tablist" aria-label={t("drop.workspace")}>
      {TABS.map(value => <button key={value} type="button" role="tab" id={`drop-tab-${value}`} aria-selected={tab === value} aria-controls={`drop-panel-${value}`}
        className={tab === value ? "active" : ""} onClick={() => setParam({ tab: value === "network" ? null : value })}>
        {tabLabel(value)}{tabCount(value) > 0 && <span className="pf-drop-count">{tabCount(value)}</span>}
      </button>)}
    </div>
    </div>
    {error && <div className="pf-callout">{error}<button className="pf-link" onClick={() => void refresh()}>{t("personal.retry")}</button></div>}
    {drop.status === "unavailable" && <p className="pf-drop-hint" role="status"><Info size={14} aria-hidden="true" />{t("drop.unavailable")}</p>}
    {demo && <p className="pf-drop-hint" role="status"><Info size={14} aria-hidden="true" />{t("drop.demoEmpty")}</p>}

    {tab === "network" && <section id="drop-panel-network" role="tabpanel" aria-labelledby="drop-tab-network">
      <div className="pf-drop-filterbar" role="group" aria-label={t("personal.filter")}>
        <div className="pf-tabs">
          {(["own", "discovered"] as const).map(value => <button key={value} type="button"
            className={filter === value ? "selected" : ""} aria-pressed={filter === value} onClick={() => { setFilter(value); setParam({ device: null }); }}>
            {value === "own" ? t("drop.filterOwn") : t("drop.filterDiscovered")}
          </button>)}
        </div>
      </div>
      <DeviceOverview devices={filtered} selected={selected} onSelect={id => { setSelectedId(id); setParam({ device: id }); }} onMenu={menu.open}
        onSend={device => void startSend(device, "files")} onMessage={openConversation} onDetails={setDetails}
        dropTargetId={nativeTarget} transfer={activeForSelected} handlers={handlers}
        onDropFiles={onDropFiles} />
    </section>}

    {tab === "conversations" && <section id="drop-panel-conversations" role="tabpanel" aria-labelledby="drop-tab-conversations">
      <DeviceConversation activeId={conversationId} onActiveChange={id => { setParam({ peer: id }); setMobileChat(true); }}
        onMenu={menu.open} onSendFiles={device => void startSend(device, "files")} onDetails={id => setDetails(id)}
        onDropFiles={onDropFiles} dropTargetId={nativeTarget}
        handlers={handlers} mobileChat={mobileChat} onMobileBack={() => setMobileChat(false)} />
    </section>}

    {tab === "transfers" && <section id="drop-panel-transfers" role="tabpanel" aria-labelledby="drop-tab-transfers">
      <TransferList filter={transferFilter} onFilter={setTransferFilter} deviceId={requested || undefined}
        onClearDevice={() => setParam({ device: null })} handlers={handlers} />
    </section>}

    {menu.menu && <DeviceActionsMenu device={menu.menu.device} anchor={menu.menu.anchor as MapAnchor} onAction={onMenuAction} onClose={menu.close} />}
    {send && sendDevice && <DeviceSendSheet device={sendDevice} init={send.init} onClose={() => setSend(null)}
      registerAppend={registerAppend} />}
    {detailsDevice && <ConnectionDetails device={detailsDevice} onClose={() => setDetails(null)} />}
    {pair && <PairDevice onClose={() => setPair(false)} />}
  </div>;
}
