import { ArrowLeftRight, LockKeyhole } from "lucide-react";
import { useTranslation } from "react-i18next";
import { routeKind } from "./DevicesParts";
import { routeLabel, statusLabel, type DropDevice } from "./dropModel";

/** Compact connection context; technical details remain in the details sheet. */
export function DeviceConnection({ self, peer, onDetails }: { self?: DropDevice; peer: DropDevice; onDetails: () => void }) {
  const { t } = useTranslation();
  return <button type="button" className="rd-connchip" onClick={onDetails} aria-label={t("drop.connectionDetails")}>
    <span className="rd-connchip-origin" title={t("drop.localEndpoint")}>{self?.name || t("drop.thisDevice")}</span>
    <ArrowLeftRight size={12} aria-hidden="true" />
    <i className={`rd-sdot ${peer.online === true ? "online" : peer.online === false ? "offline" : "unknown"}`} />
    <span className={`rd-connchip-route rd-k-${routeKind(peer)}`}>{peer.online === false ? statusLabel(t, false) : routeLabel(t, peer.route)}</span>
    {peer.online !== false && <span className="rd-connchip-lock" title={t("drop.encryptedLink")} aria-label={t("drop.encryptedLink")}><LockKeyhole size={11} /></span>}
  </button>;
}
