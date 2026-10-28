import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Clock, Info, LockKeyhole, Plus, Send, TriangleAlert, X } from "lucide-react";
import { Modal } from "../components";
import { useDrop } from "../dropStore";
import { baseName, canPickPaths, chooseDropPaths } from "../../domain/dropDesktop";
import {
  bytesValue, dropApi, formatBytes, newClientId, sumBytes,
  type Preflight,
} from "../../domain/dropClient";
import { filesFromDataTransfer, pickBrowserFiles, type PickedFile } from "./fileCollect";
import { DeviceTile, FileTile, deviceHue, fileKindOf } from "./FamilyParts";
import { CodeDigits, RouteBadge } from "./DropFamilyParts";
import { reasonLabel, relationshipLabel, sendButtonLabel, type DropDevice } from "./dropModel";

export type SendItem = {
  key: string;
  name: string;
  kind: "file" | "folder" | "unknown";
  path?: string;
  files?: PickedFile[];
  size: bigint | null;
  count: number | null;
};

let itemSeq = 0;
const nextKey = () => `item-${++itemSeq}`;

export function itemsFromPaths(paths: string[]): SendItem[] {
  return paths.map(path => ({ key: nextKey(), name: baseName(path), kind: "unknown", path, size: null, count: null }));
}

/** Groups browser files: entries under a common first folder become one folder item. */
export function itemsFromFiles(files: PickedFile[]): SendItem[] {
  const folders = new Map<string, PickedFile[]>();
  const out: SendItem[] = [];
  for (const picked of files) {
    const slash = picked.rel.indexOf("/");
    if (slash > 0) {
      const dir = picked.rel.slice(0, slash);
      if (!folders.has(dir)) folders.set(dir, []);
      folders.get(dir)!.push(picked);
    } else {
      out.push({ key: nextKey(), name: picked.file.name, kind: "file", files: [picked], size: BigInt(picked.file.size), count: 1 });
    }
  }
  for (const [dir, list] of folders) {
    out.push({ key: nextKey(), name: dir, kind: "folder", files: list, size: sumBytes(list.map(f => f.file.size)), count: list.length });
  }
  return out;
}

export const isBlocking = (preflight: Preflight | null) =>
  Boolean(preflight?.issues?.some(issue => !issue.code.startsWith("warn_")));

export type SendInit = { kind: "files" | "folder"; paths?: string[]; files?: PickedFile[] };

export function DeviceSendSheet({ device, init, onClose, registerAppend }: {
  device: DropDevice;
  init: SendInit;
  onClose: () => void;
  registerAppend?: (append: ((paths: string[]) => void) | null) => void;
}) {
  const { t } = useTranslation();
  const drop = useDrop();
  const desktop = canPickPaths();
  const [items, setItems] = useState<SendItem[]>(() => init.paths ? itemsFromPaths(init.paths) : itemsFromFiles(init.files || []));
  const [note, setNote] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [checking, setChecking] = useState(false);
  const [phase, setPhase] = useState<"idle" | "uploading" | "checking" | "sending">("idle");
  const [uploaded, setUploaded] = useState(0);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [dragging, setDragging] = useState(false);
  const clientId = useRef(newClientId());
  const uploadAbort = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const mode = device.meta?.send_mode ?? "unavailable";
  const verifyCode = device.meta?.verification.code;

  useEffect(() => { clientId.current = newClientId(); setError(""); }, [items]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; uploadAbort.current?.abort(); }; }, []);
  useEffect(() => {
    registerAppend?.(paths => setItems(current => [...current, ...itemsFromPaths(paths)]));
    return () => registerAppend?.(null);
  }, [registerAppend]);

  // Desktop: preflight the selected paths (sizes, free space, blocking issues).
  // Browser: preflight without sources for route/space; sizes are local.
  const paths = useMemo(() => items.flatMap(item => (item.path ? [item.path] : [])), [items]);
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setChecking(true);
      drop.preflight(desktop && paths.length ? { peer_id: device.id, paths } : { peer_id: device.id })
        .then(result => { if (!cancelled) setPreflight(result); })
        .catch(() => { if (!cancelled) setPreflight(null); })
        .finally(() => { if (!cancelled) setChecking(false); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [device.id, desktop, paths.join("\n")]);

  const shown = items.map((item, index) => {
    const info = item.path && preflight?.items && preflight.items.length === paths.length
      ? preflight.items[paths.indexOf(item.path)] ?? preflight.items[index] : undefined;
    return info ? { ...item, kind: info.kind, size: bytesValue(info.size), count: info.file_count } : item;
  });
  const total = desktop && paths.length && preflight?.total_bytes !== undefined
    ? bytesValue(preflight.total_bytes)
    : shown.every(item => item.size !== null) ? sumBytes(shown.map(item => item.size)) : null;
  const route = preflight?.route ?? device.route;
  const free = preflight?.receiver_free_bytes;
  const blocking = (preflight?.issues || []).filter(issue => !issue.code.startsWith("warn_") && issue.code !== "no_sources");
  const warnings = (preflight?.issues || []).filter(issue => issue.code.startsWith("warn_"));
  const busy = phase !== "idle";
  const disabled = !items.length || busy || mode === "unavailable" || (mode === "verify" && !confirmed) || (desktop && blocking.length > 0);

  const add = async (kind: "files" | "folder") => {
    if (desktop) {
      const picked = await chooseDropPaths(kind).catch(() => []);
      if (picked.length) setItems(current => [...current, ...itemsFromPaths(picked)]);
    } else {
      const picked = await pickBrowserFiles(kind);
      if (picked.length) setItems(current => [...current, ...itemsFromFiles(picked)]);
    }
  };

  const send = async () => {
    if (disabled) return;
    setError("");
    try {
      if (mode === "verify") { setPhase("checking"); await drop.verifyPeer(device.id, true); }
      if (!mounted.current) return;
      let body: { peer_id: string; paths?: string[]; upload_ids?: string[] };
      if (desktop) body = { peer_id: device.id, paths };
      else {
        uploadAbort.current = new AbortController();
        setPhase("uploading");
        setUploaded(0);
        const batch = clientId.current;
        const ids: string[] = [];
        const files = items.flatMap(item => item.files || []);
        for (const picked of files) {
          const result = await drop.run(`upload:${batch}:${picked.rel}`, () => dropApi.upload(picked.file, picked.rel, batch, uploadAbort.current!.signal));
          if (!result) throw new Error("upload");
          ids.push(result.upload_id);
          setUploaded(count => count + 1);
        }
        body = { peer_id: device.id, upload_ids: ids };
      }
      setPhase("checking");
      const final = await drop.preflight(body);
      if (!mounted.current) return;
      setPreflight(final);
      if (isBlocking(final)) { setPhase("idle"); return; }
      setPhase("sending");
      await drop.createTransfer({ ...body, note: note.trim() || undefined, client_id: clientId.current });
      drop.notify("ok", mode === "queue" ? t("drop.queuedCreated") : t("drop.transferCreated"));
      onClose();
    } catch (reason) {
      if (!mounted.current) return;
      setError(t("drop.sendFailed", { message: reason instanceof Error ? reason.message : String(reason) }));
      setPhase("idle");
    }
  };

  const fileTotal = shown.reduce((sum, item) => sum + (item.count ?? 1), 0);
  const freeBytes = free !== null && free !== undefined ? bytesValue(free) : null;
  const usedPercent = total !== null && freeBytes !== null && freeBytes > 0n ? Math.min(100, Number((total * 1000n) / freeBytes) / 10) : null;
  const hue = deviceHue(device.id, { self: device.self, online: device.online });
  return <Modal title={init.kind === "folder" ? t("drop.sheetTitleFolder") : t("drop.sheetTitleFiles")} onClose={onClose} className="rf rf-dlg rf-dlg-send">
    <span className="rf-mark"><Send size={20} strokeWidth={1.8} aria-hidden="true" /></span>
    <p className="rf-sub">{t("drop.sheetSubtitle")}</p>
    <div className="rf-body">
      <div className={`rf-recip rf-h-${hue}`} data-off={device.online === false ? "true" : "false"}>
        <DeviceTile kind={device.kind} hue={hue} off={device.online === false} />
        <div className="rf-recip-text">
          <small className="rf-recip-cap">{t("drop.sendToDevice")}</small>
          <strong className="rf-ell">{device.name}</strong>
          <small className="rf-ell">{[relationshipLabel(t, device), device.meta?.platform].filter(Boolean).join(" · ")}</small>
        </div>
        {(route === "lan" || route === "public" || route === "relay") && device.online !== false ? <RouteBadge route={route} latency={device.latency} online={device.online} /> : null}
      </div>
      <section className="rf-sec" aria-labelledby="send-ready-title">
        <div className="rf-sec-head">
          <h3 id="send-ready-title" className="rf-h3">{t("drop.readyToSend")}</h3>
          <small className="pf-drop-num">{total !== null ? t("drop.itemsSummary", { count: shown.length, size: formatBytes(total) }) : t("drop.itemsCount", { count: shown.length })}</small>
        </div>
        {items.length > 0 && <ul className="rf-list rf-files">
          {shown.map(item => <li key={item.key}>
            <div className="rf-file">
              <FileTile kind={fileKindOf(item.name, item.kind === "folder" ? "folder" : undefined)} />
              <div className="rf-file-text">
                <b className="rf-ell">{item.name}</b>
                <small className="pf-drop-num">
                  {item.kind === "folder"
                    ? item.size !== null ? t("drop.folderSummary", { count: item.count ?? 0, size: formatBytes(item.size) }) : t("drop.folderCount", { count: item.count ?? 0 })
                    : item.size !== null ? formatBytes(item.size) : item.path ? checking ? t("drop.checking") : "" : ""}
                </small>
              </div>
              {item.kind === "folder" && item.files && <button type="button" className="rf-ib rf-ib-sm" aria-expanded={expanded === item.key}
                aria-label={expanded === item.key ? t("drop.hideFiles") : t("drop.showFiles")} onClick={() => setExpanded(expanded === item.key ? null : item.key)}>
                {expanded === item.key ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
              </button>}
              <button type="button" className="rf-ib rf-ib-sm" aria-label={t("drop.removeItem", { name: item.name })} disabled={busy}
                onClick={() => setItems(current => current.filter(entry => entry.key !== item.key))}><X size={15} aria-hidden="true" /></button>
            </div>
            {expanded === item.key && item.files && <ul className="rf-subfiles">
              {item.files.slice(0, 50).map(picked => <li key={picked.rel}><span className="rf-ell">{picked.rel.slice(item.name.length + 1)}</span><small className="pf-drop-num">{formatBytes(picked.file.size)}</small></li>)}
              {item.files.length > 50 && <li><small>{t("drop.moreFiles", { count: item.files.length - 50 })}</small></li>}
            </ul>}
          </li>)}
        </ul>}
        {!items.length && <p className="rf-hint">{t("drop.emptyList")}</p>}
        <div className={`rf-drop${dragging ? " dragging" : ""}`}
          onDragOver={event => { if (!desktop && event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }}
          onDragLeave={() => setDragging(false)}
          onDrop={async event => {
            event.preventDefault();
            setDragging(false);
            if (desktop) return;
            const files = await filesFromDataTransfer(event.dataTransfer);
            if (files.length) setItems(current => [...current, ...itemsFromFiles(files)]);
          }}>
          <button type="button" className="rf-lnk" disabled={busy} onClick={() => void add("files")}><Plus size={15} strokeWidth={1.9} aria-hidden="true" />{t("drop.addFiles")}</button>
          <button type="button" className="rf-lnk" disabled={busy} onClick={() => void add("folder")}><Plus size={15} strokeWidth={1.9} aria-hidden="true" />{t("drop.addFolder")}</button>
          <span>{t("drop.orDropHere")}</span>
        </div>
      </section>
      <label className="rf-field">
        <span>{t("drop.noteLabel")}</span>
        <input className="rf-inp rf-inp-m" value={note} maxLength={500} placeholder={t("drop.notePlaceholder")} onChange={event => setNote(event.target.value)} />
      </label>
      <div className="rf-stack-box">
        {(route === "lan" || route === "public") && <div className="rf-notice rf-t-ok">
          <span className="rf-notice-ico"><LockKeyhole size={15} strokeWidth={1.8} aria-hidden="true" /></span>
          <div><b>{t("drop.directNoLimit")}</b><small>{t("drop.directNoLimitDetail")}</small></div>
        </div>}
        <p className="rf-capacity pf-drop-num" role="status">
          <span>{free === null || free === undefined ? t("drop.waitingReceiverSpace") : t("drop.receiverFree", { size: formatBytes(free) })}</span>
          {usedPercent !== null && <span className="rf-prog" aria-hidden="true"><i style={{ width: `${Math.max(3, usedPercent)}%` }} /></span>}
        </p>
      </div>
      {mode === "verify" && <div className="rf-verify rf-t-warn">
        <span className="rf-verify-cap">{t("drop.verificationCode")}</span>
        <CodeDigits code={verifyCode || ""} label={t("drop.verificationCode")} />
        <small>{t("drop.verifyHint")}</small>
        <label className="rf-check"><input type="checkbox" checked={confirmed} disabled={!verifyCode} onChange={event => setConfirmed(event.target.checked)} />{t("drop.verifyConfirm")}</label>
      </div>}
      {!desktop && <p className="rf-note"><Info size={14} aria-hidden="true" />{t("drop.browserNote")}</p>}
      {[...blocking, ...warnings].map(issue => issue.code.startsWith("warn_")
        ? <p key={`${issue.code}:${issue.path || ""}`} className="rf-note">
          <Info size={14} aria-hidden="true" />{issue.message || issue.code}{issue.path ? ` · ${issue.path}` : ""}</p>
        : <div key={`${issue.code}:${issue.path || ""}`} className="rf-callout rf-callout-err rf-t-bad" role="alert"><TriangleAlert size={16} aria-hidden="true" />
          <div><b>{issue.need_bytes !== undefined && issue.free_bytes !== undefined
            ? t("drop.st_insufficient_space", { need: formatBytes(issue.need_bytes), free: formatBytes(issue.free_bytes) })
            : issue.message || issue.code}{issue.path ? ` · ${issue.path}` : ""}</b>
            {issue.need_bytes !== undefined && issue.free_bytes !== undefined && <small>{t("drop.freeUpSpace", { name: device.name })}</small>}</div></div>)}
      {mode === "unavailable" && <p className="rf-callout rf-callout-err" role="alert"><TriangleAlert size={16} aria-hidden="true" /><span>{t("drop.sendUnavailable")}{device.meta?.reason ? ` · ${reasonLabel(t, device.meta.reason)}` : ""}</span></p>}
      {mode === "queue" && <p className="rf-callout"><Clock size={15} aria-hidden="true" /><span>{t("drop.footerQueue")}</span></p>}
      {error && <p className="rf-callout rf-callout-err" role="alert"><TriangleAlert size={16} aria-hidden="true" /><span>{error}</span></p>}
    </div>
    <footer className="rf-foot">
      <small className="rf-foot-note">{phase === "uploading" ? t("drop.uploading", { done: uploaded, total: fileTotal })
        : phase === "checking" ? t("drop.checking") : mode === "queue" ? "" : t("drop.footerPrefs")}</small>
      <button type="button" className="rf-btn" onClick={onClose}>{t("personal.cancel")}</button>
      <button type="button" className="rf-btn rf-btn-p" disabled={disabled} onClick={() => void send()}>
        {mode === "queue" ? <Clock size={15} aria-hidden="true" /> : <Send size={15} strokeWidth={1.8} aria-hidden="true" />}{sendButtonLabel(t, device)}
      </button>
    </footer>
  </Modal>;
}
