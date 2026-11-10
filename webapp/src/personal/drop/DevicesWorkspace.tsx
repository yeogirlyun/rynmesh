import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CircleAlert, Info, Lock, Plus, RefreshCw, Search } from "lucide-react";
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
import { Callout, DevicesBand, Seg } from "./DevicesParts";
import "./drop.css";

type Tab = "network" | "conversations" | "transfers";
const TABS: Tab[] = ["network", "conversations", "transfers"];

export function DevicesWorkspace() {
  const { t } = useTranslation();
  const { error, refresh, demo, loading } = usePersonal();
  const drop = useDrop();
  const { list, byId } = useDropDevices();
  const [params, setParams] = useSearchParams();
  const tab: Tab = TABS.includes(params.get("tab") as Tab) ? (params.get("tab") as Tab) : "network";
  const requested = params.get("device") || "";
  const [filter, setFilter] = useState<"own" | "discovered">("own");
  const [query, setQuery] = useState("");
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

  // Self first, then reachable devices, offline ones last; the sort is stable so equal devices keep their order.
  const ordered = useMemo(() => {
    const rank = (device: DropDevice) => device.self ? 0 : device.online === true ? 1 : device.online === null ? 2 : 3;
    return [...list].sort((a, b) => rank(a) - rank(b));
  }, [list]);
  const ownCount = ordered.filter(device => device.relationship === "own").length;
  const q = query.trim().toLowerCase();
  const filtered = ordered.filter(device => (filter === "own" ? device.relationship === "own" : device.relationship !== "own") && (!q || device.name.toLowerCase().includes(q)));
  // With nothing chosen, open the first other device: this device has no actions to offer.
  const selected = filtered.find(device => device.id === selectedId) || filtered.find(device => !device.self) || filtered[0];
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

  return <div className="pf-page pf-drop-page rd-devices">
    <PageHeading title={t("nav.devices")} description={tab === "conversations" ? t("drop.deviceConversationHint") : t("drop.deviceWorkspaceSubtitle")}>
      <div className="rd-head-actions">
        <Link className="rd-btn" to={personalHref("/devices/sync", demo)}><RefreshCw size={15} strokeWidth={1.6} aria-hidden="true" />{t("drop.deviceSync")}</Link>
        <button type="button" className="rd-btn primary" onClick={() => setPair(true)}><Plus size={15} strokeWidth={1.8} aria-hidden="true" />{t("personal.addDevice")}</button>
      </div>
    </PageHeading>
    <div className="rd-tabs" role="tablist" aria-label={t("drop.workspace")}>
      {TABS.map(value => <button key={value} type="button" role="tab" id={`drop-tab-${value}`} aria-selected={tab === value} aria-controls={`drop-panel-${value}`}
        className={tab === value ? "active" : ""} onClick={() => setParam({ tab: value === "network" ? null : value })}>
        {tabLabel(value)}{tabCount(value) > 0 && <span className={`rd-count${value === "conversations" ? " hot" : ""}`}>{tabCount(value)}</span>}
      </button>)}
    </div>
    {error && <Callout tone="err" role="alert" icon={<CircleAlert size={15} strokeWidth={1.8} />}
      action={<button type="button" className="rd-link" onClick={() => void refresh()}>{t("personal.retry")}</button>}>{error}</Callout>}
    {drop.status === "unavailable" && <Callout tone="warn" role="status" icon={<Info size={15} strokeWidth={1.7} />}>{t("drop.unavailable")}</Callout>}
    {demo && <Callout role="status" icon={<Info size={15} strokeWidth={1.7} />}>{t("drop.demoEmpty")}</Callout>}

    {tab === "network" && <section id="drop-panel-network" role="tabpanel" aria-labelledby="drop-tab-network" className="rd-panel">
      {list.length > 0 && <DevicesBand devices={list} activeCount={activeCount} unread={unreadTotal} />}
      <div className="rd-bar">
        <Seg label={t("personal.filter")} value={filter} onChange={value => { setFilter(value); setParam({ device: null }); }}
          options={[{ value: "own", label: t("drop.filterOwn"), count: ownCount }, { value: "discovered", label: t("drop.filterDiscovered"), count: ordered.length - ownCount }]} />
        <label className="rd-field">
          <Search size={15} strokeWidth={1.6} aria-hidden="true" />
          <input aria-label={t("drop.searchDevices")} placeholder={t("drop.searchDevices")} value={query} onChange={event => setQuery(event.target.value)} />
        </label>
      </div>
      <DeviceOverview devices={filtered} selected={selected} onSelect={id => { setSelectedId(id); setParam({ device: id }); }} onMenu={menu.open}
        onSend={device => void startSend(device, "files")} onMessage={openConversation} onDetails={setDetails}
        dropTargetId={nativeTarget} transfer={activeForSelected} handlers={handlers}
        onDropFiles={onDropFiles} onAdd={() => setPair(true)} loading={loading} searching={q.length > 0} />
      <div className="rd-foot">
        <span><Lock size={13} strokeWidth={1.8} aria-hidden="true" />{t("drop.e2eNote")}</span>
        <span>{t("drop.recordsLocal")}</span>
      </div>
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
