import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Copy, Pencil, Share2 } from "lucide-react";
import { Modal } from "../components";
import { EditDevice, ShareServices } from "../Dialogs";
import { personalHref, usePersonal } from "../model";
import { useDrop } from "../dropStore";
import { formatBytes } from "../../domain/dropClient";
import { relationshipLabel, routeLabel, shortId, statusLabel, type DropDevice } from "./dropModel";

/** Technical details live here only; the rest of the UI uses plain route labels. */
export function ConnectionDetails({ device, onClose }: { device: DropDevice; onClose: () => void }) {
  const { t } = useTranslation();
  const drop = useDrop();
  const { demo } = usePersonal();
  const [edit, setEdit] = useState(false);
  const [share, setShare] = useState(false);
  const [copied, setCopied] = useState(false);
  const caps = drop.capabilities;
  const meta = device.meta;
  const deviceId = meta?.device_peer_id || device.id;
  const relay = caps?.relay;
  if (edit && device.device) return <EditDevice device={device.device} onClose={() => setEdit(false)} />;
  if (share && device.device) return <ShareServices device={device.device} onClose={() => setShare(false)} />;
  const verifyKey = `verify:${device.id}`;
  const autoKey = `auto:${device.id}`;
  return <Modal title={t("drop.connectionDetails")} onClose={onClose} className="pf-drop-details">
    <p className="pf-drop-sheet-sub"><strong>{device.name}</strong> · {statusLabel(t, device.online)}</p>
    <dl className="pf-drop-kv">
      <dt>{t("drop.route")}</dt><dd>{device.self ? t("drop.thisDevice") : routeLabel(t, device.route)}</dd>
      {device.address && <><dt>{t("drop.address")}</dt><dd>{device.address}</dd></>}
      <dt>{t("drop.latencyLabel")}</dt><dd>{device.latency !== null ? t("drop.latency", { ms: Math.round(device.latency) }) : t("drop.unknown")}</dd>
      <dt>{t("drop.relationship")}</dt><dd>{relationshipLabel(t, device)}</dd>
      <dt>{t("drop.deviceId")}</dt>
      <dd className="pf-drop-idline"><code title={deviceId}>{shortId(deviceId)}</code>
        <button type="button" className="pf-icon-button" aria-label={t("drop.copy")} onClick={async () => {
          try { await navigator.clipboard.writeText(deviceId); setCopied(true); } catch { /* Clipboard may be blocked. */ }
        }}><Copy size={14} /></button>{copied && <small role="status">{t("drop.copied")}</small>}</dd>
      {meta?.platform && <><dt>{t("drop.operatingSystem")}</dt><dd>{meta.platform}</dd></>}
      <dt>{t("drop.sizeLimit")}</dt><dd>{caps ? (caps.direct_size_limit === null ? t("drop.noLimit") : formatBytes(caps.direct_size_limit)) : t("drop.unknown")}</dd>
      <dt>{t("drop.natTraversal")}</dt><dd>{caps ? (caps.nat_traversal ? t("drop.available") : t("drop.notAvailable")) : t("drop.unknown")}</dd>
      <dt>{t("drop.relay")}</dt><dd>{relay ? (relay.available ? t("drop.relayAvailable") : t("drop.relayUnavailable", { reason: relay.reason || t("drop.unknown") })) : t("drop.unknown")}</dd>
    </dl>
    {meta && meta.verification.required && !meta.verification.verified && <div className="pf-drop-verify">
      <span>{t("drop.verificationCode")}</span>
      <strong className="pf-drop-code">{meta.verification.code || "—"}</strong>
      <small>{t("drop.verifyHint")}</small>
      <div className="pf-dialog-actions">
        <button type="button" className="pf-button" disabled={drop.isPending(verifyKey)} onClick={() => void drop.run(verifyKey, () => drop.verifyPeer(device.id, false)).catch(() => undefined)}>{t("drop.codesDiffer")}</button>
        <button type="button" className="pf-button primary" disabled={!meta.verification.code || drop.isPending(verifyKey)} onClick={() => void drop.run(verifyKey, () => drop.verifyPeer(device.id, true)).catch(() => undefined)}>{t("drop.codesMatch")}</button>
      </div>
    </div>}
    {meta && !device.self && device.relationship !== "other" && device.relationship !== "external" && <label className="pf-drop-check">
      <input type="checkbox" checked={meta.auto_accept} disabled={drop.isPending(autoKey)}
        onChange={event => { const value = event.target.checked; void drop.run(autoKey, () => drop.setAutoAccept(device.id, value)).catch(() => undefined); }} />
      {t("drop.autoAcceptToggle")}
    </label>}
    <footer className="pf-drop-details-actions">
      {device.device && <button type="button" className="pf-button" onClick={() => setEdit(true)}><Pencil size={15} />{t("drop.editName")}</button>}
      {device.device && device.relationship === "own" && <button type="button" className="pf-button" onClick={() => setShare(true)}><Share2 size={15} />{t("drop.shareServices")}</button>}
      <Link className="pf-link" to={personalHref("/peers", demo)} onClick={onClose}>{t("drop.moreConnectionInfo")}</Link>
    </footer>
  </Modal>;
}
