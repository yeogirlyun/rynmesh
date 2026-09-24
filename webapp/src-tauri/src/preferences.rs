use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use tauri::Manager;

#[derive(Default)]
pub struct DesktopState { pub prefs: Mutex<Preferences> }
#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Preferences {
    pub background: bool,
    pub startup: bool,
    pub startup_supported: bool,
    pub silent_start: bool,
    pub auto_recover: bool,
    pub keep_awake: bool,
    pub close_notice_seen: bool,
}
impl Default for Preferences {
    fn default() -> Self {
        Self { background: true, startup: false, startup_supported: cfg!(windows),
            silent_start: true, auto_recover: true, keep_awake: false, close_notice_seen: false }
    }
}
fn settings_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    app.path().app_config_dir().map(|path| path.join("desktop.json")).map_err(|e| e.to_string())
}
pub fn load(app: &tauri::AppHandle) -> Preferences {
    let mut prefs = settings_path(app).ok().and_then(|p| std::fs::read(p).ok())
        .and_then(|bytes| serde_json::from_slice::<Preferences>(&bytes).ok()).unwrap_or_default();
    prefs.startup = startup_enabled();
    prefs.startup_supported = cfg!(windows);
    prefs
}
#[cfg(windows)]
fn registry_command() -> std::process::Command {
    use std::os::windows::process::CommandExt;
    let mut command = std::process::Command::new("reg.exe");
    command.creation_flags(0x08000000);
    command
}
#[cfg(windows)]
const RUN_KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run";
fn startup_enabled() -> bool {
    #[cfg(windows)] {
        let Ok(exe) = std::env::current_exe() else { return false; };
        return registry_command().args(["query", RUN_KEY, "/v", "Ryn"]).output()
            .map(|o| o.status.success() && String::from_utf8_lossy(&o.stdout).to_lowercase()
                .contains(&format!("\"{}\"", exe.to_string_lossy().to_lowercase())))
            .unwrap_or(false);
    }
    #[cfg(not(windows))] false
}
fn update_startup(prefs: &Preferences) -> Result<(), String> {
    #[cfg(windows)] {
        if !prefs.startup && !startup_enabled() { return Ok(()); }
        let mut command = registry_command();
        if prefs.startup {
            let exe = std::env::current_exe().map_err(|e| e.to_string())?;
            command.args(["add", RUN_KEY, "/v", "Ryn", "/t", "REG_SZ", "/d",
                &format!("\"{}\"{}", exe.display(), if prefs.silent_start { " --background" } else { "" }), "/f"]);
        } else { command.args(["delete", RUN_KEY, "/v", "Ryn", "/f"]); }
        if !command.output().map_err(|e| e.to_string())?.status.success() {
            return Err(crate::localization::tr("Windows could not update launch at sign-in."));
        }
    }
    #[cfg(not(windows))]
    if prefs.startup { return Err(crate::localization::tr("Launch at sign-in is available in the Windows app.")); }
    Ok(())
}
#[tauri::command]
pub fn get_desktop_preferences(state: tauri::State<DesktopState>) -> Preferences {
    let mut prefs = state.prefs.lock().unwrap().clone();
    prefs.startup = startup_enabled();
    prefs
}
#[tauri::command]
pub fn set_desktop_preferences(app: tauri::AppHandle, state: tauri::State<DesktopState>, preferences: Preferences) -> Result<Preferences, String> {
    let mut old = state.prefs.lock().unwrap();
    let mut prefs = preferences;
    prefs.startup_supported = cfg!(windows);
    prefs.close_notice_seen = old.close_notice_seen;
    let path = settings_path(&app)?;
    if let Some(parent) = path.parent() { std::fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
    let pending = path.with_extension("tmp");
    std::fs::write(&pending, serde_json::to_vec_pretty(&prefs).unwrap()).map_err(|e| e.to_string())?;
    update_startup(&prefs)?;
    if let Err(error) = std::fs::rename(pending, path) {
        let _ = update_startup(&old);
        return Err(error.to_string());
    }
    *old = prefs.clone();
    Ok(prefs)
}
pub fn acknowledge_close(app: &tauri::AppHandle) {
    let state = app.state::<DesktopState>();
    let mut prefs = state.prefs.lock().unwrap();
    prefs.close_notice_seen = true;
    if let Ok(path) = settings_path(app) {
        if let Some(parent) = path.parent() { let _ = std::fs::create_dir_all(parent); }
        let pending = path.with_extension("tmp");
        if std::fs::write(&pending, serde_json::to_vec_pretty(&*prefs).unwrap()).is_ok() {
            let _ = std::fs::rename(pending, path);
        }
    }
}
#[tauri::command]
pub fn desktop_node_port(state: tauri::State<crate::node::NodeState>) -> u16 { state.port }
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_preferences_gain_safe_defaults_without_losing_choices() {
        let p: Preferences = serde_json::from_str(r#"{"background":false,"startup":true,"startup_supported":true}"#).unwrap();
        assert!(!p.background); assert!(p.startup); assert!(p.auto_recover);
        assert!(p.silent_start); assert!(!p.keep_awake); assert!(!p.close_notice_seen);
    }
}
