import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../components/ui";
import { useUILanguage } from "../uiI18n";
import { desktopUpdates, pluginUpdates, type DesktopUpdateStatus, type PluginUpdateSource, type PluginUpdateStatus } from "../domain/updates";
import { craftPlugins, type CraftPlugin } from "../domain/craftPlugins";
import "./UpdatesPanel.css";

function useText() {
  const language = useUILanguage();
  return (zh: string, en: string) => language.startsWith("zh") ? zh : en;
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
const states: Record<string, [string, string]> = {
  idle: ["尚未检查", "Not checked"], checking: ["正在检查", "Checking"], available: ["发现新版本", "Update available"],
  up_to_date: ["已是最新兼容版本", "Up to date"], downloading: ["正在下载并验证", "Downloading and verifying"],
  ready: ["已准备好，重启后更新", "Ready to restart and update"], installing: ["正在安装", "Installing"],
  updated: ["更新完成", "Updated"], waiting_for_idle: ["等待编辑器关闭；关闭后重启 Ryn 即可释放插件", "Waiting for the editor to close; restart Ryn after closing it"],
  rolled_back: ["已恢复上一版本", "Previous version restored"], error: ["更新失败，可重试", "Update failed; retry available"],
};

export function DesktopUpdateNotice() {
  const tx = useText();
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const refresh = () => desktopUpdates.status().then(s => { if (live) setVersion(s.state === "ready" ? s.availableVersion : null); }).catch(() => {});
    void refresh(); const timer = window.setInterval(() => void refresh(), 30000);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  if (!version) return null;
  return <div className="ryn-update-notice" role="status"><span>{tx(`Ryn ${version} 已准备好，可重启更新。`, `Ryn ${version} is ready to install.`)}</span>
    <Link to="/settings/advanced?section=updates">{tx("查看更新", "Review update")}</Link></div>;
}

export function DesktopUpdatesPanel() {
  const tx = useText();
  const [status, setStatus] = useState<DesktopUpdateStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmInstall, setConfirmInstall] = useState(false);
  useEffect(() => {
    let live = true;
    const refresh = () => desktopUpdates.status().then(value => { if (live) setStatus(value); })
      .catch(e => { if (live) setError(message(e)); });
    void refresh(); const timer = window.setInterval(() => void refresh(), 1500);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  async function run(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); setStatus(await desktopUpdates.status()); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  const working = busy || ["checking", "downloading", "installing"].includes(status?.state || "");
  return <div className="ryn-update-panel">
    <h3>{tx("Ryn 软件更新", "Ryn software updates")}</h3>
    <p>{tx("当前版本", "Current version")}: {status?.currentVersion || "…"}
      {status?.availableVersion ? ` → ${status.availableVersion}` : ""}</p>
    <a href="https://github.com/yeogirlyun/rynmesh/releases" target="_blank" rel="noreferrer">GitHub · yeogirlyun/rynmesh</a>
    <p role="status">{tx(...(states[status?.state || "idle"] || states.idle))}</p>
    {status && !status.configured && <p role="alert">{tx("此构建尚未配置官方更新签名公钥，暂不能下载安装更新。", "This build does not have the official update signing key configured.")}</p>}
    <label className="toggle-row"><input type="checkbox" checked={status?.autoDownload ?? true} disabled={working || !status?.configured}
      onChange={event => void run(() => desktopUpdates.preferences(event.target.checked))} />
      {tx("自动检查并下载；安装前由我确认重启", "Automatically check and download; ask before restarting")}</label>
    {status?.state === "downloading" && <progress aria-label={tx("下载进度", "Download progress")} max={status.total || undefined} value={status.total ? status.downloaded : undefined} />}
    {status?.notes && <p className="ryn-update-notes">{status.notes}</p>}
    <div className="ryn-update-actions">
      <Button disabled={working || !status?.configured} onClick={() => void run(desktopUpdates.check)}>{tx("检查更新", "Check for updates")}</Button>
      {status?.availableVersion && status.state !== "ready" && <Button disabled={working} onClick={() => void run(desktopUpdates.download)}>{tx("下载更新", "Download update")}</Button>}
      {status?.state === "ready" && <Button disabled={working} onClick={() => setConfirmInstall(true)}>{tx("重启并更新", "Restart and update")}</Button>}
    </div>
    {confirmInstall && <div role="alertdialog" aria-label={tx("安装更新", "Install update")}>
      <p>{tx("请保存文档并结束 Agent、传输和共享服务任务。确认后 Ryn 将退出并安装新版。", "Save your documents and finish Agent, transfer and shared service tasks. Ryn will exit and install the update.")}</p>
      <Button disabled={working} onClick={() => void run(desktopUpdates.install)}>{tx("已保存，立即安装", "Saved — install now")}</Button>
      <Button disabled={working} onClick={() => setConfirmInstall(false)}>{tx("稍后", "Later")}</Button>
    </div>}
    {(error || status?.lastError) && <p role="alert">{error || status?.lastError}</p>}
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
    <p>{tx("公共插件订阅 Git Release，公司插件订阅公司后台 HTTPS 地址。内置业务插件随 Ryn 更新。", "Public plugins use Git Releases; company plugins use company HTTPS endpoints. Built-in adapters update with Ryn.")}</p>
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
        {source.kind === "company" && <label>{tx("凭证环境变量名（可选，由管理员配置）", "Credential environment variable (optional, administrator configured)")}<input placeholder="RYN_PLUGIN_TOKEN_COMPANY" value={source.token_env || ""} disabled={busy} onChange={e => setSource({ ...source, token_env: e.target.value })} /></label>}
        <label><input type="checkbox" checked={source.auto_update} disabled={busy} onChange={e => setSource({ ...source, auto_update: e.target.checked })} />{tx("允许后台下载，并在插件空闲时更新", "Allow background downloads and updates when idle")}</label>
        <Button type="submit" disabled={busy}>{tx("保存订阅", "Save source")}</Button>
      </>}
    </form>}
    {(error || status.last_error) && <p role="alert">{error || status.last_error}</p>}
  </div>;
}
