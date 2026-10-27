import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Cloud, HardDrive, ExternalLink } from "lucide-react";
import { nasChanged, nasJson, nasRequest, useNasStatus, type NasSource, type NasStatus } from "../domain/nas";
import { usePersonal } from "./model";
import { Modal } from "./components";
import { AddNas, NasBrowser } from "./Nas";
import { DrivePreview } from "./DrivePreview";
import { openDriveBrowser } from "../domain/driveBrowser";
import { isTauriDesktop } from "../domain/nodeUrl";
import "./drives.css";

type Provider = "google" | "baidu" | "onedrive";
type Authorization = { id: string; url: string; user_code?: string };

function DriveBrowserLink({ url, authorization = false }: { url: string; authorization?: boolean }) {
  const { t } = useTranslation();
  const [state, setState] = useState<"idle" | "opening" | "requested" | "failed">("idle");
  const autoOpened = useRef(false);
  async function open() {
    setState("opening");
    try { await openDriveBrowser(url); setState("requested"); }
    catch { setState("failed"); }
  }
  useEffect(() => {
    if (authorization && isTauriDesktop() && !autoOpened.current) {
      autoOpened.current = true;
      void open();
    }
  }, [url, authorization]);
  return <div className="drive-browser-link">
    <a className="pf-button" href={url} target="_blank" rel="noopener noreferrer" aria-disabled={state === "opening"}
      onClick={event => { event.preventDefault(); if (state !== "opening") void open(); }}>
      <ExternalLink size={16} />{t(state === "opening" ? "drive.openingBrowser" : authorization ? "drive.openAuthorization" : "drive.openConsole")}
    </a>
    {state === "requested" && <p role="status">{t(authorization ? "drive.authorizationBrowserNext" : "drive.consoleBrowserNext")}</p>}
    {state === "failed" && <p role="alert">{t("drive.browserOpenFailed")}</p>}
    {(authorization || state === "requested" || state === "failed") && <label>{t(authorization ? "drive.authorizationLink" : "drive.consoleLink")}<input readOnly value={url} onFocus={event => event.currentTarget.select()} /></label>}
  </div>;
}

export function DrivesPage() {
  const { demo } = usePersonal();
  return demo ? <DrivePreview /> : <LiveDrives />;
}

function LiveDrives() {
  const { t } = useTranslation();
  const { status, error } = useNasStatus();
  const [adding, setAdding] = useState(false);
  const [local, setLocal] = useState(false);
  const [editing, setEditing] = useState<NasSource | null>(null);
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  if (!status) return <p role={error ? "alert" : "status"}>{error || t("personal.loadingPlugin")}</p>;
  return <div className="drive-live screen-stack">
    {!status.enabled ? <section className="nas-empty"><h1>{t("drive.title")}</h1><p>{t("drive.liveIntro")}</p><button className="pf-button primary" disabled={busy} onClick={async () => {
      setBusy(true); setFailure("");
      try { await nasJson("", { enabled: true }, "PUT"); nasChanged(); }
      catch (e) { setFailure((e as Error).message); }
      finally { setBusy(false); }
    }}>{t("drive.enableDrives")}</button>{failure && <p role="alert">{failure}</p>}</section> :
      <NasBrowser status={status} onAdd={() => setAdding(true)} onSettings={setEditing} />}
    {adding && <ConnectDrive status={status} onClose={() => setAdding(false)} onLocal={() => { setAdding(false); setLocal(true); }} />}
    {local && <AddNas status={status} onClose={() => setLocal(false)} onAdded={() => { setLocal(false); nasChanged(); }} />}
    {editing && <DriveSettings source={editing} status={status} onClose={() => setEditing(null)} />}
  </div>;
}

function ConnectDrive({ status, onClose, onLocal, source }: { status: NasStatus; onClose: () => void; onLocal?: () => void; source?: NasSource }) {
  const { t, i18n } = useTranslation();
  const [provider, setProvider] = useState<Provider>(source?.protocol === "onedrive" ? "onedrive" : source?.protocol === "google" ? "google" : "baidu");
  const config = status.cloud_providers?.find(p => p.id === provider);
  const [name, setName] = useState(source?.name || "");
  const [writable, setWritable] = useState(source ? Boolean(source.writable) : true);
  const [ai, setAi] = useState(source ? Boolean(source.allow_ai) : true);
  const [clientId, setClientId] = useState(config?.client_id || "");
  const [secret, setSecret] = useState("");
  const [editConfig, setEditConfig] = useState(!config?.configured);
  const [auth, setAuth] = useState<Authorization | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pendingId = useRef("");
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => {
    alive.current = false;
    if (pendingId.current) void nasRequest(`/cloud/auth-status/${pendingId.current}`, { method: "DELETE" }).catch(() => {});
  }; }, []);
  useEffect(() => {
    if (!auth) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await nasJson<{ state: string; error?: string }>(`/cloud/auth-status/${auth.id}`);
        if (stopped) return;
        if (result.state === "connected") { pendingId.current = ""; nasChanged(); onClose(); return; }
        if (["failed", "cancelled"].includes(result.state)) throw new Error(t(`drive.errors.${result.error || "drive_authorization_failed"}`, { defaultValue: t("drive.authorizationFailed") }));
        timer = setTimeout(poll, provider !== "google" ? 5000 : 1500);
      } catch (e) { if (!stopped) { setError((e as Error).message); setAuth(null); } }
    };
    timer = setTimeout(poll, 1500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [auth, provider, onClose, t]);
  const changeProvider = (next: Provider) => {
    setProvider(next); setClientId(status.cloud_providers?.find(p => p.id === next)?.client_id || "");
    setEditConfig(!status.cloud_providers?.find(p => p.id === next)?.configured); setSecret(""); setError("");
  };
  async function connect(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (editConfig) await nasJson(`/cloud/apps/${provider}`, { client_id: clientId.trim(), client_secret: secret }, "PUT");
      setSecret("");
      if (pendingId.current) await nasRequest(`/cloud/auth-status/${pendingId.current}`, { method: "DELETE" });
      const result = await nasJson<Authorization>(`/cloud/auth/${provider}`, { name: name.trim() || t(`drive.${provider}Name`), writable, allow_ai: ai, source_id: source?.id, language: i18n.resolvedLanguage || i18n.language });
      if (!alive.current) { await nasRequest(`/cloud/auth-status/${result.id}`, { method: "DELETE" }); return; }
      pendingId.current = result.id; setAuth(result);
    } catch (e) { if (alive.current) setError((e as Error).message); }
    finally { if (alive.current) setBusy(false); }
  }
  return <Modal title={source ? t("drive.reauthorize") : t("drive.add")} onClose={onClose}>
    {auth ? <div className="drive-connect-form"><h3>{t("drive.finishAuthorization")}</h3><p>{t("drive.browserHint")}</p>
      {auth.user_code && <p>{t("drive.deviceCode")} <strong className="drive-auth-code">{auth.user_code}</strong></p>}
      <DriveBrowserLink key={auth.id} url={auth.url} authorization />
      <p role="status">{t("drive.waitingAuthorization")}</p><button className="pf-button" onClick={onClose}>{t("personal.cancel")}</button>
    </div> : <form className="drive-connect-form" onSubmit={connect}>
      {!source && <div className="drive-provider-options">
        {onLocal && <button type="button" disabled={busy} onClick={onLocal}><HardDrive size={22} />NAS</button>}
        {(["baidu", "google", "onedrive"] as const).map(p => <button type="button" disabled={busy} aria-pressed={provider === p} key={p} onClick={() => changeProvider(p)}><Cloud size={22} />{t(`drive.${p}Name`)}</button>)}
      </div>}
      <details open={!config?.configured} className="drive-setup-guide"><summary>{t("drive.setupGuide")}</summary>
        <ol>{[1, 2, 3].map(n => <li key={n}>{t(`drive.${provider}Step${n}`)}</li>)}</ol>
        <DriveBrowserLink key={provider} url={provider === "onedrive" ? "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" : provider === "google" ? "https://console.cloud.google.com/apis/library/drive.googleapis.com" : "https://pan.baidu.com/union/console/applist"} />
        <p>{t(provider === "onedrive" ? "drive.onedriveNotes" : provider === "google" ? "drive.googleTesting" : "drive.baiduLimits")}</p>
      </details>
      {config?.configured && <label className="drive-checkbox"><input type="checkbox" checked={editConfig} disabled={busy} onChange={e => setEditConfig(e.target.checked)} />{t("drive.changeApp")}</label>}
      {editConfig && <>
        <label>{t(`drive.${provider}ClientId`)}<input required autoComplete="off" value={clientId} disabled={busy} onChange={e => setClientId(e.target.value)} /></label>
        {provider !== "onedrive" && <label>{t(`drive.${provider}ClientSecret`)}<input type="password" autoComplete="new-password" required={!config?.configured && provider === "baidu"} value={secret} disabled={busy} onChange={e => setSecret(e.target.value)} placeholder={config?.configured ? t("drive.keepSecret") : ""} /></label>}
        <small>{t("drive.localCredentials")}</small>
      </>}
      <label>{t("drive.connectionName")}<input value={name} maxLength={80} disabled={busy} placeholder={t(`drive.${provider}Name`)} onChange={e => setName(e.target.value)} /></label>
      <label className="drive-checkbox"><input type="checkbox" disabled={busy} checked={writable} onChange={e => setWritable(e.target.checked)} />{t("drive.allowWrite")}</label>
      <label className="drive-checkbox"><input type="checkbox" disabled={busy} checked={ai} onChange={e => setAi(e.target.checked)} />{t("drive.allowSelectedAI")}</label>
      {error && <p role="alert">{error}</p>}
      <button className="pf-button primary" disabled={busy}>{t(busy ? "personal.working" : "drive.startAuthorization")}</button>
    </form>}
  </Modal>;
}

function DriveSettings({ source, status, onClose }: { source: NasSource; status: NasStatus; onClose: () => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState(source.name);
  const [writable, setWritable] = useState(source.writable);
  const [ai, setAi] = useState(source.allow_ai);
  const [reauth, setReauth] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (reauth) return <ConnectDrive status={status} source={{ ...source, name, writable, allow_ai: ai }} onClose={onClose} />;
  return <Modal title={t("drive.connectionSettings")} onClose={onClose}><form className="drive-connect-form" onSubmit={async event => {
    event.preventDefault(); setBusy(true); setError("");
    if (writable && ["google", "onedrive"].includes(source.protocol) && !source.write_authorized) { setReauth(true); return; }
    try { await nasJson(`/sources/${source.id}`, { name, writable, allow_ai: ai }, "PATCH"); nasChanged(); onClose(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }}>
    <label>{t("drive.connectionName")}<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} /></label>
    <label className="drive-checkbox"><input type="checkbox" checked={writable} onChange={e => setWritable(e.target.checked)} />{t("drive.allowWrite")}</label>
    <label className="drive-checkbox"><input type="checkbox" checked={ai} onChange={e => setAi(e.target.checked)} />{t("drive.allowSelectedAI")}</label>
    {error && <p role="alert">{error}</p>}
    <button className="pf-button primary" disabled={busy}>{t("personal.saveChanges")}</button>
    {["google", "baidu", "onedrive"].includes(source.protocol) && <button type="button" className="pf-button" disabled={busy} onClick={() => setReauth(true)}>{t("drive.reauthorize")}</button>}
  </form></Modal>;
}
