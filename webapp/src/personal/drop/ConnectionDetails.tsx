import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, Copy, Pencil, Share2 } from "lucide-react";
import { Modal } from "../components";
import { EditDevice, ShareServices } from "../Dialogs";
import { personalHref, usePersonal } from "../model";
import { useDrop } from "../dropStore";
import { formatBytes } from "../../domain/dropClient";
import { relationshipLabel, routeLabel, shortId, statusLabel, type DropDevice } from "./dropModel";
import { DeviceLine, DeviceGlyph, Tile, deviceHue } from "./FamilyParts";
import { CodeDigits, RouteBadge, StatePill } from "./DropFamilyParts";

/** Technical details live here only; the rest of the UI uses plain route labels. */
export function ConnectionDetails({ device, onClose }: { device: DropDevice; onClose: () => void }) {
  const { t } = useTranslation();
  const drop = useDrop();
  const { demo, devices } = usePersonal();
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
  const hue = deviceHue(device.id, { self: device.self, online: device.online });
  const self = devices?.find((item) => item.self);
  const needsVerify = Boolean(meta && meta.verification.required && !meta.verification.verified);
  const canAuto = Boolean(meta && !device.self && device.relationship !== "other" && device.relationship !== "external");
  const dot = (on: boolean | undefined) => <i className={`rf-dot ${on ? "on" : "off"}`} aria-hidden="true" />;
  return <Modal title={t("drop.connectionDetails")} onClose={onClose} className={`rf rf-dlg rf-dlg-conn rf-dlg-hue rf-h-${hue}`}>
    <Tile hue={hue} size="md" off={device.online === false && !device.self} className="rf-mark">{(px) => <DeviceGlyph kind={device.kind} size={px} />}</Tile>
    <div className="rf-sub rf-sub-row"><b>{device.name}</b><StatePill online={device.online} label={statusLabel(t, device.online)} /></div>
    <div className="rf-body">
      <div className="rf-strip">
        <div className="rf-strip-end rf-h-indigo"><span className="rf-chipicon"><DeviceLine kind={self?.kind ?? "laptop"} /></span>
          <div><b>{self?.name ?? t("drop.thisDevice")}</b>{self ? <small>{t("drop.thisDevice")}</small> : null}</div></div>
        <div className="rf-strip-line">
          <span className={`rf-strip-rule rf-t-${device.online === false ? "off" : "ok"}`} aria-hidden="true" />
          {device.self ? <span className="rf-route rf-t-self">{t("drop.thisDevice")}</span>
            : device.route || device.online === false ? <RouteBadge route={device.route} latency={device.latency} online={device.online} /> : <span className="rf-route rf-t-off">{t("drop.unknown")}</span>}
        </div>
        <div className={`rf-strip-end rf-h-${hue}`}><span className="rf-chipicon"><DeviceLine kind={device.kind} /></span>
          <div><b>{device.name}</b><small>{relationshipLabel(t, device)}</small></div></div>
      </div>

      <section className="rf-sec" aria-labelledby="conn-h">
        <h3 id="conn-h" className="rf-eyebrow">{t("drop.connectionSection")}</h3>
        <dl className="rf-grid">
          <div><dt>{t("drop.route")}</dt><dd>{device.self ? t("drop.thisDevice") : routeLabel(t, device.route)}</dd></div>
          <div><dt>{t("drop.latencyLabel")}</dt><dd>{device.latency !== null ? t("drop.latency", { ms: Math.round(device.latency) }) : t("drop.unknown")}</dd></div>
          {device.address && <div><dt>{t("drop.address")}</dt><dd className="rf-mono rf-wrap">{device.address}</dd></div>}
          <div><dt>{t("drop.sizeLimit")}</dt><dd>{caps ? (caps.direct_size_limit === null ? t("drop.noLimit") : formatBytes(caps.direct_size_limit)) : t("drop.unknown")}</dd></div>
          <div><dt>{t("drop.natTraversal")}</dt><dd>{caps ? <>{dot(caps.nat_traversal)}{caps.nat_traversal ? t("drop.available") : t("drop.notAvailable")}</> : t("drop.unknown")}</dd></div>
          <div><dt>{t("drop.relay")}</dt><dd>{relay ? <>{dot(relay.available)}{relay.available ? t("drop.relayAvailable") : t("drop.relayUnavailable", { reason: relay.reason || t("drop.unknown") })}</> : t("drop.unknown")}</dd></div>
        </dl>
      </section>

      <section className="rf-sec" aria-labelledby="dev-h">
        <h3 id="dev-h" className="rf-eyebrow">{t("drop.deviceSection")}</h3>
        <dl className="rf-list rf-rows">
          <div className="rf-dl-row"><dt>{t("drop.relationship")}</dt><dd>{relationshipLabel(t, device)}</dd></div>
          <div className="rf-dl-row"><dt>{t("drop.deviceId")}</dt>
            <dd className="rf-idline"><code className="rf-code-chip" title={deviceId}>{shortId(deviceId)}</code>
              <button type="button" className="rf-ib rf-ib-sm" aria-label={t("drop.copy")} onClick={async () => {
                try { await navigator.clipboard.writeText(deviceId); setCopied(true); } catch { /* Clipboard may be blocked. */ }
              }}><Copy size={13} aria-hidden="true" /></button>{copied && <small role="status" className="rf-copied">{t("drop.copied")}</small>}</dd></div>
          {meta?.platform && <div className="rf-dl-row"><dt>{t("drop.operatingSystem")}</dt><dd>{meta.platform}</dd></div>}
        </dl>
      </section>

      {needsVerify && meta && <div className="rf-verify rf-t-warn">
        <span className="rf-verify-cap">{t("drop.verificationCode")}</span>
        <CodeDigits code={meta.verification.code || ""} label={t("drop.verificationCode")} />
        <small>{t("drop.verifyHint")}</small>
        <div className="rf-row-actions">
          <button type="button" className="rf-btn" disabled={drop.isPending(verifyKey)} onClick={() => void drop.run(verifyKey, () => drop.verifyPeer(device.id, false)).catch(() => undefined)}>{t("drop.codesDiffer")}</button>
          <button type="button" className="rf-btn rf-btn-p" disabled={!meta.verification.code || drop.isPending(verifyKey)} onClick={() => void drop.run(verifyKey, () => drop.verifyPeer(device.id, true)).catch(() => undefined)}>{t("drop.codesMatch")}</button>
        </div>
      </div>}

      {canAuto && meta && <label className="rf-switch-row">
        <span className="rf-opt-stack"><b>{t("drop.autoAcceptToggle")}</b><small>{meta.auto_accept ? t("drop.autoAcceptOn", { name: device.name }) : t("drop.autoAcceptOff")}</small></span>
        <input type="checkbox" role="switch" className="rf-sw" checked={meta.auto_accept} disabled={drop.isPending(autoKey)}
          onChange={event => { const value = event.target.checked; void drop.run(autoKey, () => drop.setAutoAccept(device.id, value)).catch(() => undefined); }} />
      </label>}
    </div>
    <footer className="rf-foot rf-foot-links">
      {device.device && <button type="button" className="rf-btn rf-btn-sm" onClick={() => setEdit(true)}><Pencil size={15} aria-hidden="true" />{t("drop.editName")}</button>}
      {device.device && device.relationship === "own" && <button type="button" className="rf-btn rf-btn-sm" onClick={() => setShare(true)}><Share2 size={15} aria-hidden="true" />{t("drop.shareServices")}</button>}
      <Link className="rf-lnk rf-lnk-end" to={personalHref("/peers", demo)} onClick={onClose}>{t("drop.moreConnectionInfo")}<ArrowRight size={14} aria-hidden="true" /></Link>
    </footer>
  </Modal>;
}
