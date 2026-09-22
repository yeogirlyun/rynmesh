import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Check, Download, FolderOpen, Info, Monitor, Moon, Pause, Play, Power, RefreshCw, ShieldCheck, Zap } from "lucide-react";
import { getDesktopPreferences, getDesktopStatus, runDesktopAction, setDesktopPreferences, type DesktopAction, type DesktopPreferences, type DesktopStatus } from "../domain/desktopClient";
import { isTauriDesktop } from "../domain/nodeUrl";
import { DeviceArt, Modal, PageHeading } from "./components";
import "./desktop.css";

const initialPreferences: DesktopPreferences = { background: true, startup: false, startup_supported: true, silent_start: true, auto_recover: true, keep_awake: false, close_notice_seen: false };
const exampleStatus: DesktopStatus = { node_online: true, configured: true, sharing: true, active_tasks: 0, setup_active: false, recovery_attempts: 0, recovery: "healthy", awake_active: false, last_error: null };

function Toggle({ title, description, checked, disabled, onChange }: { title: string; description: string; checked: boolean; disabled: boolean; onChange: () => void }) {
  return <div className="pf-desktop-option"><div><h3>{title}</h3><p>{description}</p></div><button type="button" className={`pf-switch${checked ? " on" : ""}`} role="switch" aria-label={title} aria-checked={checked} disabled={disabled} onClick={onChange}><span /></button></div>;
}
function Group({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return <section className="pf-desktop-card"><header><span className="pf-desktop-glyph">{icon}</span><h2>{title}</h2></header>{children}</section>;
}
export function DesktopPage() {
  const demo = new URLSearchParams(window.location.search).get("client") === "fixture";
  const native = !demo && isTauriDesktop();
  const [prefs, setPrefs] = useState<DesktopPreferences | null>(demo ? initialPreferences : null);
  const [status, setStatus] = useState<DesktopStatus | null>(demo ? exampleStatus : null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState<"restart" | "quit" | null>(null);
  const [help, setHelp] = useState(false);
  const href = (path: string) => `${path}${demo ? "?client=fixture" : ""}`;
  useEffect(() => {
    if (!native) return;
    let active = true;
    getDesktopPreferences().then(p => { if (active) setPrefs(p); }).catch(() => { if (active) setError("Desktop preferences could not be loaded. Reopen this page to retry."); });
    const refresh = () => getDesktopStatus().then(s => { if (active) setStatus(s); }).catch(() => { if (active) { setStatus(null); setError("Desktop status is unavailable. Try reopening Ryn."); } });
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { active = false; clearInterval(timer); };
  }, [native]);
  const disabled = busy || !prefs || (!demo && !native);
  async function change(key: keyof DesktopPreferences) {
    if (!prefs) return;
    setBusy(true); setError(""); setNotice("");
    const next = { ...prefs, [key]: !prefs[key] };
    try {
      setPrefs(demo ? next : await setDesktopPreferences(next));
      if (demo) { setNotice("Preview preference updated. Windows settings are unchanged."); setStatus(s => s ? { ...s, awake_active: next.keep_awake && s.sharing } : s); }
    } catch { setError("Could not save this setting. Your previous preference is still shown."); }
    finally { setBusy(false); }
  }
  async function action(value: DesktopAction) {
    if (demo && (value === "restart" || value === "quit")) { setConfirmation(value); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      if (demo) {
        if (value === "pause" || value === "resume") setStatus(s => s ? { ...s, sharing: value === "resume", awake_active: value === "resume" && Boolean(prefs?.keep_awake) } : s);
        setNotice(value === "pause" ? "Preview: AI sharing paused. Current requests can finish." : value === "resume" ? "Preview: AI sharing enabled." : "Preview only. This action is available in the Windows app.");
      } else {
        const result = await runDesktopAction(value);
        setNotice(value === "diagnostics" ? `Diagnostics saved to ${result}` : result);
        setStatus(await getDesktopStatus());
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const title = !status || !status.recovery || status.recovery === "starting" ? "Starting this computer’s node" : !status.node_online ? "Node needs attention" : !status.configured ? "Your node is running" : status.sharing ? "AI sharing is enabled" : "AI sharing is paused";
  return <div className="pf-page pf-desktop">
    <PageHeading title="Desktop" description="A little setup. Always within reach."><button className="pf-button" onClick={() => setHelp(true)}><Info size={16} /> How it works</button></PageHeading>
    {!native && !demo && <div className="pf-desktop-message">Desktop controls are available in the installed Windows app.</div>}
    <section className="pf-desktop-overview">
      <div className="pf-desktop-device"><DeviceArt /></div>
      <div className="pf-desktop-overview-copy"><span className="pf-desktop-eyebrow">THIS COMPUTER</span><h2>{title}</h2><p>{status?.sharing ? "Your permitted devices can request AI from this node." : "Keep your computer connected, on your terms."}</p><div className="pf-desktop-badges"><span><i className={status?.node_online ? "online" : ""} />{status?.node_online ? "Node running" : "Node not ready"}</span><span>{status ? `${status.active_tasks} active AI tasks` : "Checking tasks"}</span><span>{status?.awake_active ? "Keeping system awake" : "System sleep allowed"}</span></div></div>
      <button className="pf-button" disabled={busy || !status?.node_online || !status.configured || (!demo && !native)} onClick={() => void action(status?.sharing ? "pause" : "resume")}>{status?.sharing ? <Pause size={15} /> : <Play size={15} />}{status?.sharing ? "Pause AI sharing" : "Resume AI sharing"}</button>
    </section>
    {(error || notice) && <div className={`pf-desktop-message${error ? " error" : ""}`} role={error ? "alert" : "status"}>{error ? <Info size={17} /> : <Check size={17} />}<span>{error || notice}</span></div>}
    <div className="pf-desktop-grid"><div className="pf-desktop-primary">
      <Group icon={<Power size={19} />} title="Startup & background">
        <Toggle title="Launch at sign-in" description="Start Ryn when you sign in to Windows." checked={prefs?.startup ?? false} disabled={disabled || !prefs?.startup_supported} onChange={() => void change("startup")} />
        <Toggle title="Start in the system tray" description="Keep the window out of the way at sign-in." checked={prefs?.silent_start ?? false} disabled={disabled || !prefs?.startup} onChange={() => void change("silent_start")} />
        <Toggle title="Keep running when closed" description="Closing the window keeps your node available." checked={prefs?.background ?? false} disabled={disabled} onChange={() => void change("background")} />
      </Group>
      <Group icon={<Zap size={19} />} title="Availability & recovery">
        <Toggle title="Keep awake while sharing AI" description="Prevent idle sleep while sharing or working. The screen can turn off." checked={prefs?.keep_awake ?? false} disabled={disabled || !prefs?.startup_supported} onChange={() => void change("keep_awake")} />
        <Toggle title="Recover the node automatically" description="Retry after a failure, with a limit of three attempts." checked={prefs?.auto_recover ?? false} disabled={disabled} onChange={() => void change("auto_recover")} />
        <div className="pf-desktop-recovery"><ShieldCheck size={16} /><span>{!status ? "Waiting for node status" : status.node_online ? "Node responding normally" : status.recovery === "starting" ? "Starting the local node…" : status.recovery === "needs_attention" ? "Automatic recovery stopped. Restart the node or open logs." : status.recovery === "disabled" ? "Automatic recovery is turned off." : status.recovery === "restarting" ? "Restarting the node…" : "Waiting to retry…"}</span><small>{status?.recovery_attempts ?? 0} / 3 retries</small></div>
      </Group>
    </div><aside className="pf-desktop-secondary">
      <Group icon={<Monitor size={19} />} title="At your fingertips">
        <p className="pf-desktop-copy">Look for Ryn in the Windows system tray. Your everyday controls live there, too.</p>
        <div className="pf-tray-illustration" aria-hidden="true"><div className="pf-tray-sheet"><span className="pf-tray-label"><i /> Ryn</span><span>Open Ryn <ArrowUpRight size={13} /></span><span>{status?.sharing ? "Pause AI sharing" : "Resume AI sharing"} <Pause size={12} /></span><span>Restart node <RefreshCw size={12} /></span><span>Quit Ryn <Power size={12} /></span></div><div className="pf-tray-taskbar"><span>⌃</span><b>R</b><span>▰</span><span>9:41</span></div></div>
        <small className="pf-desktop-caption">Tray menu illustration</small>
      </Group>
      <Group icon={<RefreshCw size={18} />} title="Maintenance">
        <div className="pf-desktop-actions">
          <button disabled={busy || (!native && !demo)} onClick={() => void action("restart")}><RefreshCw size={17} /><span>Restart node</span><ArrowUpRight size={14} /></button>
          <button disabled={busy || (!native && !demo)} onClick={() => void action("logs")}><FolderOpen size={17} /><span>Open logs</span><ArrowUpRight size={14} /></button>
          <button disabled={busy || (!native && !demo)} onClick={() => void action("diagnostics")}><Download size={17} /><span>Export diagnostics</span><ArrowUpRight size={14} /></button>
        </div><p className="pf-desktop-footnote">Diagnostics include app status only. Keys, invitations and conversations stay private.</p>
      </Group>
    </aside></div>
    <footer className="pf-desktop-footer"><span><Moon size={16} /> Available after Windows sign-in. Sleeping computers are unreachable.</span><button className="pf-link" disabled={busy || (!native && !demo)} onClick={() => void action("quit")}>Quit Ryn <Power size={15} /></button></footer>
    <div className="pf-desktop-bottom"><span>AI sharing permissions stay in your personal space.</span><Link to={href("/services/manage")}>Model & service setup <ArrowUpRight size={14} /></Link></div>
    {help && <Modal title="Your computer, ready when you are" onClose={() => setHelp(false)}><p>Launch Ryn at sign-in and keep it in the tray. Close the window to leave your node running, or choose Quit Ryn to disconnect.</p><p>Pause AI sharing stops new AI requests; current requests can finish. It does not pause other services or change who has access.</p><p>Automatic recovery restarts the Ryn node. It does not restart an external model app. Check Model & service setup if AI is unavailable.</p><p>Keep awake prevents idle system sleep. It does not override shutdown, manual sleep or laptop lid settings.</p><footer className="pf-dialog-actions"><button className="pf-button" onClick={() => setHelp(false)}>Got it</button></footer></Modal>}
    {confirmation && <Modal title={confirmation === "quit" ? "Quit Ryn?" : "Restart this node?"} onClose={() => setConfirmation(null)}><p>This will disconnect services on this computer. Running tasks may be interrupted.</p><p>This is a design preview. No Windows process will be changed.</p><footer className="pf-dialog-actions"><button className="pf-button" onClick={() => setConfirmation(null)}>Keep running</button><button className="pf-button primary" onClick={() => { setConfirmation(null); setNotice("Preview only. No Windows process was changed."); }}>{confirmation === "quit" ? "Quit Ryn" : "Restart node"}</button></footer></Modal>}
  </div>;
}
