import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download } from "lucide-react";
import { Modal } from "../components";
import { useDrop } from "../dropStore";
import { canPickPaths, chooseDropDir } from "../../domain/dropDesktop";
import { formatBytes, type Transfer } from "../../domain/dropClient";
import { TransferIcon } from "./TransferCard";
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
    return <Modal title={t("drop.incomingRequests")} onClose={close}>
      <p className="pf-muted">{t("drop.noIncoming")}</p>
      <footer className="pf-dialog-actions"><button type="button" className="pf-button" onClick={close}>{t("drop.close")}</button></footer>
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
  return <Modal title={title} onClose={close} className="pf-drop-incoming">
    <span className="pf-drop-mark" aria-hidden="true"><Download size={22} /></span>
    <p className="pf-drop-sheet-sub">{[name, device ? relationshipLabel(t, device) : "", transfer.route ? routeLabel(t, transfer.route) : ""].filter(Boolean).join(" · ")}</p>
    <div className="pf-drop-fileline">
      <span className="pf-drop-fileicon"><TransferIcon transfer={transfer} /></span>
      <div className="pf-drop-grow">
        <div className="pf-drop-filename">{transfer.name}</div>
        <small className="pf-drop-num">{t("drop.incomingSummary", { size: formatBytes(transfer.total_bytes), count: transfer.file_count })}</small>
      </div>
    </div>
    {transfer.note && <p className="pf-drop-note-text"><strong>{t("drop.incomingNote")}</strong> {transfer.note}</p>}
    <label className="pf-drop-field">
      <span>{t("drop.saveTo")}</span>
      <span className="pf-drop-savedir">
        <input value={saveDir} readOnly={canPickPaths()} onChange={event => setSaveDir(event.target.value)} />
        {canPickPaths() && <button type="button" className="pf-button" onClick={async () => {
          const dir = await chooseDropDir().catch(() => null);
          if (dir) setSaveDir(dir);
        }}>{t("drop.chooseFolder")}</button>}
      </span>
    </label>
    <small className="pf-drop-num">{[free !== null && free !== undefined ? t("drop.freeSpace", { size: formatBytes(free) }) : "", t("drop.autoVerify")].filter(Boolean).join(" · ")}</small>
    {transfer.status === "insufficient_space" && transfer.error?.need_bytes !== undefined && transfer.error?.free_bytes !== undefined &&
      <p className="pf-error" role="alert">{t("drop.st_insufficient_space", { need: formatBytes(transfer.error.need_bytes), free: formatBytes(transfer.error.free_bytes) })}</p>}
    {needsCode && <div className="pf-drop-verify">
      <span>{t("drop.verifyRequired")}</span>
      <strong className="pf-drop-code">{transfer.verification_code}</strong>
      <label className="pf-drop-check"><input type="checkbox" checked={verified} onChange={event => setVerified(event.target.checked)} />{t("drop.verifyConfirm")}</label>
    </div>}
    <label className="pf-drop-check">
      <input type="checkbox" checked={autoAccept} onChange={event => setAutoAccept(event.target.checked)} />
      <span>{t("drop.autoAccept")}<small>{t("drop.autoAcceptHint")}</small></span>
    </label>
    {error && <p className="pf-error" role="alert">{error}</p>}
    <footer className="pf-dialog-actions">
      {remaining > 0 && <small className="pf-drop-grow">{t("drop.queueMore", { count: remaining })}</small>}
      {transfer.actions.includes("reject") && <button type="button" className="pf-button" disabled={drop.isPending(rejectKey)} onClick={() => void reject()}>{t("drop.reject")}</button>}
      <button type="button" className="pf-button primary" disabled={!transfer.actions.includes("accept") || drop.isPending(acceptKey) || (needsCode && !verified)}
        onClick={() => void accept()}><Download size={16} aria-hidden="true" />{t("drop.accept")}</button>
    </footer>
  </Modal>;
}
