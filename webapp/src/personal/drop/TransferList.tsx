import { useTranslation } from "react-i18next";
import { X } from "lucide-react";
import { useDrop } from "../dropStore";
import { isActiveTransfer, isDoneTransfer } from "../../domain/dropClient";
import { TransferRow, type TransferHandlers } from "./TransferCard";
import { useDropDevices } from "./useDropDevices";

export type TransferFilter = "all" | "active" | "done";

export function TransferList({ filter, onFilter, deviceId, onClearDevice, handlers }: {
  filter: TransferFilter;
  onFilter: (filter: TransferFilter) => void;
  deviceId?: string;
  onClearDevice: () => void;
  handlers: TransferHandlers;
}) {
  const { t } = useTranslation();
  const { transfers, status } = useDrop();
  const { nameOf } = useDropDevices();
  const visible = transfers.filter(transfer => (!deviceId || transfer.peer_id === deviceId)
    && (filter === "all" || (filter === "active" ? isActiveTransfer(transfer) : isDoneTransfer(transfer))));
  const filters: [TransferFilter, string][] = [["all", t("drop.filterAll")], ["active", t("drop.filterActive")], ["done", t("drop.filterDone")]];
  return <div className="pf-drop-transfers">
    <div className="pf-drop-filterbar" role="group" aria-label={t("drop.transferFilters")}>
      {filters.map(([id, label]) => <button key={id} type="button" className={`pf-drop-filter${filter === id ? " active" : ""}`}
        aria-pressed={filter === id} onClick={() => onFilter(id)}>{label}</button>)}
      {deviceId && <span className="pf-drop-filterchip">{t("drop.deviceFilter", { name: nameOf(deviceId) })}
        <button type="button" aria-label={t("drop.clearFilter")} onClick={onClearDevice}><X size={13} /></button></span>}
      <span className="pf-drop-visibility">{t("drop.recordsLocal")}</span>
    </div>
    <div className="pf-drop-transferlist">
      {visible.map(transfer => <TransferRow key={transfer.transfer_id} transfer={transfer} handlers={handlers} />)}
    </div>
    {!visible.length && <div className="pf-drop-empty">
      <h3>{status === "disabled" ? t("drop.noTransfers") : status === "loading" ? t("drop.loading") : t("drop.noTransfers")}</h3>
      <p>{status === "disabled" ? t("drop.demoEmpty") : status === "unavailable" ? t("drop.unavailable") : t("drop.noTransfersHint")}</p>
    </div>}
  </div>;
}
