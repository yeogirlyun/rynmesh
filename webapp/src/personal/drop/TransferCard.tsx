import { useTranslation } from "react-i18next";
import { Archive, Check, ChevronRight, File, Files, Folder, Globe2, Pause, Play, Wifi, Waypoints } from "lucide-react";
import {
  formatBytes, formatProgressBytes, formatSpeed, progressPercent,
  type Route, type Transfer, type TransferAction,
} from "../../domain/dropClient";
import { useDrop } from "../dropStore";
import { formatClock, formatEta, routeLabel, transferActionButtons, transferIconKind, transferStatusText } from "./dropModel";
import { useDropDevices } from "./useDropDevices";

export function TransferIcon({ transfer, size = 19 }: { transfer: Pick<Transfer, "kind" | "name">; size?: number }) {
  const kind = transferIconKind(transfer);
  const Icon = kind === "folder" ? Folder : kind === "files" ? Files : kind === "archive" ? Archive : File;
  return <Icon size={size} strokeWidth={1.5} aria-hidden="true" />;
}

export function RouteChip({ route, muted }: { route: Route | null; muted?: boolean }) {
  const { t } = useTranslation();
  const Icon = route === "lan" ? Wifi : route === "relay" ? Waypoints : Globe2;
  return <span className={`pf-drop-chip${muted ? " muted" : ""}`}><Icon size={12} aria-hidden="true" />{routeLabel(t, route)}</span>;
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
  return <div className="pf-drop-progress" role="progressbar" aria-label={t("drop.progressLabel", { name: transfer.name })}
    aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent * 10) / 10}>
    <span style={{ width: `${percent}%` }} />
  </div>;
}

const showsProgress = (transfer: Transfer) =>
  ["transferring", "paused", "reconnecting", "verifying", "waiting_online", "failed", "verify_failed", "relay_confirmation"].includes(transfer.status)
  || (transfer.status === "completed");

/** Compact strip under the network map. */
export function TransferStrip({ transfer, handlers }: { transfer: Transfer; handlers?: TransferHandlers }) {
  const { t } = useTranslation();
  const { perform, isPending } = useTransferActions(handlers);
  const toggle = transfer.actions.includes("pause") ? "pause" : transfer.actions.includes("resume") ? "resume" : null;
  const right = transfer.status === "transferring"
    ? [formatSpeed(transfer.speed_bps), formatEta(t, transfer.eta_s)].filter(Boolean).join(" · ")
    : transferStatusText(t, transfer);
  return <div className="pf-drop-strip">
    <span className="pf-drop-fileicon"><TransferIcon transfer={transfer} /></span>
    <div className="pf-drop-grow">
      <div className="pf-drop-between"><strong className="pf-drop-filename">{transfer.name}</strong>{transfer.route && <RouteChip route={transfer.route} />}</div>
      <Progress transfer={transfer} />
      <div className="pf-drop-meta pf-drop-num"><span>{formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes)}</span><span>{right}</span></div>
    </div>
    {toggle && <button type="button" className="pf-icon-button" disabled={isPending(transfer, toggle)}
      aria-label={t(`drop.act_${toggle}`)} onClick={() => void perform(transfer, toggle)}>
      {toggle === "pause" ? <Pause size={17} /> : <Play size={17} />}
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
  return <div className="pf-drop-filecard">
    <div className="pf-drop-row">
      <span className="pf-drop-fileicon"><TransferIcon transfer={transfer} /></span>
      <div className="pf-drop-grow"><div className="pf-drop-filename">{transfer.name}</div><small>{target}</small></div>
    </div>
    {showsProgress(transfer) && transfer.status !== "completed" && <Progress transfer={transfer} />}
    <div className="pf-drop-meta pf-drop-num">
      <span>{transfer.status === "transferring" ? formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes) : transferStatusText(t, transfer)}</span>
      {transfer.status === "transferring" && <span>{[formatSpeed(transfer.speed_bps), formatEta(t, transfer.eta_s)].filter(Boolean).join(" · ")}</span>}
    </div>
    {transfer.note && <p className="pf-drop-note-text">{transfer.note}</p>}
    <div className="pf-drop-card-actions">
      {transfer.route ? <button type="button" className="pf-drop-link" onClick={() => handlers?.onDetails?.(transfer.peer_id)}>
        <RouteChip route={transfer.route} muted /><ChevronRight size={14} aria-hidden="true" />
      </button> : <span />}
      <span className="pf-drop-actions-inline">
        {buttons.map(button => <button key={button.action} type="button" className="pf-drop-textbutton"
          disabled={isPending(transfer, button.action)} onClick={() => void perform(transfer, button.action)}>
          {button.action === "pause" && <Pause size={15} aria-hidden="true" />}
          {button.action === "resume" && <Play size={15} aria-hidden="true" />}
          {button.label}
        </button>)}
      </span>
    </div>
  </div>;
}

/** Divider row in the transfers tab. */
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
  const left = transfer.status === "transferring"
    ? `${t(transfer.direction === "out" ? "drop.sendingPrefix" : "drop.receivingPrefix")} · ${formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes)}`
    : done ? [formatClock(transfer.completed_at || transfer.updated_at, i18n.language), transfer.route ? routeLabel(t, transfer.route) : "", transferStatusText(t, transfer)].filter(Boolean).join(" · ")
      : transferStatusText(t, transfer);
  return <article className="pf-drop-transfer">
    <div className="pf-drop-between">
      <div className="pf-drop-row">
        <span className="pf-drop-fileicon"><TransferIcon transfer={transfer} /></span>
        <div className="pf-drop-grow">
          <h3 className="pf-drop-filename">{transfer.name}</h3>
          <small>{t("drop.directionLine", { from, to, size: formatBytes(transfer.total_bytes) })}</small>
        </div>
      </div>
      {done ? <span className="pf-drop-good"><Check size={15} aria-hidden="true" />{transfer.direction === "in" ? t("drop.received") : t("drop.delivered")}</span>
        : transfer.route && <RouteChip route={transfer.route} />}
    </div>
    {!done && showsProgress(transfer) && <Progress transfer={transfer} />}
    <div className={`pf-drop-meta pf-drop-num${warn ? " warn" : ""}`}>
      <span>{left}</span>
      {transfer.status === "transferring" && <span>{[formatSpeed(transfer.speed_bps), formatEta(t, transfer.eta_s)].filter(Boolean).join(" · ")}</span>}
    </div>
    <div className="pf-drop-row-actions">
      {buttons.map(button => <button key={button.action} type="button"
        className={button.primary ? "pf-button" : "pf-drop-textbutton"}
        disabled={isPending(transfer, button.action)} onClick={() => void perform(transfer, button.action)}>
        {button.action === "pause" && <Pause size={15} aria-hidden="true" />}
        {button.action === "resume" && <Play size={15} aria-hidden="true" />}
        {button.label}
      </button>)}
      {handlers?.onOpenConversation && <button type="button" className="pf-drop-textbutton" onClick={() => handlers.onOpenConversation?.(transfer.peer_id)}>{t("drop.openConversation")}</button>}
      {handlers?.onDetails && <button type="button" className="pf-drop-textbutton" onClick={() => handlers.onDetails?.(transfer.peer_id)}>{t("drop.connectionDetails")}</button>}
    </div>
  </article>;
}
