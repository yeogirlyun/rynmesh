import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, Folder, MessageSquare, ShieldAlert } from "lucide-react";
import { Modal } from "../components";
import { useDrop } from "../dropStore";
import { canPickPaths, chooseDropDir } from "../../domain/dropDesktop";
import { formatBytes, type Transfer } from "../../domain/dropClient";
import { DeviceGlyph, FileTile, Tile, deviceHue, fileKindOf } from "./FamilyParts";
import { CodeDigits } from "./DropFamilyParts";
import { relationshipLabel, routeLabel } from "./dropModel";
import { useDropDevices } from "./useDropDevices";

export function IncomingTransferDialog() {
  const { t } = useTranslation();
  const drop = useDrop();
  const { byId, nameOf } = useDropDevices();
  const open = drop.incomingOpen;
  const targeted = typeof open === "string" ? drop.data.transfers[open] : undefined;
  const transfer: Transfer | undefined = targeted ?? drop.incoming[0];
  const [saveDir, setSaveDir] = useState("");
  const [defaultDir, setDefaultDir] = useState("");
  const [autoAccept, setAutoAccept] = useState(false);
  const [verified, setVerified] = useState(false);
  const [error, setError] = useState("");
  const transferId = transfer?.transfer_id;

  useEffect(() => {
    if (!transferId) return;
    setAutoAccept(false);
    setVerified(false);
    setError("");
    const fallback = drop.capabilities?.receive_dir || "";
    setDefaultDir(fallback);
    setSaveDir(transfer?.save_dir || fallback);
    let cancelled = false;
    drop.loadPrefs().then(prefs => {
      if (cancelled || !prefs.receive_dir) return;
      setDefaultDir(prefs.receive_dir);
      setSaveDir(current => (current && current !== fallback ? current : transfer?.save_dir || prefs.receive_dir));
    }).catch(() => undefined);
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transferId]);

  if (!open) return null;
  const close = () => drop.setIncomingOpen(false);
  if (!transfer) {
    return <Modal title={t("drop.incomingRequests")} onClose={close} className="rf rf-dlg rf-dlg-incoming">
      <span className="rf-mark"><Download size={21} strokeWidth={1.8} aria-hidden="true" /></span>
      <p className="rf-sub">{t("drop.noIncoming")}</p>
      <footer className="rf-foot rf-foot-end"><button type="button" className="rf-btn" onClick={close}>{t("drop.close")}</button></footer>
    </Modal>;
  }
  const device = byId.get(transfer.peer_id);
  const name = nameOf(transfer.peer_id);
  const title = transfer.kind === "folder" ? t("drop.incomingTitle_folder", { device: name })
    : transfer.kind === "files" ? t("drop.incomingTitle_files", { device: name, count: transfer.file_count })
      : t("drop.incomingTitle_file", { device: name });
  const needsCode = Boolean(transfer.verification_code);
  const free = saveDir === (drop.capabilities?.receive_dir || defaultDir) ? drop.capabilities?.receive_free_bytes : null;
  const remaining = drop.incoming.filter(item => item.transfer_id !== transfer.transfer_id).length;
  const acceptKey = `${transfer.transfer_id}:accept`;
  const rejectKey = `${transfer.transfer_id}:reject`;
  const after = () => { if (typeof open === "string" || remaining === 0) close(); };
  const accept = async () => {
    setError("");
    try {
      await drop.run(acceptKey, () => drop.acceptTransfer(transfer, { save_dir: saveDir || undefined, auto_accept: autoAccept }));
      after();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const reject = async () => {
    setError("");
    try {
      await drop.run(rejectKey, () => drop.transferAction(transfer, "reject"));
      after();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const hue = needsCode || device?.relationship === "other" ? "amber" : deviceHue(transfer.peer_id, { self: false, online: device?.online });
  return <Modal title={title} onClose={close} className={`rf rf-dlg rf-dlg-incoming rf-dlg-hue rf-h-${hue}`}>
    <Tile hue={hue} size="lg" className="rf-mark">{(px) => <><DeviceGlyph kind={device?.kind ?? "desktop"} size={px} /><span className="rf-mark-badge"><Download size={12} strokeWidth={2.4} aria-hidden="true" /></span></>}</Tile>
    <p className="rf-sub">{[name, device ? relationshipLabel(t, device) : "", transfer.route ? routeLabel(t, transfer.route) : ""].filter(Boolean).join(" · ")}</p>
    <div className="rf-body">
      <div className="rf-list rf-fileblock">
        <div className="rf-file">
          <FileTile kind={fileKindOf(transfer.name, transfer.kind)} />
          <div className="rf-file-text">
            <b className="rf-ell">{transfer.name}</b>
            <small className="pf-drop-num">{t("drop.incomingSummary", { size: formatBytes(transfer.total_bytes), count: transfer.file_count })}</small>
          </div>
        </div>
        {transfer.note && <blockquote className="rf-quote"><MessageSquare size={15} strokeWidth={1.7} aria-hidden="true" /><span><b>{t("drop.incomingNote")}</b> · {transfer.note}</span></blockquote>}
      </div>
      <label className="rf-field">
        <span>{t("drop.saveTo")}</span>
        <span className="rf-savedir">
          <span className="rf-inp rf-pathbox"><Folder size={15} strokeWidth={1.7} aria-hidden="true" />
            <input className="rf-mono" value={saveDir} readOnly={canPickPaths()} onChange={event => setSaveDir(event.target.value)} /></span>
          {canPickPaths() && <button type="button" className="rf-btn" onClick={async () => {
            const dir = await chooseDropDir().catch(() => null);
            if (dir) setSaveDir(dir);
          }}>{t("drop.chooseFolder")}</button>}
        </span>
        <small className="rf-hint pf-drop-num">{[free !== null && free !== undefined ? t("drop.freeSpace", { size: formatBytes(free) }) : "", t("drop.autoVerify")].filter(Boolean).join(" · ")}</small>
      </label>
      {transfer.status === "insufficient_space" && transfer.error?.need_bytes !== undefined && transfer.error?.free_bytes !== undefined &&
        <p className="rf-callout rf-callout-err" role="alert"><ShieldAlert size={16} aria-hidden="true" /><span>{t("drop.st_insufficient_space", { need: formatBytes(transfer.error.need_bytes), free: formatBytes(transfer.error.free_bytes) })}</span></p>}
      {needsCode && <div className="rf-verify rf-t-warn">
        <p className="rf-verify-lead"><ShieldAlert size={16} strokeWidth={1.7} aria-hidden="true" /><span>{t("drop.verifyRequired")}</span></p>
        <CodeDigits code={transfer.verification_code || ""} label={t("drop.verificationCode")} />
        <label className="rf-check rf-check-card"><input type="checkbox" checked={verified} onChange={event => setVerified(event.target.checked)} />{t("drop.verifyConfirm")}</label>
      </div>}
      <label className="rf-check rf-check-top">
        <input type="checkbox" checked={autoAccept} onChange={event => setAutoAccept(event.target.checked)} />
        <span className="rf-opt-stack"><b>{t("drop.autoAccept")}</b><small>{t("drop.autoAcceptHint")}</small></span>
      </label>
      {error && <p className="rf-callout rf-callout-err" role="alert"><ShieldAlert size={16} aria-hidden="true" /><span>{error}</span></p>}
    </div>
    <footer className="rf-foot">
      {remaining > 0 ? <small className="rf-foot-note">{t("drop.queueMore", { count: remaining })}</small> : <span className="rf-foot-note" />}
      {transfer.actions.includes("reject") && <button type="button" className="rf-btn" disabled={drop.isPending(rejectKey)} onClick={() => void reject()}>{t("drop.reject")}</button>}
      <button type="button" className="rf-btn rf-btn-p" disabled={!transfer.actions.includes("accept") || drop.isPending(acceptKey) || (needsCode && !verified)}
        onClick={() => void accept()}><Download size={15} strokeWidth={1.9} aria-hidden="true" />{t("drop.accept")}</button>
    </footer>
  </Modal>;
}
