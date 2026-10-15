import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, File, Folder, Info, Laptop, LockKeyhole, Monitor, Plus, Send, X } from "lucide-react";
import { Modal } from "../components";
import { useDrop } from "../dropStore";
import { baseName, canPickPaths, chooseDropPaths } from "../../domain/dropDesktop";
import {
  bytesValue, dropApi, formatBytes, newClientId, sumBytes,
  type Preflight,
} from "../../domain/dropClient";
import { filesFromDataTransfer, pickBrowserFiles, type PickedFile } from "./fileCollect";
import { RouteChip } from "./TransferCard";
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

  const DeviceIcon = device.kind === "laptop" ? Laptop : Monitor;
  const fileTotal = shown.reduce((sum, item) => sum + (item.count ?? 1), 0);
  return <Modal title={init.kind === "folder" ? t("drop.sheetTitleFolder") : t("drop.sheetTitleFiles")} onClose={onClose} className="pf-drop-sheet">
    <p className="pf-drop-sheet-sub">{t("drop.sheetSubtitle")}</p>
    <div className="pf-drop-recipient">
      <DeviceIcon size={26} strokeWidth={1.4} aria-hidden="true" />
      <div className="pf-drop-grow">
        <strong>{device.name}</strong>
        <small>{[relationshipLabel(t, device), device.meta?.platform].filter(Boolean).join(" · ")}</small>
      </div>
      <RouteChip route={route ?? null} muted />
    </div>
    <div className="pf-drop-between pf-drop-listhead">
      <h3>{t("drop.readyToSend")}</h3>
      <small className="pf-drop-num">{total !== null ? t("drop.itemsSummary", { count: shown.length, size: formatBytes(total) }) : t("drop.itemsCount", { count: shown.length })}</small>
    </div>
    <ul className="pf-drop-filelist">
      {shown.map(item => <li key={item.key}>
        <div className="pf-drop-fileline">
          <span className="pf-drop-fileicon">{item.kind === "folder" ? <Folder size={18} strokeWidth={1.5} /> : <File size={18} strokeWidth={1.5} />}</span>
          <div className="pf-drop-grow">
            <div className="pf-drop-filename">{item.name}</div>
            <small className="pf-drop-num">
              {item.kind === "folder"
                ? item.size !== null ? t("drop.folderSummary", { count: item.count ?? 0, size: formatBytes(item.size) }) : t("drop.folderCount", { count: item.count ?? 0 })
                : item.size !== null ? formatBytes(item.size) : item.path ? checking ? t("drop.checking") : "" : ""}
            </small>
          </div>
          {item.kind === "folder" && item.files && <button type="button" className="pf-icon-button" aria-expanded={expanded === item.key}
            aria-label={expanded === item.key ? t("drop.hideFiles") : t("drop.showFiles")} onClick={() => setExpanded(expanded === item.key ? null : item.key)}>
            {expanded === item.key ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          </button>}
          <button type="button" className="pf-icon-button" aria-label={t("drop.removeItem", { name: item.name })} disabled={busy}
            onClick={() => setItems(current => current.filter(entry => entry.key !== item.key))}><X size={16} /></button>
        </div>
        {expanded === item.key && item.files && <ul className="pf-drop-subfiles">
          {item.files.slice(0, 50).map(picked => <li key={picked.rel}><span>{picked.rel.slice(item.name.length + 1)}</span><small className="pf-drop-num">{formatBytes(picked.file.size)}</small></li>)}
          {item.files.length > 50 && <li><small>{t("drop.moreFiles", { count: item.files.length - 50 })}</small></li>}
        </ul>}
      </li>)}
    </ul>
    {!items.length && <p className="pf-drop-empty-line">{t("drop.emptyList")}</p>}
    <div className={`pf-drop-droparea${dragging ? " dragging" : ""}`}
      onDragOver={event => { if (!desktop && event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }}
      onDragLeave={() => setDragging(false)}
      onDrop={async event => {
        event.preventDefault();
        setDragging(false);
        if (desktop) return;
        const files = await filesFromDataTransfer(event.dataTransfer);
        if (files.length) setItems(current => [...current, ...itemsFromFiles(files)]);
      }}>
      <button type="button" className="pf-drop-link" disabled={busy} onClick={() => void add("files")}><Plus size={15} aria-hidden="true" />{t("drop.addFiles")}</button>
      <button type="button" className="pf-drop-link" disabled={busy} onClick={() => void add("folder")}><Plus size={15} aria-hidden="true" />{t("drop.addFolder")}</button>
      <span>{t("drop.orDropHere")}</span>
    </div>
    <label className="pf-drop-field">
      <span>{t("drop.noteLabel")}</span>
      <input value={note} maxLength={500} placeholder={t("drop.notePlaceholder")} onChange={event => setNote(event.target.value)} />
    </label>
    {(route === "lan" || route === "public") && <div className="pf-drop-notice">
      <LockKeyhole size={16} aria-hidden="true" />
      <div><strong>{t("drop.directNoLimit")}</strong><span>{t("drop.directNoLimitDetail")}</span></div>
    </div>}
    <p className="pf-drop-capacity pf-drop-num" role="status">
      {free === null || free === undefined ? t("drop.waitingReceiverSpace") : t("drop.receiverFree", { size: formatBytes(free) })}
    </p>
    {mode === "verify" && <div className="pf-drop-verify">
      <span>{t("drop.verificationCode")}</span>
      <strong className="pf-drop-code">{verifyCode || "—"}</strong>
      <small>{t("drop.verifyHint")}</small>
      <label className="pf-drop-check"><input type="checkbox" checked={confirmed} disabled={!verifyCode} onChange={event => setConfirmed(event.target.checked)} />{t("drop.verifyConfirm")}</label>
    </div>}
    {!desktop && <p className="pf-drop-hint"><Info size={14} aria-hidden="true" />{t("drop.browserNote")}</p>}
    {[...blocking, ...warnings].map(issue => <p key={`${issue.code}:${issue.path || ""}`} className={issue.code.startsWith("warn_") ? "pf-drop-hint" : "pf-error"} role={issue.code.startsWith("warn_") ? undefined : "alert"}>
      {issue.need_bytes !== undefined && issue.free_bytes !== undefined
        ? t("drop.st_insufficient_space", { need: formatBytes(issue.need_bytes), free: formatBytes(issue.free_bytes) })
        : issue.message || issue.code}{issue.path ? ` · ${issue.path}` : ""}
    </p>)}
    {mode === "unavailable" && <p className="pf-error" role="alert">{t("drop.sendUnavailable")}{device.meta?.reason ? ` · ${reasonLabel(t, device.meta.reason)}` : ""}</p>}
    {error && <p className="pf-error" role="alert">{error}</p>}
    <footer className="pf-drop-sheet-footer">
      <small>{phase === "uploading" ? t("drop.uploading", { done: uploaded, total: fileTotal })
        : phase === "checking" ? t("drop.checking") : mode === "queue" ? t("drop.footerQueue") : t("drop.footerPrefs")}</small>
      <button type="button" className="pf-button primary" disabled={disabled} onClick={() => void send()}>
        <Send size={16} aria-hidden="true" />{sendButtonLabel(t, device)}
      </button>
    </footer>
  </Modal>;
}
