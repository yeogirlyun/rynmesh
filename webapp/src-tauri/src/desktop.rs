use crate::{node, preferences::DesktopState};
use serde::{Deserialize, Serialize};
use std::sync::{Mutex, atomic::{AtomicBool, Ordering}};
use std::time::{Duration, Instant};
use tauri::Manager;

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct RuntimeStatus {
    pub node_online: bool,
    pub configured: bool,
    pub sharing: bool,
    pub active_tasks: u64,
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

#[derive(Default)]
pub struct Recovery { pub attempts: u8, failures: u8, healthy_ticks: u8, next: Option<Instant> }
impl Recovery {
    pub fn check(&mut self, healthy: bool, enabled: bool, now: Instant) -> bool {
        if healthy {
            self.failures = 0;
            self.healthy_ticks = self.healthy_ticks.saturating_add(1);
            // A brief successful boot must not reset a crash loop's retry budget.
            if self.healthy_ticks >= 20 { *self = Self::default(); }
            return false;
        }
        self.healthy_ticks = 0;
        self.failures = self.failures.saturating_add(1);
        if !enabled || self.failures < 3 || self.attempts >= 3 || self.next.is_some_and(|t| now < t) { return false; }
        self.attempts += 1;
        self.next = Some(now + Duration::from_secs(15 * 2_u64.pow(self.attempts as u32 - 1)));
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
#[tauri::command]
pub fn desktop_status(state: tauri::State<RuntimeState>) -> RuntimeStatus { state.status.lock().unwrap().clone() }

// Native confirmations also work when the webview or local node is unavailable.
// Buttons default to Cancel for potentially interrupted work.
#[cfg(windows)]
pub fn message(text: &str, confirm: bool) -> bool {
    #[link(name = "user32")]
    extern "system" { fn MessageBoxW(window: isize, text: *const u16, caption: *const u16, flags: u32) -> i32; }
    let text: Vec<u16> = text.encode_utf16().chain(Some(0)).collect();
    let title: Vec<u16> = "Ryn".encode_utf16().chain(Some(0)).collect();
    unsafe { MessageBoxW(0, text.as_ptr(), title.as_ptr(), if confirm { 0x10131 } else { 0x10040 }) == 1 }
}
#[cfg(not(windows))]
// Preserve the existing non-Windows lifecycle; native confirmation is Windows-only.
pub fn message(_text: &str, _confirm: bool) -> bool { true }

fn awake(enabled: bool) -> bool {
    #[cfg(windows)] {
        #[link(name = "kernel32")]
        extern "system" { fn SetThreadExecutionState(flags: u32) -> u32; }
        // Called and released on the same watchdog thread. The display may sleep.
        let result = unsafe { SetThreadExecutionState(0x80000000 | if enabled { 1 } else { 0 }) };
        return enabled && result != 0;
    }
    #[cfg(not(windows))] { let _ = enabled; false }
}
pub fn open_logs() -> Result<(), String> {
    std::process::Command::new(if cfg!(windows) { "explorer.exe" } else if cfg!(target_os = "macos") { "/usr/bin/open" } else { "xdg-open" })
        .arg(node::log_dir()).spawn().map(|_| ()).map_err(|_| "Could not open the log folder.".into())
}
pub fn run_action(app: &tauri::AppHandle, action: &str) -> Result<String, String> {
    let runtime = app.state::<RuntimeState>();
    if runtime.action_busy.swap(true, Ordering::SeqCst) { return Err("Another desktop action is in progress.".into()); }
    let result = action_inner(app, action);
    runtime.action_busy.store(false, Ordering::SeqCst);
    result
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
            let warning = match &current {
                Ok(s) if s.active_tasks > 0 || s.setup_active => format!("{} active AI tasks. Model setup {}.\n\nThis will interrupt work on this node.", s.active_tasks, if s.setup_active { "is in progress" } else { "is idle" }),
                Ok(_) => "This will disconnect services on this computer. Other service requests may also be interrupted.".into(),
                Err(_) => "Task status is unavailable. Running work may be interrupted.".into(),
            };
            if !message(&format!("{} Ryn?\n\n{warning}", if action == "quit" { "Quit" } else { "Restart the node in" }), true) {
                return Ok("Cancelled.".into());
            }
            if action == "quit" { app.exit(0); return Ok("Quitting Ryn.".into()); }
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
    tauri::async_runtime::spawn_blocking(move || run_action(&app, &action)).await.map_err(|_| "Desktop action failed.")?
}
pub fn watchdog(app: tauri::AppHandle, status_item: tauri::menu::MenuItem<tauri::Wry>, share_item: tauri::menu::MenuItem<tauri::Wry>) {
    std::thread::spawn(move || {
        let mut recovery = Recovery::default();
        // Allow the one-file sidecar to extract and start before counting failures.
        let boot_deadline = Instant::now() + Duration::from_secs(15);
        loop {
            let node = app.state::<node::NodeState>();
            if node.stopping.load(Ordering::SeqCst) { break; }
            let prefs = app.state::<DesktopState>().prefs.lock().unwrap().clone();
            let runtime = app.state::<RuntimeState>();
            if runtime.reset_recovery.swap(false, Ordering::SeqCst) { recovery = Recovery::default(); }
            let mut status = match read_status(node.port) {
                Ok(status) => status,
                Err(error) => RuntimeStatus { last_error: Some(error), ..Default::default() },
            };
            status.awake_active = awake(prefs.keep_awake && status.node_online && (status.sharing || status.active_tasks > 0 || status.setup_active));
            let should_restart = Instant::now() >= boot_deadline && recovery.check(status.node_online, prefs.auto_recover && !runtime.action_busy.load(Ordering::SeqCst), Instant::now());
            status.recovery_attempts = recovery.attempts;
            status.recovery = if status.node_online { "healthy" } else if Instant::now() < boot_deadline { "starting" } else if !prefs.auto_recover { "disabled" } else if should_restart { "restarting" } else if recovery.attempts >= 3 { "needs_attention" } else { "waiting" }.into();
            let text = if !status.node_online { "Ryn - Node unavailable" } else if !status.configured { "Ryn - Node running" } else if status.sharing { "Ryn - AI sharing enabled" } else { "Ryn - AI sharing paused" };
            let _ = status_item.set_text(text);
            let _ = share_item.set_text(if status.sharing { "Pause AI Sharing" } else { "Resume AI Sharing" });
            let _ = share_item.set_enabled(status.node_online && status.configured);
            if let Some(tray) = app.tray_by_id("main") { let _ = tray.set_tooltip(Some(text)); }
            *runtime.status.lock().unwrap() = status;
            if should_restart && !runtime.action_busy.swap(true, Ordering::SeqCst) {
                if let Err(error) = node::restart(node.inner()) { log::error!("node recovery failed: {error}"); }
                let _ = node::wait_healthy(node.port);
                runtime.action_busy.store(false, Ordering::SeqCst);
            }
            std::thread::sleep(Duration::from_secs(3));
        }
        awake(false);
    });
}
#[cfg(test)]
mod tests {
    use super::*;
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
        assert!(!r.check(false, true, now + Duration::from_secs(1000)));
        for _ in 0..20 { r.check(true, true, now); } assert_eq!(r.attempts, 0);
    }
    #[test]
    fn disabled_recovery_never_restarts() {
        let mut r = Recovery::default();
        for _ in 0..100 { assert!(!r.check(false, false, Instant::now())); }
    }
}
