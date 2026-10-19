import { ArrowDownToLine, ArrowRightLeft } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { isActiveTransfer } from "../../domain/dropClient";
import { useDrop } from "../dropStore";
import { personalHref, usePersonal } from "../model";
import "./drop.css";

/** Persistent entry while the user works elsewhere; all counts use the live store. */
export function DropActivity() {
  const { t } = useTranslation();
  const { demo } = usePersonal();
  const drop = useDrop();
  const active = drop.transfers.filter(isActiveTransfer).length;
  if (drop.status === "disabled") return null;
  return <div className="pf-drop-activity">
    {active > 0 && <Link className="pf-drop-link" to={personalHref("/devices?tab=transfers", demo)}
      aria-label={`${t("drop.tabTransfers")} · ${active}`}><ArrowRightLeft size={17} /><span className="pf-drop-count">{active}</span></Link>}
    {drop.incoming.length > 0 && <button type="button" className="pf-button" aria-label={t("drop.incomingRequestsCount", { count: drop.incoming.length })} onClick={() => drop.setIncomingOpen(true)}>
      <ArrowDownToLine size={16} /><span className="pf-drop-incoming-label">{t("drop.incomingRequests")}</span><span className="pf-drop-count">{drop.incoming.length}</span>
    </button>}
  </div>;
}
