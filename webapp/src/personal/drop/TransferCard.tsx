import { useTranslation } from "react-i18next";
import { Archive, ArrowDownLeft, ArrowUpRight, Check, ChevronRight, File, Files, Folder, Globe, Pause, Play, TriangleAlert, Wifi, Waypoints } from "lucide-react";
import {
  formatBytes, formatProgressBytes, formatSpeed, progressPercent,
  type Route, type Transfer, type TransferAction,
} from "../../domain/dropClient";
import { useDrop } from "../dropStore";
import { formatClock, formatEta, routeLabel, transferActionButtons, transferIconKind, transferStatusText } from "./dropModel";
import { FileTile, fileHue } from "./DevicesParts";
import { useDropDevices } from "./useDropDevices";

export function TransferIcon({ transfer, size = 19 }: { transfer: Pick<Transfer, "kind" | "name">; size?: number }) {
  const kind = transferIconKind(transfer);
  const Icon = kind === "folder" ? Folder : kind === "files" ? Files : kind === "archive" ? Archive : File;
  return <Icon size={size} strokeWidth={1.5} aria-hidden="true" />;
}

export function RouteChip({ route, muted }: { route: Route | null; muted?: boolean }) {
  const { t } = useTranslation();
  const Icon = route === "lan" ? Wifi : route === "relay" ? Waypoints : Globe;
  return <span className={`pf-drop-chip${muted ? " muted" : ""}`}><Icon size={12} aria-hidden="true" />{routeLabel(t, route)}</span>;
}

/** The route a transfer takes, as a tinted label (LAN green, public direct blue, relay amber). */
function RouteBadge({ route }: { route: Route | null }) {
  const { t } = useTranslation();
  const Icon = route === "lan" ? Wifi : route === "relay" ? Waypoints : Globe;
  return <span className={`rd-route rd-k-${route === "lan" || route === "public" || route === "relay" ? route : "unknown"}`}><Icon size={13} strokeWidth={2} aria-hidden="true" />{routeLabel(t, route)}</span>;
}

export type TransferHandlers = {
  onResend?: (transfer: Transfer) => void;
  onOpenConversation?: (peerId: string) => void;
  onDetails?: (peerId: string) => void;
};

/** Executes Transfer.actions; pause/resume/cancel/retry are guarded against repeats. */
export function useTransferActions(handlers: TransferHandlers = {}) {
  const drop = useDrop();
  const { t } = useTranslation();
  const perform = async (transfer: Transfer, action: TransferAction) => {
    const key = `${transfer.transfer_id}:${action}`;
    try {
      if (action === "accept") { drop.setIncomingOpen(transfer.transfer_id); return; }
      if (action === "send_again" || action === "reselect") { handlers.onResend?.(transfer); return; }
      if (action === "open" || action === "reveal") {
        try { await drop.run(key, () => drop.openTransfer(transfer, action)); } catch (error) {
          drop.notify("warn", t("drop.openFailed", { message: error instanceof Error ? error.message : String(error) }));
        }
        return;
      }
      await drop.run(key, () => drop.transferAction(transfer, action));
    } catch (error) {
      drop.notify("warn", t("drop.sendFailed", { message: error instanceof Error ? error.message : String(error) }));
    }
  };
  return { perform, isPending: (transfer: Transfer, action: TransferAction) => drop.isPending(`${transfer.transfer_id}:${action}`) };
}

function Progress({ transfer }: { transfer: Transfer }) {
  const { t } = useTranslation();
  const percent = progressPercent(transfer.confirmed_bytes, transfer.total_bytes);
  const muted = transfer.status === "paused" || ["waiting_online", "failed", "verify_failed", "insufficient_space", "reconnecting", "source_changed", "relay_confirmation"].includes(transfer.status);
  return <div className={`rd-prog${muted ? " muted" : ""}`} role="progressbar" aria-label={t("drop.progressLabel", { name: transfer.name })}
    aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent * 10) / 10}>
    <span style={{ width: `${percent}%` }} />
  </div>;
}

const showsProgress = (transfer: Transfer) =>
  ["transferring", "paused", "reconnecting", "verifying", "waiting_online", "failed", "verify_failed", "relay_confirmation"].includes(transfer.status)
  || (transfer.status === "completed");

const speedEta = (t: Parameters<typeof formatEta>[0], transfer: Transfer) => [formatSpeed(transfer.speed_bps), formatEta(t, transfer.eta_s)].filter(Boolean).join(" · ");

/** Compact strip under the device details. */
export function TransferStrip({ transfer, handlers }: { transfer: Transfer; handlers?: TransferHandlers }) {
  const { t } = useTranslation();
  const { perform, isPending } = useTransferActions(handlers);
  const toggle = transfer.actions.includes("pause") ? "pause" : transfer.actions.includes("resume") ? "resume" : null;
  const percent = progressPercent(transfer.confirmed_bytes, transfer.total_bytes);
  const right = transfer.status === "transferring" ? speedEta(t, transfer) : transferStatusText(t, transfer);
  return <div className={`rd-mini rd-h-${fileHue(transfer)}`}>
    <FileTile transfer={transfer} size={36} />
    <div className="rd-mini-main">
      <div className="rd-mini-top"><strong>{transfer.name}</strong><span className="rd-num">{Math.round(percent)}%</span></div>
      <Progress transfer={transfer} />
      <div className="rd-mini-meta rd-num"><span>{formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes)}</span><span>{right}</span></div>
    </div>
    {toggle && <button type="button" className="rd-ib" disabled={isPending(transfer, toggle)}
      aria-label={t(`drop.act_${toggle}`)} onClick={() => void perform(transfer, toggle)}>
      {toggle === "pause" ? <Pause size={16} strokeWidth={1.8} /> : <Play size={16} strokeWidth={1.8} />}
    </button>}
  </div>;
}

/** File message in a conversation; bound to the same store transfer. */
export function TransferCard({ transfer, handlers }: { transfer: Transfer; handlers?: TransferHandlers }) {
  const { t } = useTranslation();
  const { nameOf } = useDropDevices();
  const { perform, isPending } = useTransferActions(handlers);
  const buttons = transferActionButtons(t, transfer).filter(b => b.action !== "cancel" || transfer.status !== "transferring");
  const target = transfer.direction === "out"
    ? `${formatBytes(transfer.total_bytes)} · ${nameOf(transfer.peer_id)}`
    : `${formatBytes(transfer.total_bytes)} · ${t("drop.fromDevice", { name: nameOf(transfer.peer_id) })}`;
  const done = transfer.status === "completed";
  const warn = ["insufficient_space", "source_changed", "verify_failed", "failed", "relay_confirmation", "reconnecting", "waiting_online"].includes(transfer.status);
  return <div className={`rd-fcard rd-h-${fileHue(transfer)}`}>
    <div className="rd-fcard-top">
      <FileTile transfer={transfer} size={40} />
      <div className="rd-fcard-text"><div className="rd-fcard-name">{transfer.name}</div><small>{target}</small></div>
      {done && <span className="rd-doneline"><Check size={14} strokeWidth={2.2} aria-hidden="true" />{transfer.direction === "in" ? t("drop.received") : t("drop.delivered")}</span>}
    </div>
    {showsProgress(transfer) && !done && <Progress transfer={transfer} />}
    <div className={`rd-fcard-meta rd-num${warn ? " warn" : ""}`}>
      <span>{transfer.status === "transferring" ? formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes) : transferStatusText(t, transfer)}</span>
      {transfer.status === "transferring" && <span>{speedEta(t, transfer)}</span>}
    </div>
    {transfer.note && <p className="rd-note-text">{transfer.note}</p>}
    <div className="rd-fcard-foot">
      {transfer.route ? <button type="button" className="rd-routebtn" aria-label={t("drop.connectionDetails")} onClick={() => handlers?.onDetails?.(transfer.peer_id)}>
        <RouteBadge route={transfer.route} /><ChevronRight size={13} strokeWidth={1.8} aria-hidden="true" />
      </button> : <span />}
      <span className="rd-fcard-actions">
        {buttons.map(button => <button key={button.action} type="button" className={`rd-btn sm${button.action === "accept" || button.action === "open" ? " primary" : ""}`}
          disabled={isPending(transfer, button.action)} onClick={() => void perform(transfer, button.action)}>
          {button.action === "pause" && <Pause size={14} aria-hidden="true" />}
          {button.action === "resume" && <Play size={14} aria-hidden="true" />}
          {button.label}
        </button>)}
      </span>
    </div>
  </div>;
}

/** One row of the transfers tab. */
export function TransferRow({ transfer, handlers }: { transfer: Transfer; handlers?: TransferHandlers }) {
  const { t, i18n } = useTranslation();
  const { nameOf, self } = useDropDevices();
  const { perform, isPending } = useTransferActions(handlers);
  const me = self?.name || t("drop.thisDevice");
  const peer = nameOf(transfer.peer_id);
  const [from, to] = transfer.direction === "out" ? [me, peer] : [peer, me];
  const buttons = transferActionButtons(t, transfer);
  const done = transfer.status === "completed";
  const warn = ["insufficient_space", "source_changed", "verify_failed", "failed", "relay_confirmation", "reconnecting", "waiting_online"].includes(transfer.status);
  const out = transfer.direction === "out";
  // A folder or several files say how many they hold, next to the size.
  const size = transfer.kind === "folder" ? `${t("drop.folderCount", { count: transfer.file_count })} · ${formatBytes(transfer.total_bytes)}`
    : transfer.kind === "files" ? t("drop.folderSummary", { count: transfer.file_count, size: formatBytes(transfer.total_bytes) })
      : formatBytes(transfer.total_bytes);
  const left = transfer.status === "transferring"
    ? [`${t(out ? "drop.sendingPrefix" : "drop.receivingPrefix")} · ${formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes)}`, speedEta(t, transfer)].filter(Boolean).join(" · ")
    : done ? [formatClock(transfer.completed_at || transfer.updated_at, i18n.language), transfer.route ? routeLabel(t, transfer.route) : "", transferStatusText(t, transfer)].filter(Boolean).join(" · ")
      : transferStatusText(t, transfer);
  const percent = Math.round(progressPercent(transfer.confirmed_bytes, transfer.total_bytes));
  return <article className={`rd-tr rd-h-${fileHue(transfer)}`} aria-label={transfer.name}>
    <div className="rd-tr-top">
      <FileTile transfer={transfer} size={42}
        badge={<span className={`rd-dir ${out ? "out" : "in"}`} aria-hidden="true">{out ? <ArrowUpRight size={10} strokeWidth={3} /> : <ArrowDownLeft size={10} strokeWidth={3} />}</span>} />
      <div className="rd-tr-text">
        <h3>{transfer.name}</h3>
        <small>{t("drop.directionLine", { from, to, size })}</small>
      </div>
      {done ? <span className="rd-doneline pill"><Check size={14} strokeWidth={2.2} aria-hidden="true" />{transfer.direction === "in" ? t("drop.received") : t("drop.delivered")}</span>
        : transfer.route && <RouteBadge route={transfer.route} />}
    </div>
    {!done && showsProgress(transfer) && <div className="rd-tr-prog">
      <Progress transfer={transfer} /><span className="rd-pct rd-num">{percent}%</span>
    </div>}
    <div className="rd-tr-foot">
      <span className={`rd-tr-status rd-num${warn ? " warn" : done ? " quiet" : ""}`}>{warn && <TriangleAlert size={14} strokeWidth={1.9} aria-hidden="true" />}{left}</span>
      <span className="rd-tr-actions">
        {buttons.map(button => <button key={button.action} type="button"
          className={button.action === "accept" ? "rd-btn sm primary" : button.primary ? "rd-btn sm" : "rd-tb"}
          disabled={isPending(transfer, button.action)} onClick={() => void perform(transfer, button.action)}>
          {button.action === "pause" && <Pause size={14} aria-hidden="true" />}
          {button.action === "resume" && <Play size={14} aria-hidden="true" />}
          {button.label}
        </button>)}
        {handlers?.onOpenConversation && <button type="button" className="rd-tb" onClick={() => handlers.onOpenConversation?.(transfer.peer_id)}>{t("drop.openConversation")}</button>}
        {handlers?.onDetails && <button type="button" className="rd-tb" onClick={() => handlers.onDetails?.(transfer.peer_id)}>{t("drop.connectionDetails")}</button>}
      </span>
    </div>
  </article>;
}
