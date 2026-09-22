use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::Manager;

pub struct DesktopState {
    pub background: AtomicBool,
}

#[derive(Serialize, Deserialize)]
pub struct Preferences {
    pub background: bool,
    pub startup: bool,
    pub startup_supported: bool,
}

fn settings_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    app.path().app_config_dir().map(|path| path.join("desktop.json")).map_err(|e| e.to_string())
}

pub fn load_background(app: &tauri::AppHandle) -> bool {
    settings_path(app).ok().and_then(|path| std::fs::read(path).ok())
        .and_then(|bytes| serde_json::from_slice::<Preferences>(&bytes).ok())
        .map(|prefs| prefs.background).unwrap_or(true)
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
    #[cfg(windows)]
    {
        let Ok(exe) = std::env::current_exe() else { return false; };
        return registry_command().args(["query", RUN_KEY, "/v", "Ryn"])
            .output().map(|output| output.status.success() && String::from_utf8_lossy(&output.stdout).to_lowercase().contains(&exe.to_string_lossy().to_lowercase()))
            .unwrap_or(false);
    }
    #[cfg(not(windows))]
    false
}

#[tauri::command]
pub fn get_desktop_preferences(state: tauri::State<DesktopState>) -> Preferences {
    Preferences { background: state.background.load(Ordering::SeqCst), startup: startup_enabled(), startup_supported: cfg!(windows) }
}

#[tauri::command]
pub fn set_desktop_preferences(app: tauri::AppHandle, state: tauri::State<DesktopState>, background: bool, startup: bool) -> Result<Preferences, String> {
    if startup != startup_enabled() {
        #[cfg(windows)]
        {
            let mut command = registry_command();
            if startup {
                let exe = std::env::current_exe().map_err(|e| e.to_string())?;
                command.args(["add", RUN_KEY, "/v", "Ryn", "/t", "REG_SZ", "/d", &format!("\"{}\"", exe.display()), "/f"]);
            } else {
                command.args(["delete", RUN_KEY, "/v", "Ryn", "/f"]);
            }
            let output = command.output().map_err(|e| e.to_string())?;
            if !output.status.success() { return Err("Windows could not update launch at startup.".into()); }
        }
        #[cfg(not(windows))]
        return Err("Launch at startup is available in the Windows app.".into());
    }
    let prefs = Preferences { background, startup: startup_enabled(), startup_supported: cfg!(windows) };
    let path = settings_path(&app)?;
    if let Some(parent) = path.parent() { std::fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
    std::fs::write(path, serde_json::to_vec_pretty(&prefs).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    state.background.store(background, Ordering::SeqCst);
    Ok(prefs)
}

#[tauri::command]
pub fn desktop_node_port(state: tauri::State<crate::node::NodeState>) -> u16 { state.port }
