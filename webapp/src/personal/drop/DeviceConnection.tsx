import { ArrowLeftRight, LockKeyhole } from "lucide-react";
import { useTranslation } from "react-i18next";
import { routeLabel, statusLabel, type DropDevice } from "./dropModel";

/** Compact connection context; technical details remain in the details sheet. */
export function DeviceConnection({ self, peer, onDetails }: { self?: DropDevice; peer: DropDevice; onDetails: () => void }) {
  const { t } = useTranslation();
  return <button type="button" className="pf-drop-connection" onClick={onDetails} aria-label={t("drop.connectionDetails")}>
    <span className="pf-drop-connection-origin" title={t("drop.localEndpoint")}>{self?.name || t("drop.thisDevice")}</span>
    <ArrowLeftRight size={12} aria-hidden="true" />
    <span className="pf-drop-connection-route"><i className={`pf-netmap-dot ${peer.online === true ? "online" : peer.online === false ? "offline" : "unknown"}`} />
      {peer.online === false ? statusLabel(t, false) : routeLabel(t, peer.route)}</span>
    <span className="pf-drop-connection-security" title={t("drop.encryptedLink")} aria-label={t("drop.encryptedLink")}><LockKeyhole size={11} /></span>
  </button>;
}
