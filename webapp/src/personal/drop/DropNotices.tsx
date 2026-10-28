import { useTranslation } from "react-i18next";
import { Check, Download, X } from "lucide-react";
import type { Transfer } from "../../domain/dropClient";
import { useDrop, type DropNotice } from "../dropStore";
import { useTransferActions } from "./TransferCard";
import { useDropDevices } from "./useDropDevices";
import "./drop.css";

/** Cards for new requests and finished receives, so they can be handled from any page. */
export function DropNotices() {
  const { t } = useTranslation();
  const drop = useDrop();
  if (!drop.notices.length) return null;
  return <div className="pf-drop-notices" role="region" aria-live="polite" aria-label={t("drop.noticesRegion")}>
    {drop.notices.map(notice => <NoticeCard key={notice.id} notice={notice} />)}
  </div>;
}

function NoticeCard({ notice }: { notice: DropNotice }) {
  const { t } = useTranslation();
  const drop = useDrop();
  const { nameOf } = useDropDevices();
  const { perform, isPending } = useTransferActions();
  const transfers = notice.transferIds.map(id => drop.data.transfers[id]).filter((item): item is Transfer => Boolean(item));
  if (!transfers.length) return null;
  const device = nameOf(notice.peerId);
  const what = transfers.length === 1 ? transfers[0].name : t("drop.itemsCount", { count: transfers.length });
  const close = () => drop.dismissNotice(notice.id);

  if (notice.kind === "incoming") {
    return <div className="pf-drop-notice">
      <span className="pf-drop-mark" aria-hidden="true"><Download size={18} /></span>
      <div className="pf-drop-grow">
        <strong>{t("drop.notifyTitle")}</strong>
        <p className="pf-drop-notice-text">{t("drop.notifyBody", { device, name: what })}</p>
        <div className="pf-drop-actions-inline">
          <button type="button" className="pf-button primary" onClick={() => {
            drop.setIncomingOpen(transfers.length === 1 ? transfers[0].transfer_id : true);
            close();
          }}>{t("drop.review")}</button>
        </div>
      </div>
      <button type="button" className="pf-icon-button" aria-label={t("drop.close")} onClick={close}><X size={16} /></button>
    </div>;
  }

  // Several finished receives from one peer: open/reveal applies to the newest.
  const latest = transfers[transfers.length - 1];
  const canOpen = latest.actions.includes("open");
  const canReveal = latest.actions.includes("reveal");
  return <div className="pf-drop-notice">
    <span className="pf-drop-mark done" aria-hidden="true"><Check size={18} /></span>
    <div className="pf-drop-grow">
      <strong>{t("drop.notifyReceivedTitle")}</strong>
      <p className="pf-drop-notice-text">{t("drop.notifyReceivedBody", { device, name: what })}</p>
      {(canOpen || canReveal) && <div className="pf-drop-actions-inline">
        {canOpen && <button type="button" className="pf-button primary" disabled={isPending(latest, "open")}
          onClick={() => { void perform(latest, "open"); close(); }}>{t(latest.kind === "file" ? "drop.act_open" : "drop.openFolder")}</button>}
        {canReveal && <button type="button" className="pf-button" disabled={isPending(latest, "reveal")}
          onClick={() => { void perform(latest, "reveal"); close(); }}>{t("drop.act_reveal")}</button>}
      </div>}
    </div>
    <button type="button" className="pf-icon-button" aria-label={t("drop.close")} onClick={close}><X size={16} /></button>
  </div>;
}
