import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
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
    getDesktopPreferences().then(p => { if (active) setPrefs(p); }).catch(() => { if (active) setError(t("personal.desktopPreferencesCouldNotBeLoadedReopenThisPageToRetry")); });
    const refresh = () => getDesktopStatus().then(s => { if (active) setStatus(s); }).catch(() => { if (active) { setStatus(null); setError(t("personal.desktopStatusIsUnavailableTryReopeningRyn")); } });
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { active = false; clearInterval(timer); };
  }, [native, t]);
  const disabled = busy || !prefs || (!demo && !native);
  async function change(key: keyof DesktopPreferences) {
    if (!prefs) return;
    setBusy(true); setError(""); setNotice("");
    const next = { ...prefs, [key]: !prefs[key] };
    try {
      setPrefs(demo ? next : await setDesktopPreferences(next));
      if (demo) { setNotice(t("personal.previewPreferenceUpdatedWindowsSettingsAreUnchanged")); setStatus(s => s ? { ...s, awake_active: next.keep_awake && s.sharing } : s); }
    } catch { setError(t("personal.couldNotSaveThisSettingYourPreviousPreferenceIsStillShown")); }
    finally { setBusy(false); }
  }
  async function action(value: DesktopAction) {
    if (demo && (value === "restart" || value === "quit")) { setConfirmation(value); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      if (demo) {
        if (value === "pause" || value === "resume") setStatus(s => s ? { ...s, sharing: value === "resume", awake_active: value === "resume" && Boolean(prefs?.keep_awake) } : s);
        setNotice(value === "pause" ? t("personal.previewAISharingPausedCurrentRequestsCanFinish") : value === "resume" ? t("personal.previewAISharingEnabled") : t("personal.previewOnlyThisActionIsAvailableInTheWindowsApp"));
      } else {
        const result = await runDesktopAction(value);
        setNotice(value === "diagnostics" ? t("personal.diagnosticsSavedToPath", { path: result }) : result);
        setStatus(await getDesktopStatus());
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const title = !status || !status.recovery || status.recovery === "starting" ? t("personal.startingThisComputersNode") : !status.node_online ? t("personal.nodeNeedsAttention") : !status.configured ? t("personal.yourNodeIsRunning") : status.sharing ? t("personal.aiSharingIsEnabled") : t("personal.aiSharingIsPaused");
  return <div className="pf-page pf-desktop">
    <PageHeading title={t("nav.desktop")} description={t("personal.aLittleSetupAlwaysWithinReach")}><button className="pf-button" onClick={() => setHelp(true)}><Info size={16} /> {t("personal.howItWorks")}</button></PageHeading>
    {!native && !demo && <div className="pf-desktop-message">{t("personal.desktopControlsAreAvailableInTheInstalledWindowsApp")}</div>}
    <section className="pf-desktop-overview">
      <div className="pf-desktop-device"><DeviceArt /></div>
      <div className="pf-desktop-overview-copy"><span className="pf-desktop-eyebrow">{t("personal.thisCOMPUTER")}</span><h2>{title}</h2><p>{status?.sharing ? t("personal.yourPermittedDevicesCanRequestAIFromThisNode") : t("personal.keepYourComputerConnectedOnYourTerms")}</p><div className="pf-desktop-badges"><span><i className={status?.node_online ? "online" : ""} />{status?.node_online ? t("personal.nodeRunning") : t("personal.nodeNotReady")}</span><span>{status ? t("personal.countActiveAITasks", { count: status.active_tasks }) : t("personal.checkingTasks")}</span><span>{status?.awake_active ? t("personal.keepingSystemAwake") : t("personal.systemSleepAllowed")}</span></div></div>
      <button className="pf-button" disabled={busy || !status?.node_online || !status.configured || (!demo && !native)} onClick={() => void action(status?.sharing ? "pause" : "resume")}>{status?.sharing ? <Pause size={15} /> : <Play size={15} />}{status?.sharing ? t("personal.pauseAISharing") : t("personal.resumeAISharing")}</button>
    </section>
    {(error || notice) && <div className={`pf-desktop-message${error ? " error" : ""}`} role={error ? "alert" : "status"}>{error ? <Info size={17} /> : <Check size={17} />}<span>{error || notice}</span></div>}
    <div className="pf-desktop-grid"><div className="pf-desktop-primary">
      <Group icon={<Power size={19} />} title={t("personal.startupBackground")}>
        <Toggle title={t("personal.launchAtSignin")} description={t("personal.startRynWhenYouSignInToWindows")} checked={prefs?.startup ?? false} disabled={disabled || !prefs?.startup_supported} onChange={() => void change("startup")} />
        <Toggle title={t("personal.startInTheSystemTray")} description={t("personal.keepTheWindowOutOfTheWayAtSignin")} checked={prefs?.silent_start ?? false} disabled={disabled || !prefs?.startup} onChange={() => void change("silent_start")} />
        <Toggle title={t("personal.keepRunningWhenClosed")} description={t("personal.closingTheWindowKeepsYourNodeAvailable")} checked={prefs?.background ?? false} disabled={disabled} onChange={() => void change("background")} />
      </Group>
      <Group icon={<Zap size={19} />} title={t("personal.availabilityRecovery")}>
        <Toggle title={t("personal.keepAwakeWhileSharingAI")} description={t("personal.preventIdleSleepWhileSharingOrWorkingTheScreenCanTurnOff")} checked={prefs?.keep_awake ?? false} disabled={disabled || !prefs?.startup_supported} onChange={() => void change("keep_awake")} />
        <Toggle title={t("personal.recoverTheNodeAutomatically")} description={t("personal.retryAfterAFailureWithALimitOfThreeAttempts")} checked={prefs?.auto_recover ?? false} disabled={disabled} onChange={() => void change("auto_recover")} />
        <div className="pf-desktop-recovery"><ShieldCheck size={16} /><span>{!status ? t("personal.waitingForNodeStatus") : status.node_online ? t("personal.nodeRespondingNormally") : status.recovery === "starting" ? t("personal.startingTheLocalNode") : status.recovery === "needs_attention" ? t("personal.automaticRecoveryStoppedRestartTheNodeOrOpenLogs") : status.recovery === "disabled" ? t("personal.automaticRecoveryIsTurnedOff") : status.recovery === "restarting" ? t("personal.restartingTheNode") : t("personal.waitingToRetry")}</span><small>{t("personal.count3Retries", { count: status?.recovery_attempts ?? 0 })}</small></div>
      </Group>
    </div><aside className="pf-desktop-secondary">
      <Group icon={<Monitor size={19} />} title={t("personal.atYourFingertips")}>
        <p className="pf-desktop-copy">{t("personal.lookForRynInTheWindowsSystemTrayYourEverydayControlsLiveThereToo")}</p>
        <div className="pf-tray-illustration" aria-hidden="true"><div className="pf-tray-sheet"><span className="pf-tray-label"><i /> Ryn</span><span>{t("personal.openRyn")} <ArrowUpRight size={13} /></span><span>{status?.sharing ? t("personal.pauseAISharing") : t("personal.resumeAISharing")} <Pause size={12} /></span><span>{t("personal.restartNode")} <RefreshCw size={12} /></span><span>{t("personal.quitRyn")} <Power size={12} /></span></div><div className="pf-tray-taskbar"><span>⌃</span><b>R</b><span>▰</span><span>9:41</span></div></div>
        <small className="pf-desktop-caption">{t("personal.trayMenuIllustration")}</small>
      </Group>
      <Group icon={<RefreshCw size={18} />} title={t("personal.maintenance")}>
        <div className="pf-desktop-actions">
          <button disabled={busy || (!native && !demo)} onClick={() => void action("restart")}><RefreshCw size={17} /><span>{t("personal.restartNode")}</span><ArrowUpRight size={14} /></button>
          <button disabled={busy || (!native && !demo)} onClick={() => void action("logs")}><FolderOpen size={17} /><span>{t("personal.openLogs")}</span><ArrowUpRight size={14} /></button>
          <button disabled={busy || (!native && !demo)} onClick={() => void action("diagnostics")}><Download size={17} /><span>{t("personal.exportDiagnostics")}</span><ArrowUpRight size={14} /></button>
        </div><p className="pf-desktop-footnote">{t("personal.diagnosticsIncludeAppStatusOnlyKeysInvitationsAndConversationsStayPrivate")}</p>
      </Group>
    </aside></div>
    <footer className="pf-desktop-footer"><span><Moon size={16} /> {t("personal.availableAfterWindowsSigninSleepingComputersAreUnreachable")}</span><button className="pf-link" disabled={busy || (!native && !demo)} onClick={() => void action("quit")}>{t("personal.quitRyn")} <Power size={15} /></button></footer>
    <div className="pf-desktop-bottom"><span>{t("personal.aiSharingPermissionsStayInYourPersonalSpace")}</span><Link to={href("/services/manage")}>{t("personal.modelServiceSetup")} <ArrowUpRight size={14} /></Link></div>
    {help && <Modal title={t("personal.yourComputerReadyWhenYouAre")} onClose={() => setHelp(false)}><p>{t("personal.launchRynAtSigninAndKeepItInTheTrayCloseTheWindowToLeaveYourNodeRunningOrChooseQuitRynToDisconnect")}</p><p>{t("personal.pauseAISharingStopsNewAIRequestsCurrentRequestsCanFinishItDoesNotPauseOtherServicesOrChangeWhoHasAccess")}</p><p>{t("personal.automaticRecoveryRestartsTheRynNodeItDoesNotRestartAnExternalModelAppCheckModelServiceSetupIfAIIsUnavailable")}</p><p>{t("personal.keepAwakePreventsIdleSystemSleepItDoesNotOverrideShutdownManualSleepOrLaptopLidSettings")}</p><footer className="pf-dialog-actions"><button className="pf-button" onClick={() => setHelp(false)}>{t("personal.gotIt")}</button></footer></Modal>}
    {confirmation && <Modal title={confirmation === "quit" ? t("personal.quitRynLabel") : t("personal.restartThisNode")} onClose={() => setConfirmation(null)}><p>{t("personal.thisWillDisconnectServicesOnThisComputerRunningTasksMayBeInterrupted")}</p><p>{t("personal.thisIsADesignPreviewNoWindowsProcessWillBeChanged")}</p><footer className="pf-dialog-actions"><button className="pf-button" onClick={() => setConfirmation(null)}>{t("personal.keepRunning")}</button><button className="pf-button primary" onClick={() => { setConfirmation(null); setNotice(t("personal.previewOnlyNoWindowsProcessWasChanged")); }}>{confirmation === "quit" ? t("personal.quitRyn") : t("personal.restartNode")}</button></footer></Modal>}
  </div>;
}
