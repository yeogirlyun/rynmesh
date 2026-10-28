//! Desktop updates are a single signed bundle (UI + sidecar), never pip installs.
use serde::{Deserialize, Serialize};
use std::{sync::{Mutex, atomic::{AtomicBool, Ordering}}, time::{Duration, SystemTime, UNIX_EPOCH}};
use tauri::Manager;
use tauri_plugin_updater::{Update, UpdaterExt};

pub const ENDPOINT: &str = "https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json";
const PUBLIC_KEY: &str = match option_env!("RYN_DESKTOP_UPDATE_PUBLIC_KEY") { Some(key) => key, None => "" };

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Status {
    pub current_version: String,
    pub available_version: Option<String>,
    pub notes: Option<String>,
    pub state: String,
    pub auto_download: bool,
    pub configured: bool,
    pub downloaded: u64,
    pub total: Option<u64>,
    pub last_check: Option<u64>,
    pub last_error: Option<String>,
    pub endpoint: String,
}
#[derive(Default)]
pub struct UpdateState {
    pub status: Mutex<Status>,
    pending: Mutex<Option<Update>>,
    busy: AtomicBool,
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
fn cache_path(app: &tauri::AppHandle, update: &Update) -> Result<std::path::PathBuf, String> {
    // version originates from the updater's parsed SemVer, never a caller path.
    Ok(directory(app)?.join(format!("{}.update", update.version)))
}
fn snapshot(app: &tauri::AppHandle) -> Status { app.state::<UpdateState>().status.lock().unwrap().clone() }
fn failed(app: &tauri::AppHandle, error: String) -> String {
    let state = app.state::<UpdateState>();
    let mut status = state.status.lock().unwrap();
    status.state = "error".into(); status.last_error = Some(error.clone()); error
}

#[tauri::command]
pub fn desktop_update_status(app: tauri::AppHandle) -> Status { snapshot(&app) }

#[tauri::command]
pub fn desktop_update_preferences(app: tauri::AppHandle, auto_download: bool) -> Result<Status, String> {
    let state = app.state::<UpdateState>();
    let mut status = state.status.lock().unwrap();
    let path = directory(&app)?.join("preferences.json");
    let stage = path.with_extension("tmp");
    std::fs::write(&stage, serde_json::to_vec(&serde_json::json!({"autoDownload": auto_download})).unwrap()).map_err(|e| e.to_string())?;
    std::fs::rename(stage, path).map_err(|e| e.to_string())?;
    status.auto_download = auto_download;
    Ok(status.clone())
}

fn allowed_package(url: &tauri::Url) -> bool {
    url.scheme() == "https" && url.host_str() == Some("github.com")
        && url.username().is_empty() && url.password().is_none()
        && url.port_or_known_default() == Some(443)
        && url.path().starts_with("/yeogirlyun/rynmesh/releases/download/")
}

async fn check(app: &tauri::AppHandle) -> Result<(), String> {
    if PUBLIC_KEY.trim().is_empty() { return Err("update_signing_key_not_configured".into()); }
    let state = app.state::<UpdateState>();
    {
        let mut status = state.status.lock().unwrap();
        status.state = "checking".into(); status.last_error = None;
        status.last_check = Some(SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs());
    }
    let updater = app.updater_builder().pubkey(PUBLIC_KEY)
        .endpoints(vec![ENDPOINT.parse().unwrap()]).map_err(|e| e.to_string())?
        .timeout(Duration::from_secs(600)).build().map_err(|e| e.to_string())?;
    let update = updater.check().await.map_err(|e| e.to_string())?;
    if let Some(ref update) = update {
        if !allowed_package(&update.download_url) { return Err("update_download_origin_rejected".into()); }
    }
    let cached = update.as_ref().is_some_and(|u| {
        let Ok(path) = cache_path(app, u) else { return false; };
        let Ok(bytes) = std::fs::read(&path) else { return false; };
        if u.verify(&bytes).is_ok() { true } else { let _ = std::fs::remove_file(path); false }
    });
    let mut status = state.status.lock().unwrap();
    status.available_version = update.as_ref().map(|u| u.version.clone());
    status.notes = update.as_ref().and_then(|u| u.body.clone());
    status.downloaded = 0; status.total = None;
    status.state = if cached { "ready" } else if update.is_some() { "available" } else { "up_to_date" }.into();
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
    state.status.lock().unwrap().state = "ready".into();
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

#[tauri::command]
pub async fn desktop_update_install(app: tauri::AppHandle) -> Result<(), String> {
    // Installation is only initiated by the owner's explicit "restart and update" action.
    tauri::async_runtime::spawn_blocking(move || install(&app)).await.map_err(|e| e.to_string())?
}
fn install(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<UpdateState>(); let _busy = acquire(&state)?;
    if snapshot(app).state != "ready" { return Err("update_not_ready".into()); }
    if app.webview_windows().keys().any(|label| label.starts_with("craft-")) {
        return Err("update_close_editors_first".into());
    }
    let node = app.state::<crate::node::NodeState>();
    let readiness = crate::node::request(node.port, "GET", "/api/local/plugin-updates/readiness", "")
        .map_err(|_| "update_node_status_unavailable")?;
    if readiness["ready"] != true { return Err("update_plugins_agent_or_transfers_running".into()); }
    let status = crate::desktop::read_status(node.port).map_err(|_| "update_node_status_unavailable")?;
    if status.active_tasks > 0 || status.setup_active { return Err("update_tasks_running".into()); }
    let runtime = app.state::<crate::desktop::RuntimeState>();
    if runtime.action_busy.swap(true, Ordering::SeqCst) { return Err("update_busy".into()); }
    let _action = Busy(&runtime.action_busy);
    let update = state.pending.lock().unwrap().clone().ok_or("update_not_available")?;
    let bytes = std::fs::read(cache_path(app, &update)?).map_err(|e| e.to_string())?;
    update.verify(&bytes).map_err(|e| failed(app, e.to_string()))?;
    state.status.lock().unwrap().state = "installing".into();
    crate::node::stop_for_update(node.inner()).map_err(|e| failed(app, e))?;
    if let Err(error) = update.install(bytes) {
        if let Ok(path) = cache_path(app, &update) { let _ = std::fs::remove_file(path); }
        let _ = crate::node::start(node.inner());
        runtime.reset_recovery.store(true, Ordering::SeqCst);
        return Err(failed(app, error.to_string()));
    }
    app.restart();
}

pub fn initialize(app: &tauri::AppHandle) {
    let auto = directory(app).ok().and_then(|p| std::fs::read(p.join("preferences.json")).ok())
        .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
        .and_then(|v| v["autoDownload"].as_bool()).unwrap_or(true);
    *app.state::<UpdateState>().status.lock().unwrap() = Status {
        current_version: app.package_info().version.to_string(), auto_download: auto,
        configured: !PUBLIC_KEY.trim().is_empty(), state: "idle".into(), endpoint: ENDPOINT.into(), ..Default::default()
    };
    let handle = app.clone();
    std::thread::spawn(move || {
        let jitter = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().subsec_millis() as u64 % 30;
        std::thread::sleep(Duration::from_secs(30 + jitter));
        loop {
            if handle.state::<crate::node::NodeState>().stopping.load(Ordering::SeqCst) { break; }
            let status = snapshot(&handle);
            if status.configured && status.auto_download && status.state != "ready" {
                let _ = tauri::async_runtime::block_on(desktop_update_check(handle.clone()));
            }
            // A sleeping worker does not keep the process alive.
            std::thread::sleep(Duration::from_secs(6 * 3600 + jitter));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn development_configuration_can_initialize_without_a_publishing_key() {
        let value: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let config: tauri_plugin_updater::Config = serde_json::from_value(value["plugins"]["updater"].clone()).unwrap();
        assert_eq!(config.endpoints[0].as_str(), ENDPOINT);
        assert!(!config.allow_downgrades);
    }
    #[test]
    fn only_official_https_release_assets() {
        assert!(allowed_package(&"https://github.com/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe".parse().unwrap()));
        for url in ["http://github.com/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe",
            "https://github.com/attacker/rynmesh/releases/download/v1/Ryn.exe",
            "https://github.com.evil.test/yeogirlyun/rynmesh/releases/download/v1/Ryn.exe"] {
            assert!(!allowed_package(&url.parse().unwrap()));
        }
    }
}
