import { useTranslation } from "react-i18next";
import { ArrowRightLeft, Lock, X } from "lucide-react";
import { useDrop } from "../dropStore";
import { isActiveTransfer, isDoneTransfer, type Transfer } from "../../domain/dropClient";
import { TransferRow, type TransferHandlers } from "./TransferCard";
import { EmptyState, Seg } from "./DevicesParts";
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
  const scoped = transfers.filter(transfer => !deviceId || transfer.peer_id === deviceId);
  const active = scoped.filter(isActiveTransfer);
  const done = scoped.filter(isDoneTransfer);
  // Declined, cancelled and failed transfers belong to neither of the two filters but still show under All.
  const stopped = scoped.filter(transfer => !isActiveTransfer(transfer) && !isDoneTransfer(transfer));
  const groups: [string, Transfer[]][] = [];
  if (filter !== "done") groups.push([t("drop.filterActive"), active]);
  if (filter !== "active") groups.push([t("drop.filterDone"), done]);
  if (filter === "all") groups.push([t("drop.groupStopped"), stopped]);
  const shown = groups.filter(([, rows]) => rows.length);
  return <div className="rd-transfers">
    <div className="rd-bar">
      <div className="rd-bar-left">
        <Seg label={t("drop.transferFilters")} value={filter} onChange={onFilter}
          options={[{ value: "all", label: t("drop.filterAll"), count: scoped.length }, { value: "active", label: t("drop.filterActive"), count: active.length }, { value: "done", label: t("drop.filterDone"), count: done.length }]} />
        {deviceId && <span className="rd-filterchip">{t("drop.deviceFilter", { name: nameOf(deviceId) })}
          <button type="button" aria-label={t("drop.clearFilter")} onClick={onClearDevice}><X size={13} strokeWidth={2} /></button></span>}
      </div>
      <span className="rd-visibility"><Lock size={13} strokeWidth={1.8} aria-hidden="true" />{t("drop.recordsLocal")}</span>
    </div>
    {shown.length > 0 && <div className="rd-tlist">
      {shown.map(([title, rows]) => <section key={title} aria-label={title}>
        <div className="rd-group"><span>{title}</span><span>{rows.length}</span></div>
        {rows.map(transfer => <TransferRow key={transfer.transfer_id} transfer={transfer} handlers={handlers} />)}
      </section>)}
    </div>}
    {!shown.length && <div className="rd-tlist">
      <EmptyState icon={<ArrowRightLeft size={24} strokeWidth={1.7} />}
        title={status === "loading" ? t("drop.loading") : t("drop.noTransfers")}>
        {status === "disabled" ? t("drop.demoEmpty") : status === "unavailable" ? t("drop.unavailable") : t("drop.noTransfersHint")}
      </EmptyState>
    </div>}
  </div>;
}
