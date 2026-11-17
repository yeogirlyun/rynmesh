use crate::{node, preferences::DesktopState};
use serde::{Deserialize, Serialize};
use std::sync::{Mutex, atomic::{AtomicBool, Ordering}};
use std::time::{Duration, Instant};
use tauri::Manager;
use crate::localization::tr;

#[cfg(test)]
#[path = "ovh_s13_i6.rs"]
mod ovh_s13_i6;

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct RuntimeStatus {
    pub node_online: bool,
    pub configured: bool,
    pub sharing: bool,
    pub active_tasks: u64,
    pub agent_tasks: u64,
    pub agent_active_runs: u64,
    pub agent_waiting: u64,
    pub agent_task_titles: Vec<String>,
    pub setup_active: bool,
    pub recovery_attempts: u8,
    pub recovery: String,
    pub awake_active: bool,
    pub last_error: Option<String>,
}
#[derive(Default)]
pub struct RuntimeState {
    pub status: Mutex<RuntimeStatus>,
    pub reset_recovery: AtomicBool,
    pub action_busy: AtomicBool,
}

const LOW_FREQ_RECOVERY_SECS: u64 = 600;
pub const NOTIFY_NEEDS_ATTENTION_KEY: &str =
    "Ryn node could not start. Retrying periodically; open logs from the tray.";
pub const NOTIFY_RECOVERED_KEY: &str = "Ryn node has recovered.";

pub fn next_recovery_notification(
    notified_attention: &mut bool,
    recovery_state: &str,
    auto_recover: bool,
) -> Option<&'static str> {
    if !auto_recover {
        *notified_attention = false;
        return None;
    }
    if recovery_state == "needs_attention" {
        if !*notified_attention {
            *notified_attention = true;
            return Some(NOTIFY_NEEDS_ATTENTION_KEY);
        }
        return None;
    }
    if recovery_state == "healthy" && *notified_attention {
        *notified_attention = false;
        return Some(NOTIFY_RECOVERED_KEY);
    }
    None
}

#[derive(Default)]
pub struct Recovery { pub attempts: u8, failures: u8, healthy_ticks: u8, next: Option<Instant>, failed_since: Option<Instant> }
impl Recovery {
    pub fn check(&mut self, healthy: bool, enabled: bool, now: Instant) -> bool {
        self.check_with_grace(healthy, enabled, now, Duration::ZERO)
    }
    pub fn check_with_grace(&mut self, healthy: bool, enabled: bool, now: Instant, grace: Duration) -> bool {
        if healthy {
            self.failures = 0;
            self.failed_since = None;
            self.healthy_ticks = self.healthy_ticks.saturating_add(1);
            // A brief successful boot must not reset a crash loop's retry budget.
            if self.healthy_ticks >= 20 { *self = Self::default(); }
            return false;
        }
        self.healthy_ticks = 0;
        self.failures = self.failures.saturating_add(1);
        let failed_since = *self.failed_since.get_or_insert(now);
        if !enabled || self.failures < 3 || now.saturating_duration_since(failed_since) < grace
            || self.next.is_some_and(|t| now < t) { return false; }
        self.attempts = self.attempts.saturating_add(1);
        let delay_secs = if self.attempts >= 3 {
            LOW_FREQ_RECOVERY_SECS
        } else {
            (15 * 2_u64.pow(self.attempts as u32 - 1)).min(LOW_FREQ_RECOVERY_SECS)
        };
        self.next = Some(now + Duration::from_secs(delay_secs));
        true
    }
}

pub fn read_status(port: u16) -> Result<RuntimeStatus, String> {
    let value = node::request(port, "GET", "/api/local/desktop/status", "")?;
    if value["desktop_managed"] != true { return Err("unmanaged_node".into()); }
    let mut status: RuntimeStatus = serde_json::from_value(value).map_err(|_| "invalid_response")?;
    status.node_online = true;
    Ok(status)
}

fn watchdog_status(previous: RuntimeStatus, current: Result<RuntimeStatus, String>,
                   is_alive: impl FnOnce() -> bool) -> RuntimeStatus {
    match current {
        Ok(status) => status,
        Err(error) => {
            if is_alive() {
                RuntimeStatus { node_online: true, last_error: Some("node_status_unavailable".into()), ..previous }
            } else {
                RuntimeStatus { last_error: Some(error), ..Default::default() }
            }
        },
    }
}
#[tauri::command]
pub fn desktop_status(state: tauri::State<RuntimeState>) -> RuntimeStatus { state.status.lock().unwrap().clone() }

// Native confirmations also work when the webview or local node is unavailable.
// Buttons default to Cancel for potentially interrupted work.
#[cfg_attr(windows, allow(dead_code))]
fn non_windows_dialog_decision(
    text: &str,
    confirm: bool,
    invoke_dialog: impl FnOnce(
        &str,
        tauri_plugin_dialog::MessageDialogKind,
        tauri_plugin_dialog::MessageDialogButtons,
    ) -> bool,
) -> bool {
    use tauri_plugin_dialog::{MessageDialogButtons, MessageDialogKind};
    if confirm {
        invoke_dialog(text, MessageDialogKind::Warning, MessageDialogButtons::OkCancel)
    } else {
        invoke_dialog(text, MessageDialogKind::Info, MessageDialogButtons::Ok)
    }
}

#[cfg(windows)]
fn windows_message_box(text: &str, confirm: bool) -> bool {
    #[link(name = "user32")]
    extern "system" { fn MessageBoxW(window: isize, text: *const u16, caption: *const u16, flags: u32) -> i32; }
    let text: Vec<u16> = text.encode_utf16().chain(Some(0)).collect();
    let title: Vec<u16> = "Ryn".encode_utf16().chain(Some(0)).collect();
    unsafe { MessageBoxW(0, text.as_ptr(), title.as_ptr(), if confirm { 0x10131 } else { 0x10040 }) == 1 }
}

pub fn startup_alert(text: &str) {
    #[cfg(windows)]
    { let _ = windows_message_box(text, false); }
    #[cfg(not(windows))]
    {
        let _ = rfd::MessageDialog::new()
            .set_title("Ryn")
            .set_description(text)
            .set_level(rfd::MessageLevel::Error)
            .set_buttons(rfd::MessageButtons::Ok)
            .show();
    }
}

#[cfg(windows)]
pub fn message(_app: &tauri::AppHandle, text: &str, confirm: bool) -> bool {
    windows_message_box(text, confirm)
}

#[cfg(not(windows))]
pub fn message(app: &tauri::AppHandle, text: &str, confirm: bool) -> bool {
    use tauri_plugin_dialog::DialogExt;
    non_windows_dialog_decision(text, confirm, |body, kind, buttons| {
        app.dialog().message(body).title("Ryn").kind(kind).buttons(buttons).blocking_show()
    })
}

pub fn should_keep_awake(keep_awake_pref: bool, status: &RuntimeStatus) -> bool {
    keep_awake_pref
        && status.node_online
        && (status.sharing || status.active_tasks > 0 || status.agent_active_runs > 0 || status.setup_active)
}

pub fn confirm_warning(current: Result<&RuntimeStatus, &str>) -> String {
    match current {
        Ok(s) if s.agent_active_runs > 0 => {
            let shown: Vec<String> = s
                .agent_task_titles
                .iter()
                .map(|t| t.trim().chars().take(40).collect::<String>())
                .filter(|t| !t.is_empty())
                .take(3)
                .collect();
            let mut head = tr("{{count}} Agent tasks are running")
                .replace("{{count}}", &s.agent_active_runs.to_string());
            if !shown.is_empty() {
                let joined = shown.join(&tr(", "));
                if (s.agent_active_runs as usize) > shown.len() {
                    head.push_str(
                        &tr(" ({{titles}}, etc. {{count}} total)")
                            .replace("{{titles}}", &joined)
                            .replace("{{count}}", &s.agent_active_runs.to_string()),
                    );
                } else {
                    head.push_str(&tr(" ({{titles}})").replace("{{titles}}", &joined));
                }
            }
            if s.setup_active {
                let setup_line = tr("Model setup: {{state}}.").replace("{{state}}", &tr("in progress"));
                format!("{head}\n{setup_line}\n\n{}", tr("This will interrupt work on this node."))
            } else {
                format!("{head}\n\n{}", tr("This will interrupt work on this node."))
            }
        }
        Ok(s) if s.active_tasks > 0 || s.setup_active => tr("{{count}} active AI tasks. Model setup: {{state}}.\n\nThis will interrupt work on this node.")
            .replace("{{count}}", &s.active_tasks.to_string())
            .replace("{{state}}", &tr(if s.setup_active { "in progress" } else { "idle" })),
        Ok(_) => tr("This will disconnect services on this computer. Other service requests may also be interrupted."),
        Err(_) => tr("Task status is unavailable. Running work may be interrupted."),
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn command_in_path(program: &str, path_env: Option<&std::ffi::OsStr>) -> bool {
    let Some(paths) = path_env else { return false; };
    std::env::split_paths(paths).any(|dir| {
        let candidate = dir.join(program);
        match std::fs::metadata(&candidate) {
            Ok(meta) if meta.is_file() => {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    (meta.permissions().mode() & 0o111) != 0
                }
                #[cfg(not(unix))]
                {
                    true
                }
            }
            _ => false,
        }
    })
}

pub fn awake_supported() -> bool {
    #[cfg(any(windows, target_os = "macos"))]
    {
        true
    }
    #[cfg(target_os = "linux")]
    {
        command_in_path("systemd-inhibit", std::env::var_os("PATH").as_deref())
    }
    #[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
    {
        false
    }
}

trait AwakeBackend {
    type Guard;
    fn acquire(&mut self) -> Option<Self::Guard>;
    fn is_alive(&mut self, _guard: &mut Self::Guard) -> bool {
        true
    }
    fn release(&mut self, guard: Self::Guard);
}

struct AwakeController<B: AwakeBackend> {
    backend: B,
    guard: Option<B::Guard>,
}

impl<B: AwakeBackend> AwakeController<B> {
    fn new(backend: B) -> Self {
        Self { backend, guard: None }
    }

    fn set(&mut self, enabled: bool) -> bool {
        if !enabled {
            if let Some(guard) = self.guard.take() {
                self.backend.release(guard);
            }
            return false;
        }
        if let Some(guard) = self.guard.as_mut() {
            if self.backend.is_alive(guard) {
                return true;
            }
            if let Some(stale) = self.guard.take() {
                self.backend.release(stale);
            }
        }
        self.guard = self.backend.acquire();
        self.guard.is_some()
    }
}

impl<B: AwakeBackend> Drop for AwakeController<B> {
    fn drop(&mut self) {
        if let Some(guard) = self.guard.take() {
            self.backend.release(guard);
        }
    }
}

#[cfg(windows)]
struct WindowsAwakeBackend;

#[cfg(windows)]
impl WindowsAwakeBackend {
    fn set_thread_state(enabled: bool) -> bool {
        #[link(name = "kernel32")]
        extern "system" {
            fn SetThreadExecutionState(flags: u32) -> u32;
        }
        // Called and released on the same watchdog thread. The display may sleep.
        let result = unsafe { SetThreadExecutionState(0x8000_0000 | if enabled { 1 } else { 0 }) };
        enabled && result != 0
    }
}

#[cfg(windows)]
impl AwakeBackend for WindowsAwakeBackend {
    type Guard = ();

    fn acquire(&mut self) -> Option<Self::Guard> {
        Self::set_thread_state(true).then_some(())
    }

    fn is_alive(&mut self, _guard: &mut Self::Guard) -> bool {
        Self::set_thread_state(true)
    }

    fn release(&mut self, _guard: Self::Guard) {
        let _ = Self::set_thread_state(false);
    }
}

#[cfg(target_os = "macos")]
struct MacOsAwakeBackend;

#[cfg(target_os = "macos")]
impl AwakeBackend for MacOsAwakeBackend {
    type Guard = std::process::Child;

    fn acquire(&mut self) -> Option<Self::Guard> {
        use std::process::{Command, Stdio};
        Command::new("/usr/bin/caffeinate")
            .args(["-i", "-w", &std::process::id().to_string()])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().ok()
    }

    fn is_alive(&mut self, child: &mut Self::Guard) -> bool {
        matches!(child.try_wait(), Ok(None))
    }

    fn release(&mut self, mut child: Self::Guard) {
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
struct SystemdInhibitBackend {
    program: &'static str,
}

impl SystemdInhibitBackend {
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    const fn new(program: &'static str) -> Self {
        Self { program }
    }
}

impl AwakeBackend for SystemdInhibitBackend {
    type Guard = std::process::Child;

    fn acquire(&mut self) -> Option<Self::Guard> {
        use std::process::{Command, Stdio};
        let mut cmd = Command::new(self.program);
        cmd.args(["--what=idle:sleep", "--why=Ryn", "sleep", "infinity"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::process::CommandExt;
            let parent_pid = unsafe { libc::getpid() };
            unsafe {
                cmd.pre_exec(move || {
                    if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL as libc::c_ulong) != 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    if libc::getppid() != parent_pid {
                        libc::_exit(1);
                    }
                    Ok(())
                });
            }
        }
        cmd.spawn().ok()
    }

    fn is_alive(&mut self, child: &mut Self::Guard) -> bool {
        matches!(child.try_wait(), Ok(None))
    }

    fn release(&mut self, mut child: Self::Guard) {
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
struct UnsupportedAwakeBackend;

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
impl AwakeBackend for UnsupportedAwakeBackend {
    type Guard = ();
    fn acquire(&mut self) -> Option<Self::Guard> {
        None
    }
    fn release(&mut self, _guard: Self::Guard) {}
}

#[cfg(windows)]
type PlatformAwakeController = AwakeController<WindowsAwakeBackend>;
#[cfg(target_os = "macos")]
type PlatformAwakeController = AwakeController<MacOsAwakeBackend>;
#[cfg(target_os = "linux")]
type PlatformAwakeController = AwakeController<SystemdInhibitBackend>;
#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
type PlatformAwakeController = AwakeController<UnsupportedAwakeBackend>;

fn new_awake_controller() -> PlatformAwakeController {
    #[cfg(windows)]
    {
        AwakeController::new(WindowsAwakeBackend)
    }
    #[cfg(target_os = "macos")]
    {
        AwakeController::new(MacOsAwakeBackend)
    }
    #[cfg(target_os = "linux")]
    {
        AwakeController::new(SystemdInhibitBackend::new("systemd-inhibit"))
    }
    #[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
    {
        AwakeController::new(UnsupportedAwakeBackend)
    }
}

fn awake(controller: &mut PlatformAwakeController, enabled: bool) -> bool {
    controller.set(enabled)
}
pub fn open_logs() -> Result<(), String> {
    std::process::Command::new(if cfg!(windows) { "explorer.exe" } else if cfg!(target_os = "macos") { "/usr/bin/open" } else { "xdg-open" })
        .arg(node::log_dir()).spawn().map(|_| ()).map_err(|_| "Could not open the log folder.".into())
}
pub fn run_action(app: &tauri::AppHandle, action: &str) -> Result<String, String> {
    let runtime = app.state::<RuntimeState>();
    if runtime.action_busy.swap(true, Ordering::SeqCst) { return Err(tr("Another desktop action is in progress.")); }
    let result = action_inner(app, action);
    runtime.action_busy.store(false, Ordering::SeqCst);
    result.map(|value| tr(&value)).map_err(|error| tr(&error))
}
fn action_inner(app: &tauri::AppHandle, action: &str) -> Result<String, String> {
    let state = app.state::<node::NodeState>();
    match action {
        "logs" => { open_logs()?; Ok("Log folder opened.".into()) }
        "diagnostics" => {
            let status = app.state::<RuntimeState>().status.lock().unwrap().clone();
            // Explicit allowlist: never copy logs, environment, identity, paths,
            // peer details, prompts, invitation codes or access credentials.
            let report = serde_json::json!({"schema": 1, "version": app.package_info().version.to_string(),
                "platform": std::env::consts::OS, "runtime": status});
            let directory = node::log_dir().join("diagnostics");
            std::fs::create_dir_all(&directory).map_err(|_| "Could not create diagnostics folder.")?;
            let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis();
            let path = directory.join(format!("ryn-diagnostics-{stamp}.json"));
            std::fs::write(&path, serde_json::to_vec_pretty(&report).unwrap()).map_err(|_| "Could not save diagnostics.")?;
            Ok(path.to_string_lossy().to_string())
        }
        "pause" | "resume" => {
            node::request(state.port, "POST", "/api/local/desktop/sharing", if action == "resume" { r#"{"enabled":true}"# } else { r#"{"enabled":false}"# })
                .map_err(|_| "Could not change sharing. Check the node and AI service setup.")?;
            if let Ok(status) = read_status(state.port) { *app.state::<RuntimeState>().status.lock().unwrap() = status; }
            Ok(if action == "pause" { "AI sharing paused. Current requests can finish." } else { "AI sharing enabled. Model availability is checked in Service setup." }.into())
        }
        "restart" | "quit" => {
            let current = read_status(state.port);
            let warning = confirm_warning(current.as_ref().map_err(String::as_str));
            let update_notice = match (action, crate::updates::version_on_quit(app)) {
                ("quit", Some(version)) => format!("\n\n{}", tr("Ryn {{version}} will be installed as Ryn quits.").replace("{{version}}", &version)),
                _ => String::new(),
            };
            if !message(app, &format!("{}\n\n{warning}{update_notice}", tr(if action == "quit" { "Quit Ryn?" } else { "Restart the node in Ryn?" })), true) {
                return Ok("Cancelled.".into());
            }
            if action == "quit" { crate::updates::note_quit(app); app.exit(0); return Ok("Quitting Ryn.".into()); }
            node::restart(state.inner()).map_err(|_| "Could not restart the node. Open logs for details.")?;
            app.state::<RuntimeState>().reset_recovery.store(true, Ordering::SeqCst);
            if !node::wait_healthy(state.port) { return Err("The node did not become ready. Open logs or retry.".into()); }
            Ok("Node restarted.".into())
        }
        _ => Err("Unknown desktop action.".into())
    }
}
#[tauri::command]
pub async fn desktop_action(app: tauri::AppHandle, action: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || run_action(&app, &action)).await.map_err(|_| tr("Desktop action failed."))?
}
pub fn tray_status_key(status: &RuntimeStatus) -> &'static str {
    if !status.node_online {
        "Ryn - Node unavailable"
    } else if status.agent_active_runs > 0 {
        "Ryn - Agent running"
    } else if !status.configured {
        "Ryn - Node running"
    } else if status.sharing {
        "Ryn - AI sharing enabled"
    } else {
        "Ryn - AI sharing paused"
    }
}
const STARTUP_GRACE: Duration = Duration::from_secs(120);

fn long_task_awake_notice(started: &mut Option<Instant>, status: &RuntimeStatus,
                         keep_awake: bool, seen: bool, now: Instant) -> bool {
    if seen || keep_awake || !status.node_online
        || (status.active_tasks == 0 && status.agent_active_runs == 0) {
        *started = None;
        return false;
    }
    now.saturating_duration_since(*started.get_or_insert(now)) >= Duration::from_secs(60)
}

pub fn watchdog(app: tauri::AppHandle, status_item: tauri::menu::MenuItem<tauri::Wry>, share_item: tauri::menu::MenuItem<tauri::Wry>) {
    std::thread::spawn(move || {
        let mut recovery = Recovery::default();
        let mut notified_attention = false;
        let mut awake_controller = new_awake_controller();
        let mut long_task_started = None;
        // Allow the one-file sidecar to extract and start before counting failures.
        let mut boot_deadline = Instant::now() + STARTUP_GRACE;
        loop {
            let node = app.state::<node::NodeState>();
            if node.stopping.load(Ordering::SeqCst) { break; }
            let prefs = app.state::<DesktopState>().prefs.lock().unwrap().clone();
            let runtime = app.state::<RuntimeState>();
            if runtime.reset_recovery.swap(false, Ordering::SeqCst) { recovery = Recovery::default(); }
            let previous = runtime.status.lock().unwrap().clone();
            let mut status = watchdog_status(previous, read_status(node.port), || node::health_ok(node.port));
            status.awake_active = awake(&mut awake_controller, should_keep_awake(prefs.keep_awake, &status));
            if prefs.awake_supported && long_task_awake_notice(&mut long_task_started, &status,
                prefs.keep_awake, prefs.awake_notice_seen, Instant::now()) {
                crate::preferences::acknowledge_awake_notice(&app);
                use tauri_plugin_notification::NotificationExt;
                let _ = app.notification().builder().title("Ryn").body(
                    tr("A long task is running. Enable keep-awake in Desktop settings to prevent idle sleep from interrupting it.")
                ).show();
            }
            // A slow metadata/CLI refresh is not proof that the live node died.
            // Give a running child time to answer before interrupting its tasks;
            // exited children retain the ordinary three-failure crash recovery.
            let grace = if node::owned_child_running(node.inner()) { Duration::from_secs(90) } else { Duration::ZERO };
            let should_restart = Instant::now() >= boot_deadline && recovery.check_with_grace(status.node_online,
                prefs.auto_recover && !runtime.action_busy.load(Ordering::SeqCst), Instant::now(), grace);
            status.recovery_attempts = recovery.attempts;
            status.recovery = if status.node_online { "healthy" } else if Instant::now() < boot_deadline { "starting" } else if !prefs.auto_recover { "disabled" } else if should_restart { "restarting" } else if recovery.attempts >= 3 { "needs_attention" } else { "waiting" }.into();
            if let Some(notice_key) = next_recovery_notification(&mut notified_attention, &status.recovery, prefs.auto_recover) {
                use tauri_plugin_notification::NotificationExt;
                let body = tr(notice_key);
                let _ = app.notification().builder().title("Ryn").body(&body).show();
            }
            let text = tr(tray_status_key(&status));
            let _ = status_item.set_text(&text);
            let _ = share_item.set_text(tr(if status.sharing { "Pause AI Sharing" } else { "Resume AI Sharing" }));
            let _ = share_item.set_enabled(status.node_online && status.configured);
            if let Some(tray) = app.tray_by_id("main") { let _ = tray.set_tooltip(Some(&text)); }
            *runtime.status.lock().unwrap() = status;
            if should_restart && !runtime.action_busy.swap(true, Ordering::SeqCst) {
                if let Err(error) = node::restart(node.inner()) {
                    log::error!("node recovery failed: {error}");
                } else {
                    boot_deadline = Instant::now() + STARTUP_GRACE;
                }
                let _ = node::wait_healthy(node.port);
                runtime.action_busy.store(false, Ordering::SeqCst);
            }
            std::thread::sleep(Duration::from_secs(3));
        }
        awake(&mut awake_controller, false);
    });
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn live_child_transient_timeout_does_not_interrupt_tasks() {
        let start = Instant::now();
        let mut recovery = Recovery::default();
        let grace = Duration::from_secs(90);
        for seconds in [0, 10, 20, 40, 80] {
            assert!(!recovery.check_with_grace(false, true, start + Duration::from_secs(seconds), grace));
        }
        assert_eq!(recovery.attempts, 0);
        assert!(!recovery.check_with_grace(true, true, start + Duration::from_secs(85), grace));
        assert!(!recovery.check_with_grace(false, true, start + Duration::from_secs(100), grace));
        assert!(!recovery.check_with_grace(false, true, start + Duration::from_secs(110), grace));
        assert!(!recovery.check_with_grace(false, true, start + Duration::from_secs(120), grace));
        assert!(recovery.check_with_grace(false, true, start + Duration::from_secs(190), grace));
        assert_eq!(recovery.attempts, 1);
    }
    #[test]
    fn unavailable_metadata_keeps_a_live_node_online_without_recovery() {
        let previous = RuntimeStatus { node_online: true, configured: true, sharing: true,
            active_tasks: 2, setup_active: true, ..Default::default() };
        let now = Instant::now();
        let mut recovery = Recovery::default();
        for _ in 0..5 {
            let status = watchdog_status(previous.clone(), Err("node_unavailable".into()), || true);
            assert!(status.node_online && status.configured && status.sharing && status.setup_active);
            assert_eq!(status.active_tasks, 2);
            assert_eq!(status.last_error.as_deref(), Some("node_status_unavailable"));
            assert!(!recovery.check(status.node_online, true, now));
        }
        assert_eq!(recovery.attempts, 0);
    }

    #[test]
    fn failed_liveness_still_uses_the_existing_three_failure_budget() {
        let mut recovery = Recovery::default();
        let now = Instant::now();
        for expected in [false, false, true] {
            let status = watchdog_status(RuntimeStatus::default(), Err("node_unavailable".into()), || false);
            assert!(!status.node_online);
            assert_eq!(recovery.check(status.node_online, true, now), expected);
        }
    }

    #[test]
    fn successful_metadata_does_not_probe_liveness_again() {
        let current = RuntimeStatus { node_online: true, active_tasks: 3, ..Default::default() };
        let status = watchdog_status(RuntimeStatus::default(), Ok(current), || panic!("unneeded health probe"));
        assert!(status.node_online);
        assert_eq!(status.active_tasks, 3);
        assert!(status.last_error.is_none());
    }
    #[test]
    fn backoff_is_bounded_and_short_success_does_not_reset_it() {
        let now = Instant::now(); let mut r = Recovery::default();
        assert!(!r.check(false, true, now)); assert!(!r.check(false, true, now));
        assert!(r.check(false, true, now));
        assert!(!r.check(false, true, now + Duration::from_secs(14)));
        assert!(!r.check(true, true, now));
        for _ in 0..2 { assert!(!r.check(false, true, now + Duration::from_secs(15))); }
        assert!(r.check(false, true, now + Duration::from_secs(15)));
        assert!(r.check(false, true, now + Duration::from_secs(45)));
        assert!(!r.check(false, true, now + Duration::from_secs(644)));
        for _ in 0..20 { r.check(true, true, now); } assert_eq!(r.attempts, 0);
    }
    #[test]
    fn low_frequency_retry_waits_ten_minutes_after_three_failures() {
        let now = Instant::now();
        let mut r = Recovery::default();
        // Rapid budget: 3 restarts at +0s, +15s, +45s.
        assert!(!r.check(false, true, now));
        assert!(!r.check(false, true, now));
        assert!(r.check(false, true, now));
        assert!(r.check(false, true, now + Duration::from_secs(15)));
        let third_at = now + Duration::from_secs(45);
        assert!(r.check(false, true, third_at));
        assert_eq!(r.attempts, 3);

        // Within 10 minutes (< 600s) after the 3rd restart, no restart is allowed.
        for elapsed in [1_u64, 60, 300, 599] {
            assert!(!r.check(false, true, third_at + Duration::from_secs(elapsed)));
        }

        // At +600s (10 minutes), one low-frequency restart is allowed and capped at 10m again.
        let fourth_at = third_at + Duration::from_secs(600);
        assert!(r.check(false, true, fourth_at));
        assert_eq!(r.attempts, 4);
        assert!(!r.check(false, true, fourth_at + Duration::from_secs(599)));
        assert!(r.check(false, true, fourth_at + Duration::from_secs(600)));
    }
    #[test]
    fn healthy_twenty_ticks_resets_low_frequency_recovery() {
        let now = Instant::now();
        let mut r = Recovery::default();
        assert!(!r.check(false, true, now));
        assert!(!r.check(false, true, now));
        assert!(r.check(false, true, now));
        assert!(r.check(false, true, now + Duration::from_secs(15)));
        assert!(r.check(false, true, now + Duration::from_secs(45)));
        assert!(r.check(false, true, now + Duration::from_secs(645)));
        assert_eq!(r.attempts, 4);

        for _ in 0..19 {
            assert!(!r.check(true, true, now + Duration::from_secs(700)));
        }
        assert_eq!(r.attempts, 4);
        assert!(!r.check(true, true, now + Duration::from_secs(701)));
        assert_eq!(r.attempts, 0);
        assert!(r.next.is_none());
    }
    #[test]
    fn recovery_notification_deduplicates_attention_and_recovery_transitions() {
        let mut notified = false;
        // Normal startup and healthy ticks never emit notifications.
        assert_eq!(next_recovery_notification(&mut notified, "starting", true), None);
        assert_eq!(next_recovery_notification(&mut notified, "healthy", true), None);

        // First entry into needs_attention notifies once; subsequent ticks or low-freq retries do not.
        assert_eq!(
            next_recovery_notification(&mut notified, "needs_attention", true),
            Some(NOTIFY_NEEDS_ATTENTION_KEY)
        );
        assert_eq!(next_recovery_notification(&mut notified, "needs_attention", true), None);
        assert_eq!(next_recovery_notification(&mut notified, "restarting", true), None);
        assert_eq!(next_recovery_notification(&mut notified, "starting", true), None);
        assert_eq!(next_recovery_notification(&mut notified, "needs_attention", true), None);

        // Returning to healthy notifies once and resets state.
        assert_eq!(
            next_recovery_notification(&mut notified, "healthy", true),
            Some(NOTIFY_RECOVERED_KEY)
        );
        assert_eq!(next_recovery_notification(&mut notified, "healthy", true), None);

        // Disabled auto_recover never notifies.
        assert_eq!(next_recovery_notification(&mut notified, "needs_attention", false), None);
        assert!(!notified);
    }
    #[test]
    fn disabled_recovery_never_restarts() {
        let mut r = Recovery::default();
        for _ in 0..100 { assert!(!r.check(false, false, Instant::now())); }
    }

    #[test]
    fn old_node_status_defaults_agent_counts_and_keep_awake_includes_agent_runs() {
        let legacy_json = serde_json::json!({
            "desktop_managed": true,
            "configured": false,
            "sharing": false,
            "active_tasks": 0,
            "setup_active": false
        });
        let legacy: RuntimeStatus = serde_json::from_value(legacy_json).unwrap();
        assert_eq!(legacy.agent_active_runs, 0);
        assert_eq!(legacy.agent_waiting, 0);
        assert!(legacy.agent_task_titles.is_empty());
        assert!(!should_keep_awake(true, &legacy));

        let mut active_agent = RuntimeStatus {
            node_online: true,
            configured: false,
            sharing: false,
            active_tasks: 0,
            agent_active_runs: 2,
            agent_waiting: 1,
            agent_task_titles: vec!["Task Alpha".into(), "Task Beta".into()],
            setup_active: false,
            ..Default::default()
        };
        assert!(should_keep_awake(true, &active_agent));
        assert!(!should_keep_awake(false, &active_agent));
        assert_eq!(tray_status_key(&active_agent), "Ryn - Agent running");

        let warn_two = confirm_warning(Ok(&active_agent));
        assert!(warn_two.contains("2"));
        assert!(warn_two.contains("Task Alpha"));
        assert!(warn_two.contains("Task Beta"));

        active_agent.agent_active_runs = 5;
        active_agent.agent_task_titles = vec!["T1".into(), "T2".into(), "T3".into()];
        let warn_five = confirm_warning(Ok(&active_agent));
        assert!(warn_five.contains("5"));
        assert!(warn_five.contains("T1") && warn_five.contains("T2") && warn_five.contains("T3"));
    }

    #[test]
    fn non_windows_confirmation_dialog_returns_false_when_cancelled() {
        use tauri_plugin_dialog::{MessageDialogButtons, MessageDialogKind};
        let mut seen_body = None;
        let mut seen_warning_ok_cancel = false;
        let cancelled = non_windows_dialog_decision("Quit Ryn?", true, |body, kind, buttons| {
            seen_body = Some(body.to_owned());
            seen_warning_ok_cancel = matches!(kind, MessageDialogKind::Warning)
                && matches!(buttons, MessageDialogButtons::OkCancel);
            false
        });
        assert!(!cancelled);
        assert_eq!(seen_body.as_deref(), Some("Quit Ryn?"));
        assert!(seen_warning_ok_cancel);

        let accepted = non_windows_dialog_decision("Quit Ryn?", true, |_body, kind, buttons| {
            assert!(matches!(kind, MessageDialogKind::Warning));
            assert!(matches!(buttons, MessageDialogButtons::OkCancel));
            true
        });
        assert!(accepted);
    }

    #[test]
    fn awake_on_off_on_holds_at_most_one_assertion_or_child() {
        use std::cell::RefCell;
        use std::rc::Rc;

        #[derive(Default)]
        struct Tracker {
            next_id: u32,
            live_ids: Vec<u32>,
            max_live: usize,
            acquired: Vec<u32>,
            released: Vec<u32>,
        }

        struct FakeBackend {
            tracker: Rc<RefCell<Tracker>>,
        }

        impl AwakeBackend for FakeBackend {
            type Guard = u32;

            fn acquire(&mut self) -> Option<Self::Guard> {
                let mut t = self.tracker.borrow_mut();
                t.next_id += 1;
                let id = t.next_id;
                t.live_ids.push(id);
                t.max_live = t.max_live.max(t.live_ids.len());
                t.acquired.push(id);
                Some(id)
            }

            fn release(&mut self, guard: Self::Guard) {
                let mut t = self.tracker.borrow_mut();
                t.live_ids.retain(|&id| id != guard);
                t.released.push(guard);
            }
        }

        let tracker = Rc::new(RefCell::new(Tracker::default()));
        {
            let mut controller = AwakeController::new(FakeBackend {
                tracker: Rc::clone(&tracker),
            });

            // On (first tick + repeated watchdog tick): acquires once, holds 1
            assert!(controller.set(true));
            assert!(controller.set(true));
            assert_eq!(tracker.borrow().live_ids, vec![1]);
            assert_eq!(tracker.borrow().max_live, 1);
            assert_eq!(tracker.borrow().acquired, vec![1]);
            assert!(tracker.borrow().released.is_empty());

            // Off (and repeated off tick): releases 1, holds 0
            assert!(!controller.set(false));
            assert!(!controller.set(false));
            assert!(tracker.borrow().live_ids.is_empty());
            assert_eq!(tracker.borrow().released, vec![1]);

            // On again (on -> off -> on): acquires #2, still holds at most 1
            assert!(controller.set(true));
            assert!(controller.set(true));
            assert_eq!(tracker.borrow().live_ids, vec![2]);
            assert_eq!(tracker.borrow().max_live, 1);
            assert_eq!(tracker.borrow().acquired, vec![1, 2]);
            assert_eq!(tracker.borrow().released, vec![1]);
        }

        // Dropping an active controller cleans up the remaining assertion/child
        assert!(tracker.borrow().live_ids.is_empty());
        assert_eq!(tracker.borrow().released, vec![1, 2]);
    }

    #[test]
    fn linux_awake_returns_false_when_systemd_inhibit_is_missing() {
        let mut controller = AwakeController::new(SystemdInhibitBackend::new(
            "ryn-nonexistent-systemd-inhibit-binary",
        ));
        assert!(!controller.set(true));
        assert!(controller.guard.is_none());
        assert!(!controller.set(false));

        let empty_dir = std::env::temp_dir().join(format!(
            "ryn-awake-path-test-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&empty_dir);
        std::fs::create_dir_all(&empty_dir).unwrap();
        assert!(!command_in_path("systemd-inhibit", Some(empty_dir.as_os_str())));
        let _ = std::fs::remove_dir_all(&empty_dir);
    }
}
