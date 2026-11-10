//! Desktop updates are a single signed bundle (UI + sidecar), never pip installs.
//!
//! Updates are checked and downloaded in the background. Once one is ready the app
//! shell shows "Update": one click installs it silently and reopens Ryn. While work is
//! running the click is remembered and the update installs once Ryn is idle (the owner
//! may also choose to update right away; open documents are never closed for them).
//! A ready update is also installed when the owner quits Ryn, unless they turned that off.
use serde::{Deserialize, Serialize};
use std::{sync::{Mutex, atomic::{AtomicBool, Ordering}}, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use tauri::{Manager, Url};
use tauri_plugin_updater::{Update, UpdaterExt};

pub const ENDPOINT: &str = "https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json";
const PUBLIC_KEY: &str = match option_env!("RYN_DESKTOP_UPDATE_PUBLIC_KEY") { Some(key) => key, None => "" };
/// End-to-end test builds only; the release workflow never sets it. The manifest URL of a
/// local test server: packages must come from its origin and still carry a signature for
/// the key compiled into the build.
const TEST_ENDPOINT: Option<&str> = option_env!("RYN_DESKTOP_UPDATE_TEST_ENDPOINT");
const CHECK_INTERVAL: Duration = Duration::from_secs(3600);
const RETRY_INTERVAL: Duration = Duration::from_secs(15 * 60);
const TICK: Duration = Duration::from_secs(15);
/// Background failures reach the app shell only once they keep happening; a failure of
/// an update the owner asked to install is shown at once.
pub const VISIBLE_FAILURES: u32 = 3;

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Status {
    pub current_version: String,
    pub available_version: Option<String>,
    pub notes: Option<String>,
    pub state: String,
    pub auto_download: bool,
    pub install_on_quit: bool,
    pub configured: bool,
    pub downloaded: u64,
    pub total: Option<u64>,
    pub last_check: Option<u64>,
    pub last_error: Option<String>,
    pub failures: u32,
    pub endpoint: String,
    /// The owner asked to update while Ryn was busy; it installs once idle.
    pub install_requested: bool,
    pub waiting_for: Option<String>,
    /// Set on the first start after an update: the version Ryn was updated from.
    pub updated_from: Option<String>,
}
#[derive(Default)]
pub struct UpdateState {
    pub status: Mutex<Status>,
    pending: Mutex<Option<Update>>,
    busy: AtomicBool,
    quitting: AtomicBool,
}
#[derive(Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct Preferences { auto_download: bool, install_on_quit: bool, last_version: Option<String> }
impl Default for Preferences {
    fn default() -> Self { Self { auto_download: true, install_on_quit: true, last_version: None } }
}

struct Busy<'a>(&'a AtomicBool);
impl Drop for Busy<'_> { fn drop(&mut self) { self.0.store(false, Ordering::SeqCst); } }
fn acquire(state: &UpdateState) -> Result<Busy<'_>, String> {
    if state.busy.swap(true, Ordering::SeqCst) { Err("update_busy".into()) } else { Ok(Busy(&state.busy)) }
}
fn directory(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let root = match crate::deployment::desktop_config_dir() {
        Some(root) => root,
        None => app.path().app_config_dir().map_err(|e| e.to_string())?,
    };
    let path = root.join("updates");
    std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
    Ok(path)
}
fn read_preferences(app: &tauri::AppHandle) -> Preferences {
    directory(app).ok().and_then(|p| std::fs::read(p.join("preferences.json")).ok())
        .and_then(|bytes| serde_json::from_slice(&bytes).ok()).unwrap_or_default()
}
fn write_preferences(app: &tauri::AppHandle, preferences: &Preferences) -> Result<(), String> {
    let path = directory(app)?.join("preferences.json");
    let stage = path.with_extension("tmp");
    std::fs::write(&stage, serde_json::to_vec(preferences).unwrap()).map_err(|e| e.to_string())?;
    std::fs::rename(stage, path).map_err(|e| e.to_string())
}
fn cache_path(app: &tauri::AppHandle, update: &Update) -> Result<std::path::PathBuf, String> {
    // version originates from the updater's parsed SemVer, never a caller path.
    Ok(directory(app)?.join(format!("{}.update", update.version)))
}
fn snapshot(app: &tauri::AppHandle) -> Status { app.state::<UpdateState>().status.lock().unwrap().clone() }
fn record_failure(app: &tauri::AppHandle, error: String, visible: bool) -> String {
    let state = app.state::<UpdateState>();
    let mut status = state.status.lock().unwrap();
    status.state = "error".into(); status.last_error = Some(error.clone());
    status.failures = if visible { status.failures.max(VISIBLE_FAILURES) } else { status.failures + 1 };
    status.install_requested = false; status.waiting_for = None;
    error
}
fn failed(app: &tauri::AppHandle, error: String) -> String { record_failure(app, error, false) }
fn install_failed(app: &tauri::AppHandle, error: String) -> String { record_failure(app, error, true) }

fn endpoint() -> Url {
    TEST_ENDPOINT.and_then(|value| value.parse().ok()).unwrap_or_else(|| ENDPOINT.parse().unwrap())
}
fn allowed_package(url: &Url, endpoint: &Url) -> bool {
    if !url.username().is_empty() || url.password().is_some() { return false; }
    if endpoint.as_str() == ENDPOINT {
        return url.scheme() == "https" && url.host_str() == Some("github.com") && url.port_or_known_default() == Some(443)
            && url.path().starts_with("/yeogirlyun/rynmesh/releases/download/");
    }
    // A test build's local server serves packages from the origin of its manifest.
    url.origin() == endpoint.origin()
}

#[tauri::command]
pub fn desktop_update_status(app: tauri::AppHandle) -> Status { snapshot(&app) }

#[tauri::command]
pub fn desktop_update_preferences(app: tauri::AppHandle, auto_download: Option<bool>, install_on_quit: Option<bool>) -> Result<Status, String> {
    let state = app.state::<UpdateState>();
    let mut status = state.status.lock().unwrap();
    let mut preferences = read_preferences(&app);
    if let Some(value) = auto_download { preferences.auto_download = value; }
    if let Some(value) = install_on_quit { preferences.install_on_quit = value; }
    write_preferences(&app, &preferences)?;
    status.auto_download = preferences.auto_download; status.install_on_quit = preferences.install_on_quit;
    Ok(status.clone())
}

/// The owner has seen the "updated" notice.
#[tauri::command]
pub fn desktop_update_acknowledge(app: tauri::AppHandle) -> Status {
    let state = app.state::<UpdateState>();
    let mut status = state.status.lock().unwrap();
    status.updated_from = None;
    status.clone()
}

async fn check(app: &tauri::AppHandle) -> Result<(), String> {
    if PUBLIC_KEY.trim().is_empty() { return Err("update_signing_key_not_configured".into()); }
    let state = app.state::<UpdateState>();
    let endpoint = endpoint();
    {
        let mut status = state.status.lock().unwrap();
        status.state = "checking".into(); status.last_error = None;
        status.last_check = Some(SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs());
    }
    let updater = app.updater_builder().pubkey(PUBLIC_KEY)
        .endpoints(vec![endpoint.clone()]).map_err(|e| e.to_string())?
        .timeout(Duration::from_secs(600)).build().map_err(|e| e.to_string())?;
    let update = updater.check().await.map_err(|e| e.to_string())?;
    if let Some(ref update) = update {
        if !allowed_package(&update.download_url, &endpoint) { return Err("update_download_origin_rejected".into()); }
    }
    let cached = update.as_ref().is_some_and(|u| {
        let Ok(path) = cache_path(app, u) else { return false; };
        let Ok(bytes) = std::fs::read(&path) else { return false; };
        if u.verify(&bytes).is_ok() { true } else { let _ = std::fs::remove_file(path); false }
    });
    let mut status = state.status.lock().unwrap();
    status.available_version = update.as_ref().map(|u| u.version.clone());
    status.notes = update.as_ref().and_then(|u| u.body.clone());
    status.downloaded = 0; status.total = None; status.failures = 0;
    status.state = if cached && status.install_requested { "waiting" } else if cached { "ready" }
        else if update.is_some() { "available" } else { "up_to_date" }.into();
    if !cached { status.install_requested = false; status.waiting_for = None; }
    *state.pending.lock().unwrap() = update;
    Ok(())
}

async fn download(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<UpdateState>();
    let update = state.pending.lock().unwrap().clone().ok_or("update_not_available")?;
    let path = cache_path(app, &update)?;
    // Always download/verify before declaring ready; cached files are reverified by install().
    { let mut status = state.status.lock().unwrap(); status.state = "downloading".into(); status.downloaded = 0; }
    let bytes = update.download(|length, total| {
        let mut status = state.status.lock().unwrap();
        status.downloaded += length as u64; status.total = total;
    }, || {}).await.map_err(|e| e.to_string())?;
    let stage = path.with_extension("part");
    std::fs::write(&stage, bytes).map_err(|e| e.to_string())?;
    std::fs::rename(stage, path).map_err(|e| e.to_string())?;
    let mut status = state.status.lock().unwrap();
    status.state = "ready".into(); status.failures = 0;
    Ok(())
}

#[tauri::command]
pub async fn desktop_update_check(app: tauri::AppHandle) -> Result<Status, String> {
    let state = app.state::<UpdateState>(); let _busy = acquire(&state)?;
    check(&app).await.map_err(|e| failed(&app, e))?;
    if snapshot(&app).auto_download && snapshot(&app).state == "available" {
        download(&app).await.map_err(|e| failed(&app, e))?;
    }
    Ok(snapshot(&app))
}

#[tauri::command]
pub async fn desktop_update_download(app: tauri::AppHandle) -> Result<Status, String> {
    let state = app.state::<UpdateState>(); let _busy = acquire(&state)?;
    download(&app).await.map_err(|e| failed(&app, e))?;
    Ok(snapshot(&app))
}

/// Why Ryn cannot be replaced right now, if anything. Open documents are never closed
/// for the owner; running Agent work, transfers and plugin calls are left to finish.
fn blocker(app: &tauri::AppHandle) -> Option<String> {
    if app.webview_windows().keys().any(|label| label.starts_with("craft-")) { return Some("editors_open".into()); }
    let node = app.state::<crate::node::NodeState>();
    match crate::node::request(node.port, "GET", "/api/local/plugin-updates/readiness", "") {
        Ok(readiness) if readiness["ready"] == true => {}
        Ok(readiness) => return Some(readiness["blockers"].get(0).and_then(|b| b.as_str()).unwrap_or("plugin_busy").to_string()),
        Err(_) => return Some("node_unavailable".into()),
    }
    match crate::desktop::read_status(node.port) {
        Ok(status) if status.active_tasks > 0 || status.setup_active => Some("tasks_running".into()),
        Ok(_) => None,
        Err(_) => Some("node_unavailable".into()),
    }
}
/// "Update now" interrupts running work, but never discards an open document.
fn blocks(reason: &Option<String>, now: bool) -> bool {
    match reason.as_deref() { None => false, Some("editors_open") => true, Some(_) => !now }
}

/// The one-click "restart to update": install now when idle (or when the owner chose
/// `now`), otherwise remember the request and install as soon as Ryn becomes idle.
#[tauri::command]
pub async fn desktop_update_install(app: tauri::AppHandle, now: Option<bool>) -> Result<Status, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if !matches!(snapshot(&app).state.as_str(), "ready" | "waiting") { return Err("update_not_ready".to_string()); }
        let reason = blocker(&app);
        if blocks(&reason, now.unwrap_or(false)) {
            let state = app.state::<UpdateState>();
            let mut status = state.status.lock().unwrap();
            status.state = "waiting".into(); status.install_requested = true; status.waiting_for = reason;
            return Ok(status.clone());
        }
        install(&app, true).map(|_| snapshot(&app))
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub fn desktop_update_cancel(app: tauri::AppHandle) -> Status {
    let state = app.state::<UpdateState>();
    let mut status = state.status.lock().unwrap();
    if status.state == "waiting" { status.state = "ready".into(); }
    status.install_requested = false; status.waiting_for = None;
    status.clone()
}

fn install(app: &tauri::AppHandle, reopen: bool) -> Result<(), String> {
    let state = app.state::<UpdateState>(); let _busy = acquire(&state)?;
    let runtime = app.state::<crate::desktop::RuntimeState>();
    if runtime.action_busy.swap(true, Ordering::SeqCst) { return Err("update_busy".into()); }
    let _action = Busy(&runtime.action_busy);
    let update = state.pending.lock().unwrap().clone().ok_or("update_not_available")?.restart_after_install(reopen);
    let bytes = std::fs::read(cache_path(app, &update)?).map_err(|e| install_failed(app, e.to_string()))?;
    update.verify(&bytes).map_err(|e| install_failed(app, e.to_string()))?;
    state.status.lock().unwrap().state = "installing".into();
    // Let the shell show "Updating Ryn…" before the window goes away.
    std::thread::sleep(Duration::from_millis(1500));
    let node = app.state::<crate::node::NodeState>();
    crate::node::stop_for_update(node.inner()).map_err(|e| install_failed(app, e))?;
    let services = app.state::<crate::plugin_services::PluginServices>();
    services.stop();
    // On Windows a successful install starts the silent installer and exits Ryn; the
    // installer reopens Ryn when `reopen` is set.
    if let Err(error) = update.install(bytes) {
        if let Ok(path) = cache_path(app, &update) { let _ = std::fs::remove_file(path); }
        services.resume();
        let _ = crate::node::start(node.inner());
        runtime.reset_recovery.store(true, Ordering::SeqCst);
        return Err(install_failed(app, error.to_string()));
    }
    app.restart();
}

/// The version that will be installed when the owner quits Ryn, if any.
pub fn version_on_quit(app: &tauri::AppHandle) -> Option<String> {
    let status = snapshot(app);
    (status.install_on_quit && matches!(status.state.as_str(), "ready" | "waiting")).then_some(status.available_version).flatten()
}

/// Marks a quit the owner chose (not a restart or a system shutdown).
pub fn note_quit(app: &tauri::AppHandle) { app.state::<UpdateState>().quitting.store(true, Ordering::SeqCst); }

/// Runs after the node has stopped on an owner-chosen quit: installs a ready update
/// silently without reopening Ryn, so its next start is the new version.
pub fn install_on_quit(app: &tauri::AppHandle) {
    let state = app.state::<UpdateState>();
    if !state.quitting.load(Ordering::SeqCst) || version_on_quit(app).is_none() { return; }
    let Some(update) = state.pending.lock().unwrap().clone() else { return; };
    let Ok(bytes) = cache_path(app, &update).and_then(|path| std::fs::read(path).map_err(|e| e.to_string())) else { return; };
    if update.verify(&bytes).is_err() { return; }
    if let Err(error) = update.restart_after_install(false).install(bytes) {
        log::warn!("update on quit failed: {error}");
    }
}

/// The version this start was updated from, recording the current one for next time.
fn updated_from(previous: Option<&str>, current: &str) -> Option<String> {
    previous.filter(|p| !p.is_empty() && *p != current).map(str::to_string)
}

pub fn initialize(app: &tauri::AppHandle) {
    let mut preferences = read_preferences(app);
    let current = app.package_info().version.to_string();
    let from = updated_from(preferences.last_version.as_deref(), &current);
    if preferences.last_version.as_deref() != Some(current.as_str()) {
        preferences.last_version = Some(current.clone());
        if let Err(error) = write_preferences(app, &preferences) { log::warn!("could not record the running version: {error}"); }
    }
    *app.state::<UpdateState>().status.lock().unwrap() = Status {
        current_version: current, auto_download: preferences.auto_download, install_on_quit: preferences.install_on_quit,
        configured: !PUBLIC_KEY.trim().is_empty(), state: "idle".into(), endpoint: endpoint().to_string(),
        updated_from: from, ..Default::default()
    };
    let handle = app.clone();
    std::thread::spawn(move || {
        let jitter = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().subsec_millis() as u64 % 30;
        std::thread::sleep(Duration::from_secs(30 + jitter));
        let mut next_check = Instant::now();
        loop {
            if handle.state::<crate::node::NodeState>().stopping.load(Ordering::SeqCst) { break; }
            let status = snapshot(&handle);
            if status.install_requested && status.state == "waiting" {
                match blocker(&handle) {
                    None => { let _ = install(&handle, true); }
                    reason => { handle.state::<UpdateState>().status.lock().unwrap().waiting_for = reason; }
                }
            } else if status.configured && status.auto_download && Instant::now() >= next_check
                && !matches!(status.state.as_str(), "ready" | "waiting" | "checking" | "downloading" | "installing") {
                let ok = tauri::async_runtime::block_on(desktop_update_check(handle.clone())).is_ok();
                next_check = Instant::now() + if ok { CHECK_INTERVAL + Duration::from_secs(jitter * 20) } else { RETRY_INTERVAL };
            }
            // A sleeping worker does not keep the process alive.
            std::thread::sleep(TICK);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    fn url(value: &str) -> Url { value.parse().unwrap() }
    #[test]
    fn development_configuration_can_initialize_without_a_publishing_key() {
        for source in [include_str!("../tauri.conf.json"), include_str!("../tauri.updater.conf.json")] {
            let value: serde_json::Value = serde_json::from_str(source).unwrap();
            let config: tauri_plugin_updater::Config = serde_json::from_value(value["plugins"]["updater"].clone()).unwrap();
            assert_eq!(config.endpoints[0].as_str(), ENDPOINT);
            assert!(!config.allow_downgrades);
            assert!(!config.dangerous_insecure_transport_protocol);
            // Updates install silently; the click (or quitting) is the owner's consent.
            assert_eq!(value["plugins"]["updater"]["windows"]["installMode"], "quiet");
        }
    }
    #[test]
    fn release_builds_use_the_official_release() {
        if TEST_ENDPOINT.is_none() { assert_eq!(endpoint().as_str(), ENDPOINT); }
    }
    #[test]
    fn only_official_https_release_assets() {
        let official = url(ENDPOINT);
        assert!(allowed_package(&url("https://github.com/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe"), &official));
        for package in ["http://github.com/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe",
            "https://github.com/attacker/rynmesh/releases/download/v1/Ryn.exe",
            "https://user@github.com/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe",
            "https://github.com.evil.test/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe",
            "https://updates.example.test/ryn/Ryn.exe"] {
            assert!(!allowed_package(&url(package), &official), "{package}");
        }
    }
    #[test]
    fn a_test_server_serves_packages_from_its_own_origin() {
        let local = url("http://127.0.0.1:47811/desktop-latest.json");
        assert!(allowed_package(&url("http://127.0.0.1:47811/Ryn_0.8.91_x64-setup.exe"), &local));
        for package in ["http://127.0.0.1:47812/Ryn.exe", "http://localhost:47811/Ryn.exe",
            "https://github.com/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe"] {
            assert!(!allowed_package(&url(package), &local), "{package}");
        }
    }
    #[test]
    fn update_now_never_closes_open_documents() {
        assert!(!blocks(&None, false));
        assert!(blocks(&Some("agent_running".into()), false));
        assert!(!blocks(&Some("agent_running".into()), true));
        assert!(blocks(&Some("editors_open".into()), true));
    }
    #[test]
    fn preferences_default_to_automatic_and_keep_unknown_files_working() {
        let defaults: Preferences = serde_json::from_str("{}").unwrap();
        assert!(defaults.auto_download && defaults.install_on_quit);
        // The file written by earlier versions only had autoDownload.
        let old: Preferences = serde_json::from_str(r#"{"autoDownload":false}"#).unwrap();
        assert!(!old.auto_download && old.install_on_quit && old.last_version.is_none());
    }
    #[test]
    fn notices_an_update_only_when_the_version_changed() {
        assert_eq!(updated_from(Some("0.8.0"), "0.9.0").as_deref(), Some("0.8.0"));
        assert_eq!(updated_from(Some("0.9.0"), "0.9.0"), None);
        assert_eq!(updated_from(None, "0.9.0"), None);
    }
}
