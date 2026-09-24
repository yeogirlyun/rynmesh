import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { invoke } from "@tauri-apps/api/core";
import { Box, FileInput, MoreHorizontal, Play, Square } from "lucide-react";
import { tr } from "../../uiI18n";
import { isTauriDesktop } from "../../domain/nodeUrl";
import { inspectNativeModel, uploadNativeModel, nativeBusy, nativeError, nativeModelRequest, nativeState, type LocalModelFile, type NativeModelStatus } from "../../domain/nativeModel";
import "./NativeModelPanel.css";

export default function NativeModelPanel({ status, onUpdate, onConnect }: {
  status: NativeModelStatus | null; onUpdate: (value: NativeModelStatus) => void; onConnect: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  const [remove, setRemove] = useState(false);
  const [selected, setSelected] = useState<(LocalModelFile & { source?: string; file?: File }) | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const upload = useRef<AbortController | null>(null);
  const perform = async (task: () => Promise<NativeModelStatus | null>) => {
    setBusy(true); setError("");
    try { const next = await task(); if (next) onUpdate(next); }
    catch (e) { if ((e as Error).message !== "cancelled") setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const action = (name: string) => void perform(() => nativeModelRequest(name, {}));
  const setting = (key: "autostart" | "sharing", value: boolean) => void perform(() => nativeModelRequest("", { [key]: value }, "PATCH"));
  const choose = () => {
    if (!isTauriDesktop()) { input.current?.click(); return; }
    void perform(async () => {
      const source = await invoke<string | null>("choose_local_model");
      if (source) { setSelected(null); setSelected({ ...await inspectNativeModel(source), source }); }
      return null;
    });
  };
  const chooseBrowserFile = async (file?: File) => {
    if (!file) return;
    setError(""); setSelected(null);
    if (!file.name.toLowerCase().endsWith(".gguf")) { setError("unsupported_file"); return; }
    if (file.size > 64 * 1024 ** 3) { setError("model_too_large"); return; }
    const header = new Uint8Array(await file.slice(0, 8).arrayBuffer());
    if (header.length !== 8 || String.fromCharCode(...header.slice(0, 4)) !== "GGUF" || new DataView(header.buffer).getUint32(4, true) !== 3) {
      setError("invalid_gguf"); return;
    }
    setSelected({ name: file.name.replace(/\.gguf$/i, ""), file_name: file.name, size_bytes: file.size, file });
  };
  const importSelected = () => void perform(async () => {
    if (!selected) return null;
    let result: NativeModelStatus;
    if (selected.file) {
      upload.current = new AbortController(); setUploadProgress(0);
      try { result = await uploadNativeModel(selected.file, setUploadProgress, upload.current.signal); }
      finally { upload.current = null; setUploadProgress(null); }
    } else result = await nativeModelRequest("import", { source: selected.source });
    setSelected(null);
    return result;
  });
  if (!status) return <p role="status">{tr("正在读取模型状态…")}</p>;
  const running = status.state === "running";
  const working = nativeBusy(status) || uploadProgress !== null;
  const stages: Record<string, string> = { checking: "正在检查设备…", uploading: "正在导入文件…", validating: "正在校验模型…", copying: "正在导入文件…", runtime: "正在准备运行组件…", starting: "正在启动模型…", stopping: "正在停止", testing: "正在测试回答…", draining: "当前回答完成后停止" };
  const errorCode = error || status.error;
  const title = status.installed ? status.name : selected?.name || tr("导入本地模型");
  const progress = uploadProgress ?? status.progress;
  return <div className="native-model-panel">
    <input ref={input} type="file" accept=".gguf" hidden aria-label={tr("选择模型文件")} onChange={e => { void chooseBrowserFile(e.target.files?.[0]).catch(() => setError("unsupported_file")); e.target.value = ""; }} />
    <div className="native-model-heading"><span className="native-model-symbol"><Box size={32} strokeWidth={1.5} /></span><div>
      <h3>{title}</h3><p>{tr("在这台电脑上运行")}</p>
      {status.installed ? <span className={`native-model-state ${running ? "running" : ""}`}><i />{nativeState(status)}</span>
        : <small>{selected ? `${selected.file_name} · ${(selected.size_bytes / 1024 ** 3).toFixed(2)} GB` : tr("选择你已有的 GGUF 模型文件")}</small>}
    </div></div>
    {!status.supported && <p className="native-model-notice">{nativeError("unsupported_platform")}</p>}
    {working ? <div className="native-model-progress" role="status" aria-live="polite">
      <p>{tr(uploadProgress !== null ? "正在导入文件…" : stages[status.stage] || "正在处理…")}{progress !== null && <span>{progress}%</span>}</p>
      <progress aria-label={tr("模型安装进度")} max={100} {...(progress === null ? {} : { value: progress })} />
      <div><small>{tr("关闭此窗口后，任务继续在后台运行。")}</small>{(status.state === "installing" || uploadProgress !== null) && <button className="native-model-link" onClick={() => upload.current ? upload.current.abort() : action("cancel")}>{tr("取消安装")}</button>}</div>
    </div> : <>
      <div className="native-model-actions">
        {running ? <><Link className="ais-primary" to={`/services/private-ai/chat?peer=${encodeURIComponent(status.peer_id)}&service=${encodeURIComponent(status.id)}`}>{tr("开始聊天")}</Link><button className="ais-secondary" disabled={busy} onClick={() => action("stop")}><Square size={15} />{tr("停止")}</button></>
          : status.installed ? <button className="ais-primary" disabled={busy || !status.supported} onClick={() => action("start")}><Play size={16} />{tr(status.state === "error" ? "重试" : "启动")}</button>
          : <button className="ais-primary" disabled={busy || !status.supported} onClick={selected ? importSelected : choose}><FileInput size={18} />{busy ? tr("处理中…") : tr(selected ? "导入并启动" : "选择模型文件")}</button>}
        {status.installed && <button className="ais-secondary native-model-more" aria-label={tr("更多模型设置")} aria-expanded={more} onClick={() => setMore(!more)}><MoreHorizontal size={19} /></button>}
      </div>
      {!status.installed && <div className="native-model-import">
        {selected && <button className="native-model-link" disabled={busy} onClick={choose}>{tr("重新选择文件")}</button>}
        <p>{tr("支持单个 GGUF 文本模型，最大 64 GB。导入时检查兼容性。")}</p>
        <small>{tr("缺少运行组件时会自动下载组件，不会下载模型。")}</small>
      </div>}
    </>}
    {errorCode && <div className="native-model-notice" role="alert">{nativeError(errorCode)}{errorCode === "personal_space_required" && <Link to="/settings/space">{tr("打开个人空间设置")}</Link>}</div>}
    {status.installed && <div className="native-model-settings">
      <label><span><strong>{tr("随 Ryn 启动")}</strong><small>{tr("下次打开软件时自动启动这个模型")}</small></span><input role="switch" type="checkbox" aria-label={tr("随 Ryn 启动")} checked={status.autostart} disabled={busy} onChange={e => setting("autostart", e.target.checked)} /></label>
      <label><span><strong>{tr("分享给我的设备")}</strong><small>{tr("默认仅本机使用。")}</small></span><input role="switch" type="checkbox" aria-label={tr("分享给我的设备")} checked={status.sharing} disabled={busy} onChange={e => setting("sharing", e.target.checked)} /></label>
    </div>}
    {more && !working && <div className="native-model-details"><p>{status.file_name}</p><p>{tr("运行方式")}: {status.backend || tr("启动时自动检测")}</p>
      <p>{tr("更换模型时，先卸载当前副本，再选择其他文件。")}</p>
      {!remove ? <button className="native-model-link" onClick={() => setRemove(true)}>{tr("卸载模型")}</button>
        : <><p>{tr("删除 Ryn 管理的模型副本，保留你导入的原文件。")}</p><button className="ais-secondary" disabled={busy} onClick={() => { setRemove(false); setMore(false); action("uninstall"); }}>{tr("确认卸载")}</button><button className="native-model-link" onClick={() => setRemove(false)}>{tr("取消")}</button></>}
    </div>}
    <div className="native-model-footer">{status.installed ? tr("停止后保留模型文件，下次可直接启动。") : <>{tr("系统会自动匹配 Mac 或 Windows。")}<button className="native-model-link" onClick={onConnect}>{tr("连接已有服务")}</button></>}</div>
  </div>;
}
