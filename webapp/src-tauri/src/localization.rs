use std::sync::{atomic::{AtomicBool, Ordering}, Mutex, OnceLock};
use tauri::{menu::MenuItem, Manager, Wry};

static CHINESE: AtomicBool = AtomicBool::new(false);
static ZH: OnceLock<serde_json::Value> = OnceLock::new();

#[derive(Default)]
pub struct LocalizedMenu(pub Mutex<Vec<(&'static str, MenuItem<Wry>)>>);

fn translate(message: &str, chinese: bool) -> String {
    if chinese {
        let catalog = ZH.get_or_init(|| serde_json::from_str(include_str!("../../src/locales/ui.zh-CN.json")).expect("valid Chinese UI catalog"));
        if let Some(value) = catalog.get(message).and_then(|value| value.as_str()) { return value.to_owned(); }
    }
    message.to_owned()
}

pub fn tr(message: &str) -> String { translate(message, CHINESE.load(Ordering::SeqCst)) }

fn language_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    app.path().app_config_dir().map(|path| path.join("language.json")).map_err(|e| e.to_string())
}

pub fn initialize(app: &tauri::AppHandle) {
    let saved = language_path(app).ok().and_then(|path| std::fs::read(path).ok())
        .and_then(|bytes| serde_json::from_slice::<String>(&bytes).ok());
    let chinese = match saved.as_deref() {
        Some("zh-CN") => true,
        Some("en") => false,
        _ => system_is_chinese(),
    };
    CHINESE.store(chinese, Ordering::SeqCst);
}

fn system_is_chinese() -> bool {
    #[cfg(windows)] {
        #[link(name = "kernel32")]
        extern "system" { fn GetUserDefaultUILanguage() -> u16; }
        return unsafe { GetUserDefaultUILanguage() & 0x03ff == 0x04 };
    }
    #[cfg(not(windows))] {
        std::env::var("LC_ALL").or_else(|_| std::env::var("LANG")).unwrap_or_default().to_lowercase().starts_with("zh")
    }
}

#[tauri::command]
pub fn set_desktop_language(app: tauri::AppHandle, language: String) -> Result<(), String> {
    if language != "en" && language != "zh-CN" { return Err("unsupported_language".into()); }
    CHINESE.store(language == "zh-CN", Ordering::SeqCst);
    let status = app.state::<crate::desktop::RuntimeState>().status.lock().unwrap().clone();
    for (key, item) in app.state::<LocalizedMenu>().0.lock().unwrap().iter() {
        let message = match *key {
            "status" => crate::desktop::tray_status_key(&status),
            "sharing" => if status.sharing { "Pause AI Sharing" } else { "Resume AI Sharing" },
            key => key,
        };
        let _ = item.set_text(tr(message));
    }
    if let Some(tray) = app.tray_by_id("main") { let _ = tray.set_tooltip(Some(tr(crate::desktop::tray_status_key(&status)))); }
    let path = language_path(&app)?;
    let bytes = serde_json::to_vec(&language).map_err(|e| e.to_string())?;
    if std::fs::read(&path).ok().as_ref() != Some(&bytes) {
        if let Some(parent) = path.parent() { std::fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
        std::fs::write(path, bytes).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_copy_uses_the_ui_catalog_without_translating_paths_or_codes() {
        assert_eq!(translate("Open Ryn", true), "打开 Ryn");
        assert_eq!(translate("Open Ryn", false), "Open Ryn");
        assert_eq!(translate("node_unavailable", true), "node_unavailable");
        assert_eq!(translate("C:\\Users\\device\\diagnostics.json", true), "C:\\Users\\device\\diagnostics.json");
    }
}
