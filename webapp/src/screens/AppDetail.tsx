import { useEffect, useId, useState } from "react";
import { Image, Package, Pin } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { APP_PINS_CHANGE_EVENT, isPinned, togglePin } from "../domain/apps";
import { IMAGE_STUDIO_SITE_ID } from "../domain/imageStudio";
import { NodeSiteError, nodeSites, type HostedSite, type SiteMember } from "../domain/nodeSites";
import { publicPlugins, type PublicCatalog, type PublicPlugin } from "../domain/publicPlugins";
import ImageStudioOwner from "./ImageStudioOwner";
import { PublicPluginFrame } from "./PublicPluginPage";
import "./nodeSites.css";
import "./publicPlugins.css";

type Translate = (zh: string, en: string) => string;
type PluginAction = "enable" | "disable" | "update" | "rollback" | "uninstall";

function pluginError(code: string, tx: Translate) {
  return ({
    plugin_busy: tx("插件有排队或执行中的任务，请完成或取消任务后重试。", "Finish or cancel active jobs before changing this plugin."),
    plugin_signature_invalid: tx("发布签名校验失败，未安装。请联系发布者。", "Publisher signature verification failed."),
    plugin_integrity_failed: tx("插件文件校验失败，未启动。", "Plugin files failed verification."),
    plugin_start_failed: tx("插件启动失败；更新时会恢复原版本。", "Plugin failed to start; updates restore the previous version."),
    plugin_not_enabled: tx("请先安装并启用插件。", "Install and enable the plugin first."),
    plugin_no_update: tx("当前没有更高版本可更新。", "No newer version is available."),
  }[code] || tx("连接公开插件源失败，请检查网络后重试。", "Could not reach the public plugin source. Check your connection and retry."));
}

function SharingPanel({ site, members, onUpdate, tx }: {
  site: HostedSite;
  members: SiteMember[];
  onUpdate: (site: HostedSite) => void;
  tx: Translate;
}) {
  const { t } = useTranslation();
  const labelId = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const studio = site.site_id === IMAGE_STUDIO_SITE_ID;
  const update = async (changes: Parameters<typeof nodeSites.update>[1]) => {
    setBusy(true);
    setError("");
    try {
      onUpdate(await nodeSites.update(site.site_id, changes));
    } catch (err) {
      const code = err instanceof NodeSiteError ? err.code : "site_unavailable";
      setError(code);
    } finally {
      setBusy(false);
    }
  };

  const { mode, members: selected } = site.audience;
  const audienceControl = <div className="ns-card ns-stat">
    <div role="radiogroup" aria-labelledby={labelId} style={{ display: "grid", gap: 4 }}>
      <p className="ns-muted" id={labelId}>{t("sites.studio.who")}</p>
      <label><input type="radio" name={labelId} checked={mode === "all"} disabled={busy}
        onChange={() => void update({ audience: { mode: "all", members: [] } })} />{t("sites.audienceAll")}</label>
      <label><input type="radio" name={labelId} checked={mode === "members"} disabled={busy}
        onChange={() => void update({ audience: { mode: "members", members: selected } })} />{t("sites.audienceMembers")}</label>
    </div>
    {mode === "members" && <fieldset className="ns-stat" disabled={busy} style={{ borderColor: "var(--border-color)", padding: 8 }}>
      <legend className="ns-muted">{t("sites.chooseMembers")}</legend>
      {!members.length && <p className="ns-muted">{t("sites.studio.noMembers")}</p>}
      {members.map(member => <label key={`${member.provider_resource}/${member.member_id}`}>
        <input type="checkbox" checked={selected.includes(member.member_id)} onChange={event => {
          const next = event.target.checked ? [...selected, member.member_id] : selected.filter(id => id !== member.member_id);
          void update({ audience: { mode: "members", members: next } });
        }} />{member.member_name || member.member_id}
      </label>)}
    </fieldset>}
  </div>;

  const sharing = tx("共享给同事", "Share with colleagues");
  return <>
    <div className="ns-card ns-mine">
      <div className="ns-mine-row">
        <div style={{ flex: "1 1 300px" }}>
          <strong>{sharing}</strong>
          <p className="ns-muted">{studio ? t("sites.studio.openHelp") :
            tx("打开后，同事可以从「应用 › 同事共享」使用这个应用。", "When enabled, colleagues can use this app from Apps › Shared by colleagues.")}</p>
        </div>
        <div className="ns-actions" style={{ alignItems: "center", color: site.enabled ? "var(--accent-green)" : "var(--text-secondary)" }}>
          {site.enabled ? tx("已共享", "Shared") : tx("未共享", "Not shared")}
          <button type="button" className={`pf-switch${site.enabled ? " on" : ""}`} role="switch" aria-label={sharing}
            aria-checked={site.enabled} disabled={busy} onClick={() => void update({ enabled: !site.enabled })}><span /></button>
        </div>
      </div>
      {error && <p className="ns-error" role="alert">{t(`sites.errors.${error}`, { defaultValue: t("sites.errors.site_unavailable") })}</p>}
    </div>
    {studio ? <ImageStudioOwner audienceControl={audienceControl} sharingEnabled={site.enabled} /> : audienceControl}
  </>;
}

function VersionPanel({ plugin, busy, run, tx }: {
  plugin: PublicPlugin;
  busy: boolean;
  run: (action: PluginAction) => void;
  tx: Translate;
}) {
  const [confirm, setConfirm] = useState<PluginAction | null>(null);
  const update = tx("更新", "Update");
  const rollback = tx("回退上一版", "Roll back");
  const uninstall = tx("卸载", "Uninstall");
  return <section className="ns-card ns-mine">
    <div className="ns-mine-row" style={{ gap: 24 }}>
      <div><p className="ns-muted">{tx("已安装", "Installed")}</p><strong>{plugin.installed_version || tx("未安装", "Not installed")}</strong></div>
      <div><p className="ns-muted">{tx("最新版本", "Latest")}</p><strong>{plugin.version}</strong></div>
    </div>
    <details open>
      <summary>{tx("更新说明与来源", "Release notes and source")}</summary>
      <p>{plugin.notes}</p>
      <p className="ns-muted">{tx("公开来源：老板的 rynmesh 仓库；更新不使用公司账号。", "Public source: the rynmesh repository; no company credentials are used.")}</p>
    </details>
    {plugin.installed_version && <div className="ns-mine-row">
      {plugin.update_available && <button className="ns-button is-primary" disabled={busy} onClick={() => setConfirm("update")}>{update}</button>}
      {plugin.rollback_available && <button className="ns-button" disabled={busy} onClick={() => setConfirm("rollback")}>{rollback}</button>}
      <button className="ns-button" disabled={busy} onClick={() => run(plugin.enabled ? "disable" : "enable")}>
        {plugin.enabled ? tx("停用", "Disable") : tx("启用", "Enable")}
      </button>
      <button className="ns-button" style={{ color: "var(--accent-red)" }} disabled={busy} onClick={() => setConfirm("uninstall")}>{uninstall}</button>
    </div>}
    {busy && <p role="status">{tx("正在处理，请稍候…", "Working…")}</p>}
    {confirm && <div className="public-plugin-confirm" role="dialog" aria-modal="true" aria-label={tx("确认插件操作", "Confirm plugin action")}>
      <h3>{plugin.name} · {confirm === "uninstall" ? uninstall : confirm === "rollback" ? rollback : `${update} ${plugin.version}`}</h3>
      <p>{confirm === "uninstall"
        ? tx("卸载会关闭本机插件和对外分享。作品和配置保留，重新安装后可继续使用。", "Uninstall stops the plugin and sharing. Images and settings remain available for reinstall.")
        : tx("此插件会在本机运行发布者的代码，调用本机登录的 Codex 并保存图片。安装和更新不会自动开启对外分享；已有分享设置在更新时保留。", "This plugin runs publisher code locally, uses your signed-in Codex and stores images. Installation does not enable sharing; updates preserve existing sharing settings.")}</p>
      <button className="ns-button" onClick={() => setConfirm(null)}>{tx("取消", "Cancel")}</button>
      <button className="ns-button is-primary" onClick={() => { setConfirm(null); run(confirm); }}>{tx("确认", "Confirm")}</button>
    </div>}
  </section>;
}

export default function AppDetail() {
  const { id = "" } = useParams();
  return <PublicAppDetail key={id} id={id} />;
}

function PublicAppDetail({ id }: { id: string }) {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  const tx: Translate = (cn, en) => zh ? cn : en;
  const [search, setSearch] = useSearchParams();
  const [catalog, setCatalog] = useState<PublicCatalog>();
  const [hosting, setHosting] = useState<{ sites: HostedSite[]; members: SiteMember[] }>();
  const [pin, setPin] = useState<boolean>();
  const [pinBusy, setPinBusy] = useState(false);
  const [pinError, setPinError] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [overviewError, setOverviewError] = useState(false);
  const [shareError, setShareError] = useState(false);
  const key = `local:${id}`;
  const detailPath = `/apps/app/public/${encodeURIComponent(id)}`;

  useEffect(() => {
    let active = true;
    publicPlugins.overview().then(value => { if (active) setCatalog(value); }).catch(() => { if (active) setOverviewError(true); });
    nodeSites.mine().then(value => { if (active) setHosting(value); }).catch(() => { if (active) setShareError(true); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const load = () => void isPinned(key).then(value => {
      if (active) { setPin(value); setPinError(false); }
    }).catch(() => { if (active) setPinError(true); });
    load();
    window.addEventListener(APP_PINS_CHANGE_EVENT, load);
    return () => { active = false; window.removeEventListener(APP_PINS_CHANGE_EVENT, load); };
  }, [key]);

  const plugin = catalog?.plugins.find(item => item.id === id);
  const site = hosting?.sites.find(item => item.site_id === id);
  const tabs = [
    { id: "use", label: tx("使用", "Use") },
    ...(site ? [{ id: "share", label: tx("共享给同事", "Share with colleagues") }] : []),
    { id: "version", label: tx("版本与更新", "Version and updates") },
  ];
  const requested = search.get("tab");
  const tab = tabs.find(item => item.id === requested)?.id || "use";

  const run = async (action: PluginAction) => {
    setBusy(true);
    setError("");
    try {
      setCatalog(await publicPlugins.action(id, action));
      // Enabling or disabling the plugin can register or unregister its site.
      try {
        setHosting(await nodeSites.mine());
        setShareError(false);
      } catch {
        setHosting(undefined);
        setShareError(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "plugin_download_failed");
    } finally {
      setBusy(false);
    }
  };
  const updatePin = async () => {
    if (!plugin) return;
    setPinBusy(true);
    setPinError(false);
    try {
      const { pins } = await togglePin({ kind: "local", key, app_id: id, title: plugin.name, open_path: detailPath });
      setPin(pins.some(item => item.key === key));
    } catch {
      setPinError(true);
    } finally {
      setPinBusy(false);
    }
  };

  const Icon = id === IMAGE_STUDIO_SITE_ID ? Image : Package;
  const versionText = [plugin?.installed_version ? tx("装在本机", "Installed locally") : tx("未安装", "Not installed"),
    plugin?.installed_version && `v${plugin.installed_version}`, tx("公开应用", "Public app")].filter(Boolean).join(" · ");
  const visibleError = error || plugin?.error || catalog?.error;
  return <section className="ns-page" style={{ maxWidth: 1280 }}>
    <div className="ns-muted">
      <Link to="/apps" style={{ color: "var(--accent-blue)" }}>{tx("应用", "Apps")}</Link>
      {plugin && ` / ${plugin.name}`}
    </div>
    {overviewError && <p className="ns-error" role="alert">{tx("当前 Ryn 未提供公开插件管理，请升级 Ryn。", "Upgrade Ryn to use public plugin management.")}</p>}
    {!plugin && !overviewError && <p className="ns-muted" role="status">{catalog ? tx("找不到这个应用。", "App unavailable.") : tx("正在加载应用…", "Loading app…")}</p>}
    {plugin && <>
      <header style={{ justifyContent: "initial", alignItems: "center", gap: 14 }}>
        <span className="ns-avatar" style={{ width: 52, height: 52, borderRadius: 13, color: "var(--accent-blue)", background: "var(--accent-blue-soft)" }}>
          <Icon size={24} aria-hidden="true" />
        </span>
        <div style={{ flex: "1 1 200px", minWidth: 0 }}>
          <h1>{plugin.name}</h1><p className="ns-muted">{versionText}</p>
        </div>
        {plugin.installed_version && <button className="ns-button" aria-pressed={pin === true} disabled={pinBusy || pin === undefined}
          onClick={() => void updatePin()} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <Pin size={15} color={pin ? "var(--accent-amber)" : undefined} fill={pin ? "currentColor" : "none"} aria-hidden="true" />
          {pin ? tx("已置顶", "Pinned") : tx("置顶", "Pin")}
        </button>}
      </header>
      {pinError && <p className="ns-error" role="alert">{tx("无法读取或保存置顶，请稍后重试。", "Could not load or save pinned apps. Try again later.")}</p>}
      <nav aria-label={tx("应用详情", "App details")} style={{ display: "flex", flexWrap: "wrap", gap: 4, borderBottom: "1px solid var(--border-color)" }}>
        {tabs.map(item => <button key={item.id} type="button" aria-current={item.id === tab ? "page" : undefined}
          onClick={() => setSearch(previous => { const next = new URLSearchParams(previous); next.set("tab", item.id); return next; })}
          style={{
            background: "none", padding: "10px 14px", cursor: "pointer", border: 0, font: "inherit",
            fontWeight: item.id === tab ? 600 : 400,
            color: item.id === tab ? "var(--text-primary)" : "var(--text-secondary)",
            borderBottom: `2px solid ${item.id === tab ? "var(--accent-blue)" : "transparent"}`,
          }}>{item.label}</button>)}
      </nav>
      {visibleError && <p className="ns-error" role="alert">{pluginError(visibleError, tx)}</p>}
      {requested === "share" && !site && shareError && <p className="ns-error" role="alert">
        {tx("无法读取共享设置，请重新打开这个应用。", "Could not load sharing settings. Reopen this app to try again.")}
      </p>}
      {tab === "use" && (requested !== "share" || hosting || shareError) && <PublicPluginFrame pluginId={id} />}
      {tab === "share" && site && hosting && <SharingPanel site={site} members={hosting.members}
        tx={tx} onUpdate={value => setHosting(current => current && ({
          ...current, sites: current.sites.map(item => item.site_id === value.site_id ? value : item),
        }))} />}
      {tab === "version" && <VersionPanel plugin={plugin} busy={busy} run={action => void run(action)} tx={tx} />}
    </>}
  </section>;
}
