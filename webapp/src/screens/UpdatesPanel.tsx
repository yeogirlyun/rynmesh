import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDownToLine, Check, CircleAlert, Clock3, LoaderCircle, Lock } from "lucide-react";
import { Button } from "../components/ui";
import { useUILanguage } from "../uiI18n";
import {
  desktopUpdates,
  pickLocalizedLabel,
  pluginUpdates,
  SERVICE_UPDATE_CHANGED_EVENT,
  serviceUpdates,
  VISIBLE_UPDATE_FAILURES,
  type DesktopUpdateStatus,
  type PluginUpdateSource,
  type PluginUpdateStatus,
  type ServiceUpdateRow,
  type ServiceUpdateStatus,
} from "../domain/updates";
import { craftPlugins, type CraftPlugin } from "../domain/craftPlugins";
import { isTauriDesktop } from "../domain/nodeUrl";
import { PageHeading } from "../personal/components";
import "./UpdatesPanel.css";

type Text = (zh: string, en: string) => string;
function useText(): Text {
  const language = useUILanguage();
  return (zh: string, en: string) => language.startsWith("zh") ? zh : en;
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
const RELEASES = "https://github.com/yeogirlyun/rynmesh/releases";
const states: Record<string, [string, string]> = {
  idle: ["尚未检查", "Not checked"], checking: ["正在检查", "Checking"], available: ["发现新版本", "Update available"],
  cancelled: ["已取消检查", "Check cancelled"],
  up_to_date: ["已是最新兼容版本", "Up to date"], downloading: ["正在下载并验证", "Downloading and verifying"],
  ready: ["已准备好，点击即可重启更新", "Ready — restart to update"], installing: ["正在安装，Ryn 将自动重新打开", "Installing; Ryn reopens automatically"],
  waiting: ["当前任务结束后自动更新", "Updates when current work finishes"],
  updated: ["更新完成", "Updated"], waiting_for_idle: ["等待编辑器关闭；关闭后重启 Ryn 即可释放插件", "Waiting for the editor to close; restart Ryn after closing it"],
  rolled_back: ["已恢复上一版本", "Previous version restored"], error: ["更新失败，可重试", "Update failed; retry available"],
};
const waitingFor: Record<string, [string, string]> = {
  editors_open: ["先关闭正在编辑的文档，关闭后自动更新", "Close open documents; Ryn updates after that"],
  agent_running: ["Agent 任务完成后自动更新", "Updates when the Agent task finishes"],
  transfer_running: ["文件传输完成后自动更新", "Updates when file transfers finish"],
  plugin_busy: ["插件操作完成后自动更新", "Updates when plugin work finishes"],
  tasks_running: ["AI 任务完成后自动更新", "Updates when AI tasks finish"],
  node_unavailable: ["Ryn 本机服务就绪后自动更新", "Updates once Ryn's local service is ready"],
};
function waitingText(tx: Text, status: DesktopUpdateStatus | null) {
  return tx(...(waitingFor[status?.waitingFor || ""] || states.waiting));
}
/** Plain words for the errors an owner can act on; the raw error stays in Settings. */
export function updateErrorText(tx: Text, error: string | null | undefined) {
  const value = (error || "").toLowerCase();
  if (value.includes("update_check_timeout")) return tx("检查更新超时（20 秒），请稍后重试。", "Update check timed out after 20 seconds. Try again later.");
  if (value.includes("signing_key_not_configured")) return tx("这个版本没有内置更新公钥，需要手动安装一次新版本。", "This build has no update key; install a new version manually once.");
  if (value.includes("origin_rejected")) return tx("更新地址不是官方发布页，已拒绝。", "The update did not come from the official release page and was rejected.");
  if (value.includes("signature")) return tx("更新包签名不对，已拒绝安装。", "The update's signature did not match, so it was not installed.");
  if (value.includes("node_not_owned")) return tx("Ryn 本机服务由其他程序管理，无法在这里更新。", "Ryn's local service is managed by another program, so it cannot update here.");
  if (/request|connect|dns|timed? ?out|network|tls|status code|404|403/.test(value)) return tx("连不上 GitHub，稍后会自动重试。", "Could not reach GitHub; Ryn retries automatically.");
  return tx("更新失败，可以重试。", "The update failed; you can retry.");
}

/** Plain, human-readable words for plugin runtime update errors — never raw error codes. */
export function serviceUpdateErrorText(tx: Text, error: string | null | undefined) {
  const value = (error || "").toLowerCase();
  if (value.includes("untrusted_signer") || value.includes("bad_signature") || value.includes("invalid_public_key")) {
    return tx("插件更新包的签名不对，已拒绝安装，现有版本可以继续用。", "The plugin update's signature did not match and was refused; the current version keeps working.");
  }
  if (value.includes("checksum_mismatch") || value.includes("download_too_large") || value.includes("too_large")) {
    return tx("下载的文件校验未通过，已放弃本次更新，现有版本不受影响。", "Downloaded files failed verification and were discarded; the current version is untouched.");
  }
  if (value.includes("origin_rejected") || value.includes("auth_origin_mismatch") || value.includes("invalid_url")) {
    return tx("更新地址与登录域名不一致，已拒绝连接。", "The update address did not match the sign-in domain and was refused.");
  }
  if (value.includes("not_published")) {
    return tx("更新服务暂未发布新版本，稍后会自动再查。", "No release is published at the update service yet; Ryn checks again later.");
  }
  if (value.includes("unhealthy") || value.includes("rolled_back")) {
    return tx("新版本没能正常启动，已自动退回上一版本，插件可以照常使用。", "The new version could not start and was rolled back automatically; your plugins still work.");
  }
  if (value.includes("busy")) {
    return tx("正在处理上一项更新操作，请稍候再试。", "Another update operation is in progress; try again in a moment.");
  }
  if (value.includes("server_error") || value.includes("download_failed")) {
    return tx("暂时连不上插件更新服务器，15 分钟后会自动重试。", "Could not reach the plugin update server; Ryn retries in 15 minutes.");
  }
  return tx("插件更新遇到问题，现有版本可以照常使用，稍后可重试。", "The plugin update ran into a problem; your current version still works and you can retry.");
}

function canInstall(status: DesktopUpdateStatus | null) { return status?.state === "ready" || status?.state === "waiting"; }
function shellVisibleError(status: DesktopUpdateStatus | null) {
  return status?.state === "error" && (status.failures || 0) >= VISIBLE_UPDATE_FAILURES;
}

function useDesktopUpdate(interval: number) {
  const [status, setStatus] = useState<DesktopUpdateStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(() => desktopUpdates.status().then(setStatus), []);
  useEffect(() => {
    let live = true;
    const tick = () => desktopUpdates.status().then(value => { if (live) setStatus(value); }).catch(e => { if (live) setError(message(e)); });
    tick(); const timer = window.setInterval(tick, interval);
    return () => { live = false; window.clearInterval(timer); };
  }, [interval]);
  const run = useCallback(async (action: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (e) { setError(message(e)); await refresh().catch(() => undefined); }
    finally { setBusy(false); }
  }, [refresh]);
  return { status, error, busy, run };
}

function useServiceUpdates(interval: number) {
  const [status, setStatus] = useState<ServiceUpdateStatus>({ services: [] });
  const [busyIds, setBusyIds] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const refresh = useCallback(async () => {
    if (!serviceUpdates?.status) return;
    const value = await serviceUpdates.status();
    setStatus(value);
  }, []);
  useEffect(() => {
    if (!serviceUpdates?.status) return;
    let live = true;
    const tick = () => {
      serviceUpdates.status()
        .then(value => { if (live && value) setStatus(value); })
        .catch(() => undefined);
    };
    const onChanged = (event: Event) => {
      const detail = (event as CustomEvent<ServiceUpdateStatus>).detail;
      if (live && detail?.services) setStatus(detail);
      else tick();
    };
    tick();
    const timer = window.setInterval(tick, interval);
    window.addEventListener(SERVICE_UPDATE_CHANGED_EVENT, onChanged);
    return () => {
      live = false;
      window.clearInterval(timer);
      window.removeEventListener(SERVICE_UPDATE_CHANGED_EVENT, onChanged);
    };
  }, [interval]);
  const runService = useCallback(async (id: string, action: () => Promise<ServiceUpdateStatus>) => {
    setBusyIds(prev => ({ ...prev, [id]: true }));
    setErrors(prev => ({ ...prev, [id]: "" }));
    try {
      const next = await action();
      if (next?.services) setStatus(next);
    } catch (e) {
      setErrors(prev => ({ ...prev, [id]: message(e) }));
      await refresh().catch(() => undefined);
    } finally {
      setBusyIds(prev => ({ ...prev, [id]: false }));
    }
  }, [refresh]);
  return { status, busyIds, errors, runService };
}

/** Codex-style entry next to the Ryn logo: hidden until an update needs the owner. */
export function DesktopUpdateButton({ floating = false }: { floating?: boolean }) {
  const tx = useText();
  const { status, error, busy, run } = useDesktopUpdate(5000);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  const state = status?.state;
  const failed = shellVisibleError(status);
  if (!status || !(state === "ready" || state === "waiting" || failed)) return null;
  const kind = state === "waiting" ? "waiting" : failed ? "error" : "ready";
  const label = kind === "waiting" ? tx("等任务完成", "Waiting") : kind === "error" ? tx("更新失败", "Update failed") : tx("更新", "Update");
  const Icon = kind === "waiting" ? Clock3 : kind === "error" ? CircleAlert : ArrowDownToLine;
  const version = status.availableVersion || "";
  return <div className={`ryn-update-entry${floating ? " floating" : ""}`} ref={root}
    onKeyDown={event => { if (event.key === "Escape") setOpen(false); }}>
    <button type="button" className={`ryn-update-pill ${kind}${open ? " open" : ""}`} aria-expanded={open} aria-haspopup="dialog"
      aria-label={kind === "ready" ? tx(`Ryn ${version} 可以更新`, `Ryn ${version} is ready to install`) : label}
      onClick={() => setOpen(value => !value)}>
      <Icon size={15} strokeWidth={2.4} aria-hidden="true" /><span>{label}</span>
    </button>
    {open && <div className="ryn-update-popover" role="dialog" aria-label={tx("Ryn 更新", "Ryn update")}>
      {kind === "ready" && <>
        <div className="ryn-update-popover-head">
          <span className="ryn-update-popover-icon"><ArrowDownToLine size={18} strokeWidth={2.4} aria-hidden="true" /></span>
          <div><strong>{tx(`Ryn ${version} 已准备好`, `Ryn ${version} is ready`)}</strong>
            <span>{tx(`已在后台下载并验证签名 · 当前 ${status.currentVersion}`, `Downloaded and verified · current ${status.currentVersion}`)}</span></div>
        </div>
        <div className="ryn-update-popover-notes">
          <span>{tx("更新内容", "What's new")}</span>
          {status.notes ? <p>{status.notes}</p> : <p>{tx("这个版本没有附说明。", "No release notes for this version.")}</p>}
          <a href={RELEASES} target="_blank" rel="noreferrer">{tx("查看完整说明", "Full release notes")}</a>
        </div>
        <div className="ryn-update-popover-actions">
          <button type="button" className="primary" disabled={busy} onClick={() => void run(() => desktopUpdates.install())}>{tx("重启并更新", "Restart to update")}</button>
          <button type="button" disabled={busy} onClick={() => setOpen(false)}>{tx("稍后", "Later")}</button>
        </div>
        <span className="ryn-update-popover-foot">{status.installOnQuit
          ? tx("选「稍后」也没关系：下次退出 Ryn 时会自动安装。", "Later is fine: it installs the next time you quit Ryn.")
          : tx("你可以随时回到这里更新。", "You can update from here at any time.")}</span>
      </>}
      {kind === "waiting" && <>
        <div className="ryn-update-popover-head">
          <span className="ryn-update-popover-icon waiting"><Clock3 size={18} strokeWidth={2.4} aria-hidden="true" /></span>
          <div><strong>{tx(`Ryn ${version} 等待更新`, `Ryn ${version} is waiting`)}</strong><span>{waitingText(tx, status)}</span></div>
        </div>
        <div className="ryn-update-popover-actions">
          {status.waitingFor !== "editors_open" && <button type="button" className="primary" disabled={busy}
            onClick={() => void run(() => desktopUpdates.install(true))}>{tx("立即更新", "Update now")}</button>}
          <button type="button" disabled={busy} onClick={() => void run(desktopUpdates.cancel)}>{tx("取消", "Cancel")}</button>
        </div>
        {status.waitingFor !== "editors_open" && <span className="ryn-update-popover-foot">
          {tx("「立即更新」会中断正在运行的任务；打开的文档不会被关闭。", "Update now interrupts running work; open documents are never closed.")}</span>}
      </>}
      {kind === "error" && <>
        <div className="ryn-update-popover-head">
          <span className="ryn-update-popover-icon error"><CircleAlert size={18} strokeWidth={2.4} aria-hidden="true" /></span>
          <div><strong>{tx("更新没有完成", "The update did not finish")}</strong><span>{updateErrorText(tx, status.lastError)}</span></div>
        </div>
        <div className="ryn-update-popover-actions">
          <button type="button" className="primary" disabled={busy || !status.configured} onClick={() => void run(desktopUpdates.check)}>{tx("重试", "Retry")}</button>
          <Link className="ryn-update-popover-link" to="/settings/updates" onClick={() => setOpen(false)}>{tx("去设置", "Settings")}</Link>
        </div>
      </>}
      {error && <p role="alert" className="ryn-update-popover-error">{updateErrorText(tx, error)}</p>}
    </div>}
  </div>;
}

/** "Updating Ryn…" while the installer takes over, and one-time "Updated" notices for Ryn & plugin packs. */
export function DesktopUpdateOverlay() {
  const tx = useText();
  const language = useUILanguage();
  const { status } = useDesktopUpdate(1500);
  const { status: serviceStatus } = useServiceUpdates(2500);
  const [dismissed, setDismissed] = useState(false);
  const [dismissedServices, setDismissedServices] = useState<Record<string, boolean>>({});
  const updatedTo = !dismissed && status?.updatedFrom ? status.currentVersion : null;
  const updatedService = serviceStatus.services.find(row => {
    if (!row.updated?.version) return false;
    const key = `${row.id}:${row.updated.version}:${row.updated.at ?? ""}`;
    return !dismissedServices[key];
  });
  const serviceKey = updatedService?.updated
    ? `${updatedService.id}:${updatedService.updated.version}:${updatedService.updated.at ?? ""}`
    : null;
  useEffect(() => {
    if (!updatedTo) return;
    // Ryn may reopen hidden in the tray: the notice counts down only while it can be seen.
    let timer = 0;
    const start = () => {
      if (timer || document.visibilityState !== "visible") return;
      timer = window.setTimeout(() => { setDismissed(true); void desktopUpdates.acknowledge().catch(() => undefined); }, 6000);
    };
    start();
    document.addEventListener("visibilitychange", start);
    return () => { document.removeEventListener("visibilitychange", start); window.clearTimeout(timer); };
  }, [updatedTo]);
  useEffect(() => {
    if (!updatedService || !serviceKey) return;
    let timer = 0;
    const id = updatedService.id;
    const start = () => {
      if (timer || document.visibilityState !== "visible") return;
      timer = window.setTimeout(() => {
        setDismissedServices(prev => ({ ...prev, [serviceKey]: true }));
        void serviceUpdates?.acknowledge?.(id).catch(() => undefined);
      }, 6000);
    };
    start();
    document.addEventListener("visibilitychange", start);
    return () => { document.removeEventListener("visibilitychange", start); window.clearTimeout(timer); };
  }, [updatedService, serviceKey]);
  if (status?.state === "installing") {
    return <div className="ryn-update-installing" role="status" aria-live="polite">
      <LoaderCircle className="spin" size={40} strokeWidth={2.2} aria-hidden="true" />
      <strong>{tx("正在更新 Ryn…", "Updating Ryn…")}</strong>
      <span>{tx("Ryn 会自动关闭并在几秒后重新打开，任务记录和设置都会保留。", "Ryn closes and reopens in a few seconds. Your tasks and settings stay.")}</span>
    </div>;
  }
  if (!updatedTo && !updatedService) return null;
  const serviceName = updatedService ? pickLocalizedLabel(updatedService.name, language, updatedService.id) : "";
  const serviceVersion = updatedService?.updated?.version || "";
  return <>
    {updatedTo && <div className="ryn-update-toast" role="status">
      <Check size={16} strokeWidth={2.6} aria-hidden="true" />
      <span>{tx(`已更新到 Ryn ${updatedTo}`, `Updated to Ryn ${updatedTo}`)}</span>
      <Link to="/settings/updates" onClick={() => { setDismissed(true); void desktopUpdates.acknowledge().catch(() => undefined); }}>
        {tx("看看新内容", "What's new")}</Link>
    </div>}
    {updatedService && serviceKey && <div className={`ryn-update-toast${updatedTo ? " stacked" : ""}`} role="status">
      <Check size={16} strokeWidth={2.6} aria-hidden="true" />
      <span>{tx(`${serviceName}已更新到 ${serviceVersion}`, `${serviceName} updated to ${serviceVersion}`)}</span>
      <Link to="/settings/updates" onClick={() => {
        setDismissedServices(prev => ({ ...prev, [serviceKey]: true }));
        void serviceUpdates?.acknowledge?.(updatedService.id).catch(() => undefined);
      }}>{tx("看看新内容", "What's new")}</Link>
    </div>}
  </>;
}

function formatTime(seconds: number | null | undefined, language: string) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString(language.startsWith("zh") ? "zh-CN" : undefined, { dateStyle: "short", timeStyle: "short" });
}

function PluginPackSquaresIcon() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <rect x="3" y="3" width="8" height="8" rx="2" />
    <rect x="13" y="3" width="8" height="8" rx="2" />
    <rect x="3" y="13" width="8" height="8" rx="2" />
    <rect x="13" y="13" width="8" height="8" rx="2" />
  </svg>;
}

function pluginCountFootnote(tx: Text, count: number): string {
  const zhThis = count === 3 ? "这三个插件" : count === 2 ? "这两个插件" : count === 1 ? "这个插件" : `这 ${count} 个插件`;
  const zhAll = count === 3 ? "三个插件一起更新，" : count === 2 ? "两个插件一起更新，" : count > 3 ? `${count} 个插件一起更新，` : "";
  const enTarget = count === 1 ? "this plugin" : `these ${count} plugins`;
  const enTogether = count > 1 ? "All included plugins update together; login state and data are kept." : "Login state and data are kept.";
  return tx(
    `「立即更新」只重启${zhThis}，大约 10 秒，Ryn 不用重启。${zhAll}登录状态和数据都保留。`,
    `"Update now" restarts only ${enTarget} in about 10 seconds — Ryn stays open. ${enTogether}`,
  );
}

function ServiceUpdateCard({
  row,
  language,
  tx,
  busy,
  error,
  hostBusy,
  onCheck,
  onApply,
  onRetry,
  onUpdateHost,
}: {
  row: ServiceUpdateRow;
  language: string;
  tx: Text;
  busy: boolean;
  error?: string;
  hostBusy: boolean;
  onCheck: () => void;
  onApply: () => void;
  onRetry: () => void;
  onUpdateHost: () => void;
}) {
  const name = pickLocalizedLabel(row.name, language, row.id);
  const subtitle = row.plugin_names.join(" · ");
  const authLabel = pickLocalizedLabel(row.auth_label, language, tx("账号", "your account"));
  const state = row.state || "idle";
  const working = busy || ["checking", "downloading", "applying"].includes(state);
  const percent = row.total ? Math.min(100, Math.round(row.downloaded / row.total * 100)) : null;
  const loginTarget = (row.auth_provider || row.plugins[0])
    ? `/apps/plugins/${encodeURIComponent(row.auth_provider || row.plugins[0])}`
    : "/settings/extensions/plugins";

  let cardVariant = "";
  let iconVariant = "";
  let statusTone = "";
  let statusLine = "";

  if (state === "ready") {
    cardVariant = " ready";
    statusTone = "tone-ready";
    statusLine = tx(
      `${row.available_version || ""} 已下载好，下次打开 Ryn 时自动生效`,
      `${row.available_version || ""} is downloaded and takes effect next time you open Ryn`,
    );
  } else if (state === "downloading") {
    statusTone = "tone-ready";
    statusLine = tx(
      `正在下载 ${row.available_version || ""} · 只下载有变化的文件${percent === null ? "" : ` · ${percent}%`}`,
      `Downloading ${row.available_version || ""} · changed files only${percent === null ? "" : ` · ${percent}%`}`,
    );
  } else if (state === "applying") {
    statusTone = "tone-ready";
    statusLine = tx(
      `正在更新到 ${row.available_version || row.current_version || ""}，大约 10 秒…`,
      `Updating to ${row.available_version || row.current_version || ""}, about 10 seconds…`,
    );
  } else if (state === "waiting") {
    cardVariant = " waiting";
    iconVariant = " warn";
    statusTone = "tone-warn";
    statusLine = tx(
      `${row.available_version || ""} 已下载好，等插件里的任务结束后自动更新`,
      `${row.available_version || ""} is downloaded and updates automatically once plugin tasks finish`,
    );
  } else if (state === "rolled_back") {
    cardVariant = " rolled-back";
    iconVariant = " err";
    statusTone = "tone-err";
    const badVer = row.rolled_back_version || row.available_version || "";
    statusLine = tx(
      `${badVer} 没能正常启动，已自动退回 ${row.current_version || ""}，插件可以照常使用`,
      `${badVer} could not start and was rolled back to ${row.current_version || ""}; your plugins still work normally`,
    );
  } else if (state === "requires_host") {
    statusLine = tx(
      `${row.available_version || ""} 需要 Ryn ${row.min_host || ""} 或更高版本，Ryn 更新后会自动安装`,
      `${row.available_version || ""} requires Ryn ${row.min_host || ""} or newer and installs after Ryn updates`,
    );
  } else if (state === "login_required") {
    iconVariant = " muted";
    statusLine = tx(
      `登录${authLabel}后才能检查更新，现有版本可以继续用`,
      `Sign in to ${authLabel} to check for updates; the current version keeps working`,
    );
  } else if (state === "checking") {
    statusLine = tx("正在检查更新…", "Checking for updates…");
  } else if (state === "error") {
    cardVariant = " error";
    iconVariant = " err";
    statusTone = "tone-err";
    statusLine = serviceUpdateErrorText(tx, error || row.last_error);
  } else if (state === "idle" && !row.last_check) {
    statusLine = tx("已是最新版本", "Up to date");
  } else {
    statusLine = tx(
      `已是最新版本${row.last_check ? ` · 上次检查 ${formatTime(row.last_check, language)}` : ""}`,
      `Up to date${row.last_check ? ` · checked ${formatTime(row.last_check, language)}` : ""}`,
    );
  }

  return <section className={`ryn-update-card ryn-service-card${cardVariant}`}>
    <span className={`ryn-service-icon${iconVariant}`} aria-hidden="true">
      {state === "downloading" ? <ArrowDownToLine size={26} strokeWidth={2} />
        : state === "applying" ? <LoaderCircle className="spin" size={26} strokeWidth={2.2} />
        : state === "waiting" ? <Clock3 size={26} strokeWidth={2} />
        : state === "rolled_back" || state === "error" ? <CircleAlert size={26} strokeWidth={2} />
        : state === "login_required" ? <Lock size={24} strokeWidth={2} />
        : <PluginPackSquaresIcon />}
    </span>
    <div className="ryn-update-card-copy">
      <strong>{name}{row.current_version ? ` ${row.current_version}` : ""}</strong>
      {subtitle && <span className="ryn-service-sub">{subtitle}</span>}
      <span role="status" className={statusTone || undefined}>{statusLine}</span>
      {state === "downloading" && <progress aria-label={tx("插件下载进度", "Plugin download progress")}
        max={row.total || undefined} value={row.total ? row.downloaded : undefined} />}
    </div>
    {state === "ready" && <Button variant="primary" disabled={working} onClick={onApply}>{tx("立即更新", "Update now")}</Button>}
    {state === "waiting" && <Button disabled={working} onClick={onApply}>{tx("立即更新", "Update now")}</Button>}
    {(state === "rolled_back" || state === "error") && <Button disabled={working} onClick={state === "rolled_back" ? onRetry : onCheck}>{tx("重试", "Retry")}</Button>}
    {state === "requires_host" && <Button disabled={hostBusy} onClick={onUpdateHost}>{tx("更新 Ryn", "Update Ryn")}</Button>}
    {state === "login_required" && <Link className="ryn-service-link-btn" to={loginTarget}>{tx("去登录", "Sign in")}</Link>}
    {state === "applying" && <Button disabled>{tx("正在更新…", "Updating…")}</Button>}
    {state === "checking" && <Button disabled>{tx("正在检查", "Checking")}</Button>}
    {(state === "up_to_date" || state === "idle" || state === "updated") && (
      <Button disabled={working} onClick={onCheck}>{tx("检查更新", "Check for updates")}</Button>
    )}
    {state === "ready" && <div className="ryn-service-notes-wrap">
      <div className="ryn-service-notes">
        <strong>{tx(`${row.available_version || ""} 更新内容`, `What's new in ${row.available_version || ""}`)}</strong>
        <p>{row.notes || tx("包含功能改进与问题修复。", "Includes improvements and fixes.")}</p>
      </div>
      <p className="ryn-service-foot">{pluginCountFootnote(tx, row.plugins.length)}</p>
    </div>}
    {error && state !== "error" && <p role="alert" className="ryn-service-foot">{serviceUpdateErrorText(tx, error)}</p>}
  </section>;
}

export function DesktopUpdatesPanel() {
  const tx = useText();
  const language = useUILanguage();
  const { status, error, busy, run } = useDesktopUpdate(1500);
  const { status: serviceStatus, busyIds, errors: serviceErrors, runService } = useServiceUpdates(1500);
  const services = serviceStatus.services || [];
  const hasServices = services.length > 0;
  const state = status?.state || "idle";
  const working = busy || ["checking", "downloading", "installing"].includes(state);
  const percent = status?.total ? Math.min(100, Math.round(status.downloaded / status.total * 100)) : null;
  let line: string;
  if (!status) line = "…";
  else if (state === "downloading") line = tx(`正在下载 ${status.availableVersion || ""}${percent === null ? "" : ` · ${percent}%`}`, `Downloading ${status.availableVersion || ""}${percent === null ? "" : ` · ${percent}%`}`);
  else if (state === "ready") line = tx(`${status.availableVersion} 已下载好，重启即可更新`, `${status.availableVersion} is downloaded; restart to update`);
  else if (state === "waiting") line = waitingText(tx, status);
  else if (state === "available") line = tx(`发现新版本 ${status.availableVersion}`, `Version ${status.availableVersion} is available`);
  else if (state === "up_to_date") line = tx(`已是最新版本${status.lastCheck ? ` · 上次检查 ${formatTime(status.lastCheck, language)}` : ""}`, `Up to date${status.lastCheck ? ` · checked ${formatTime(status.lastCheck, language)}` : ""}`);
  else if (state === "error") line = updateErrorText(tx, status.lastError);
  else line = tx(...(states[state] || states.idle));

  const onUpdateHost = () => {
    if (canInstall(status)) void run(() => desktopUpdates.install());
    else if (status?.availableVersion && state === "available") void run(desktopUpdates.download);
    else void run(desktopUpdates.check);
  };

  const provenanceFineprint = (() => {
    if (!hasServices) {
      return tx(
        "更新包由 GitHub 官方发布页提供，并用内置公钥核对签名；签名不对的包一律不装。更新不会动你的任务记录、文件和设置。",
        "Updates come from the official GitHub release page and are checked against the key built into Ryn; anything else is refused. Updating keeps your tasks, files and settings.",
      );
    }
    const zhParts = services.map(svc => {
      const n = pickLocalizedLabel(svc.name, language, svc.id);
      const pub = pickLocalizedLabel(svc.publisher, language, "发布方服务器");
      const auth = pickLocalizedLabel(svc.auth_label, language, "");
      return `${n}的更新来自${pub}${auth ? `，用${auth}验证` : ""}。`;
    }).join("");
    const enParts = services.map(svc => {
      const n = pickLocalizedLabel(svc.name, language, svc.id);
      const pub = pickLocalizedLabel(svc.publisher, language, "their publisher's server");
      const auth = pickLocalizedLabel(svc.auth_label, language, "");
      return `${n} updates come from ${pub}${auth ? `, verified with your ${auth}` : ""}. `;
    }).join("");
    return tx(
      `Ryn 的更新来自 GitHub 官方发布页。${zhParts}两者都核对发布签名，签名不对的包一律不装。`,
      `Ryn updates come from the official GitHub release page. ${enParts}Both verify publisher signatures and refuse any untrusted package.`,
    );
  })();

  return <div className="ryn-update-panel ryn-desktop-updates">
    <section className="ryn-update-card">
      <span className="ryn-update-logo" aria-hidden="true">
        <svg viewBox="0 0 30 34"><path d="M2 3h15c15 0 16 18 3 21l9 10H18L8 22v12H2V15h14c5 0 5-5 0-5H2V3Z" fill="currentColor" /></svg>
      </span>
      <div className="ryn-update-card-copy">
        <strong>Ryn {status?.currentVersion || ""}</strong>
        <span role="status">{line}</span>
        {state === "downloading" && <progress aria-label={tx("下载进度", "Download progress")} max={status?.total || undefined} value={status?.total ? status.downloaded : undefined} />}
      </div>
      {state === "checking"
        ? <Button onClick={() => void run(desktopUpdates.cancel)}>{tx("取消检查", "Cancel check")}</Button>
        : canInstall(status)
        ? <Button variant="primary" disabled={working} onClick={() => void run(() => desktopUpdates.install())}>{tx("重启并更新", "Restart to update")}</Button>
        : status?.availableVersion && state === "available"
          ? <Button disabled={working} onClick={() => void run(desktopUpdates.download)}>{tx("下载更新", "Download update")}</Button>
          : <Button disabled={working || !status?.configured} onClick={() => void run(desktopUpdates.check)}>{tx("检查更新", "Check for updates")}</Button>}
    </section>
    {state === "checking" && <p role="status">{tx("正在连接更新服务，最多等待 20 秒。", "Connecting to the update service; this takes at most 20 seconds.")}</p>}
    {status && !status.configured && <p role="alert">{tx("此构建尚未配置官方更新签名公钥，暂不能下载安装更新。需要手动安装一次正式版本。", "This build does not have the official update signing key configured. Install an official version manually once.")}</p>}
    {state === "waiting" && <div className="ryn-update-actions">
      {status?.waitingFor !== "editors_open" && <Button disabled={working} onClick={() => void run(() => desktopUpdates.install(true))}>{tx("立即更新", "Update now")}</Button>}
      <Button disabled={working} onClick={() => void run(desktopUpdates.cancel)}>{tx("取消自动更新", "Cancel pending update")}</Button>
    </div>}
    {hasServices && <div className="ryn-service-updates">
      <h3 className="ryn-update-section-title">{tx("已安装的插件", "Installed plugins")}</h3>
      {services.map(row => <ServiceUpdateCard
        key={row.id}
        row={row}
        language={language}
        tx={tx}
        busy={Boolean(busyIds[row.id])}
        error={serviceErrors[row.id]}
        hostBusy={working || !status?.configured}
        onCheck={() => void runService(row.id, () => serviceUpdates.check(row.id))}
        onApply={() => void runService(row.id, () => serviceUpdates.apply(row.id))}
        onRetry={() => void runService(row.id, () => serviceUpdates.retry(row.id))}
        onUpdateHost={onUpdateHost}
      />)}
    </div>}
    <div className="ryn-update-settings">
      <label className="ryn-update-setting">
        <span><strong>{tx("自动下载更新", "Download updates automatically")}</strong>
          <small>{tx(
            "Ryn 和插件都在后台下载并核对签名。关掉后只能在这里手动检查。",
            "Ryn and plugins download and verify signatures in the background. Turn off to check manually here only.",
          )}</small></span>
        <input type="checkbox" checked={status?.autoDownload ?? true} disabled={working || !status?.configured}
          onChange={event => void run(() => desktopUpdates.preferences({ autoDownload: event.target.checked }))} />
      </label>
      <label className="ryn-update-setting">
        <span><strong>{tx("退出 Ryn 时自动安装", "Install when Ryn quits")}</strong>
          <small>{tx(
            "已下载的 Ryn 和插件更新在你退出 Ryn 时安装，下次打开就是新版本。",
            "Downloaded Ryn and plugin updates install when you quit Ryn, so it opens on the new version next time.",
          )}</small></span>
        <input type="checkbox" checked={status?.installOnQuit ?? true} disabled={working || !status?.configured}
          onChange={event => void run(() => desktopUpdates.preferences({ installOnQuit: event.target.checked }))} />
      </label>
      <div className="ryn-update-setting">
        <span><strong>{tx("更新说明", "Release notes")}</strong>
          <small className="ryn-update-notes">{status?.notes || tx("有新版本时显示。", "Shown when a new version is available.")}</small></span>
        <a href={RELEASES} target="_blank" rel="noreferrer">{tx("版本历史", "Version history")}</a>
      </div>
    </div>
    <p className="ryn-update-fineprint">{provenanceFineprint}</p>
    {error && <p role="alert">{updateErrorText(tx, error)}</p>}
  </div>;
}

export function PluginUpdatesPanel() {
  const tx = useText();
  const [status, setStatus] = useState<PluginUpdateStatus>({ plugins: [] });
  const [plugins, setPlugins] = useState<CraftPlugin[]>([]);
  const [id, setId] = useState("");
  const [source, setSource] = useState<PluginUpdateSource>({ kind: "public", url: "", public_key: "", auto_update: false });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    const refresh = async () => {
      try {
        const [updates, catalog] = await Promise.all([pluginUpdates.status(), craftPlugins.catalog()]);
        if (live) { setStatus(updates); setPlugins(catalog.plugins.filter(p => p.installed && p.web)); }
      } catch (e) { if (live) setError(message(e)); }
    };
    void refresh(); const timer = window.setInterval(() => void refresh(), 5000);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  async function run(action: () => Promise<PluginUpdateStatus>) {
    setBusy(true); setError("");
    try { setStatus(await action()); } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  function select(value: string) {
    setId(value);
    const existing = status.plugins.find(p => p.id === value);
    setSource(existing?.source || { kind: "public", url: `https://github.com/yeogirlyun/rynmesh/releases/latest/download/${value}-latest.json`, public_key: "", auto_update: false });
  }
  return <div className="ryn-update-panel">
    <h3>{tx("插件自动更新", "Plugin updates")}</h3>
    <p>{tx("公共插件订阅 Git Release，私有插件使用配置的 HTTPS 更新地址。内置适配器随 Ryn 更新。", "Public plugins use Git Releases; private plugins use their configured HTTPS update endpoints. Built-in adapters update with Ryn.")}</p>
    {!plugins.length && <p>{tx("安装可独立更新的插件后，可在这里配置订阅。", "Install a downloadable plugin to configure its update source here.")}</p>}
    {status.plugins.map(row => <div key={row.id} className="ryn-update-row">
      <strong>{plugins.find(p => p.id === row.id)?.name || row.id}</strong>
      <span>{row.current_version}{row.available_version ? ` → ${row.available_version}` : ""}</span>
      <span>{tx(...(states[row.state] || states.idle))}</span>
      <label><input type="checkbox" checked={row.source.auto_update} disabled={busy} onChange={e => void run(() => pluginUpdates.configure(row.id, { ...row.source, auto_update: e.target.checked }))} />{tx("空闲时自动更新", "Automatically update when idle")}</label>
      <div className="ryn-update-actions">
        <Button disabled={busy} onClick={() => void run(() => pluginUpdates.action(row.id, "check"))}>{tx("检查更新", "Check")}</Button>
        {row.available_version && <Button disabled={busy} onClick={() => void run(() => pluginUpdates.action(row.id, "apply"))}>{tx("更新", "Update")}</Button>}
        {row.can_rollback && <Button disabled={busy} onClick={() => void run(() => pluginUpdates.action(row.id, "rollback"))}>{tx("恢复上一版本", "Restore previous version")}</Button>}
        <Button disabled={busy} onClick={() => select(row.id)}>{tx("编辑更新源", "Edit source")}</Button>
      </div>
      {row.last_error && <p role="alert">{row.last_error}</p>}
    </div>)}
    {plugins.length > 0 && <form className="ryn-update-source" onSubmit={e => { e.preventDefault(); void run(() => pluginUpdates.configure(id, source)); }}>
      <label>{tx("选择插件", "Plugin")}<select value={id} disabled={busy} required onChange={e => select(e.target.value)}><option value="">—</option>{plugins.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      {id && <>
        <label>{tx("更新来源", "Source")}<select value={source.kind} disabled={busy} onChange={e => setSource({ ...source, kind: e.target.value as PluginUpdateSource["kind"], url: "", token_env: "" })}><option value="public">{tx("公共 Git Release", "Public Git Release")}</option><option value="company">{tx("公司后台 URL", "Company URL")}</option></select></label>
        <label>{tx("订阅地址", "Manifest URL")}<input type="url" required value={source.url} disabled={busy} onChange={e => setSource({ ...source, url: e.target.value })} /></label>
        <label>{tx("发布者公钥（由发布者提供）", "Publisher public key")}<input required value={source.public_key} disabled={busy} onChange={e => setSource({ ...source, public_key: e.target.value.trim() })} /></label>
        {source.kind === "company" && <label>{tx("凭证环境变量名（可选，由管理员配置）", "Credential environment variable (optional, administrator configured)")}<input placeholder={tx("RYN_PLUGIN_TOKEN_COMPANY", "RYN_PLUGIN_TOKEN_COMPANY")} value={source.token_env || ""} disabled={busy} onChange={e => setSource({ ...source, token_env: e.target.value })} /></label>}
        <label><input type="checkbox" checked={source.auto_update} disabled={busy} onChange={e => setSource({ ...source, auto_update: e.target.checked })} />{tx("允许后台下载，并在插件空闲时更新", "Allow background downloads and updates when idle")}</label>
        <Button type="submit" disabled={busy}>{tx("保存订阅", "Save source")}</Button>
      </>}
    </form>}
    {(error || status.last_error) && <p role="alert">{error || status.last_error}</p>}
  </div>;
}

/** Settings > Software updates: one level down from Settings, like Codex. */
export function SoftwareUpdatesPage() {
  const tx = useText();
  const desktop = isTauriDesktop();
  const { status: serviceStatus } = useServiceUpdates(desktop ? 5000 : 60000);
  const hasServices = desktop && serviceStatus.services.length > 0;
  return <div className="pf-page ryn-updates-page">
    <PageHeading title={tx("软件更新", "Software updates")}
      description={hasServices
        ? tx("Ryn 和已安装的插件都在后台下载新版本，不打断你。", "Ryn and your installed plugins download new versions in the background without interrupting you.")
        : tx("Ryn 在后台下载新版本；下好后点左上角「更新」即可。", "Ryn downloads new versions in the background; click Update next to the logo when one is ready.")} />
    {desktop ? <DesktopUpdatesPanel /> : <p>{tx("网页版随服务器更新，无需在这里操作。", "The web version updates with its server; nothing to do here.")}</p>}
  </div>;
}
