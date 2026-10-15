use std::path::Path;
use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use crate::node::{self, NodeState};

const APPS: [&str; 13] = ["cadcraft", "deckcraft", "designcraft", "effectcraft", "figcraft",
    "filmcraft", "gridcraft", "lightcraft", "pdfcraft", "photocraft", "soundcraft", "vectorcraft", "wordcraft"];

fn editor_url(app_id: &str) -> Result<tauri::Url, String> {
    if app_id == "filmcraft" { return Err("craft_native_only".into()); }
    let index = APPS.iter().position(|name| *name == app_id).ok_or("plugin_launch_rejected")?;
    tauri::Url::parse(&format!("http://127.0.0.1:{}/", 8920 + index))
        .map_err(|_| "plugin_launch_rejected".into())
}

fn allows_navigation(app_id: &str, target: &tauri::Url) -> bool {
    editor_url(app_id).is_ok_and(|expected| target.origin() == expected.origin()
        && target.username().is_empty() && target.password().is_none())
}

fn allows_download(app_id: &str, target: &tauri::Url) -> bool {
    match target.scheme() {
        "blob" => tauri::Url::parse(target.path()).is_ok_and(|url| allows_navigation(app_id, &url)),
        "data" => true, // Locally generated exports still require the native Save As dialog.
        _ => allows_navigation(app_id, target),
    }
}

fn profile_key(storage: &str) -> Result<String, String> {
    let key = Path::new(storage).file_name().and_then(|value| value.to_str())
        .ok_or("plugin_launch_rejected")?;
    if key.len() != 24 || !key.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) {
        return Err("plugin_launch_rejected".into());
    }
    Ok(key.to_owned())
}

// Editor content gets no native commands, including commands outside this module.
pub(crate) fn allows_native_commands(label: &str) -> bool { label == "main" }

// Only the trusted control window may supply a known app ID. URLs, executable
// paths and commands are never accepted from an editor, friend or model.
#[tauri::command]
pub async fn open_craft_browser(app: tauri::AppHandle, state: State<'_, NodeState>, app_id: String) -> Result<(), String> {
    let url = editor_url(&app_id)?;
    let port = state.port;
    let requested = app_id.clone();
    let catalog = tauri::async_runtime::spawn_blocking(move || {
        let catalog = node::request(port, "GET", "/api/local/craft-plugins", "")?;
        let trusted = catalog["plugins"].as_array().is_some_and(|rows| rows.iter().any(|p|
            p["id"].as_str() == Some(requested.as_str()) && p["web"] == true
                && p["installed"] == true && p["enabled"] == true && p["running"] == true));
        if !trusted { return Err("plugin_launch_rejected".to_string()); }
        Ok(catalog)
    }).await.map_err(|_| "plugin_window_failed".to_string())??;

    let label = format!("craft-{app_id}");
    if let Some(window) = app.get_webview_window(&label) {
        if !window.url().is_ok_and(|current| allows_navigation(&app_id, &current)) {
            return Err("plugin_launch_rejected".into());
        }
        window.show().and_then(|_| window.unminimize()).and_then(|_| window.set_focus())
            .map_err(|_| "plugin_window_failed".to_string())?;
        return Ok(()); // Never reload an open document when its app is opened again.
    }

    let node_key = profile_key(catalog["storage_path"].as_str().ok_or("plugin_launch_rejected")?)?;
    let profile = app.path().app_local_data_dir().map_err(|_| "plugin_window_failed")?
        .join("editors").join(node_key).join(&app_id);
    std::fs::create_dir_all(&profile).map_err(|_| "plugin_window_failed")?;
    let navigation_app = app_id.clone();
    let download_app = app_id.clone();
    let title = format!("{} — Ryn", catalog["plugins"].as_array().unwrap().iter()
        .find(|row| row["id"].as_str() == Some(app_id.as_str()))
        .and_then(|row| row["name"].as_str()).unwrap_or(&app_id));

    WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(url))
        .title(title).inner_size(1280.0, 850.0).min_inner_size(800.0, 560.0)
        .data_directory(profile)
        .disable_drag_drop_handler() // Keep the editor's standard HTML file-drop UI.
        .devtools(false)
        .on_navigation(move |target| allows_navigation(&navigation_app, target))
        .on_new_window(|_, _| NewWindowResponse::Deny)
        .on_download(move |_webview, event| {
            if let DownloadEvent::Requested { url, destination } = event {
                if !allows_download(&download_app, &url) { return false; }
                let filename = destination.file_name().and_then(|name| name.to_str()).unwrap_or("document");
                if let Some(chosen) = rfd::FileDialog::new().set_file_name(filename).save_file() {
                    *destination = chosen;
                    return true;
                }
                return false;
            }
            true
        })
        .build().map_err(|error| {
            log::error!("could not open local editor {app_id}: {error}");
            "plugin_window_failed".to_string()
        })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_fixed_app_origins() {
        assert_eq!(editor_url("wordcraft").unwrap().as_str(), "http://127.0.0.1:8932/");
        assert_eq!(editor_url("pdfcraft").unwrap().as_str(), "http://127.0.0.1:8928/");
        for value in ["filmcraft", "https://evil.test", "wordcraft/../", "cmd.exe", ""] {
            assert!(editor_url(value).is_err());
        }
    }

    #[test]
    fn editor_cannot_navigate_to_control_api_other_apps_or_external_sites() {
        assert!(allows_navigation("wordcraft", &tauri::Url::parse("http://127.0.0.1:8932/document").unwrap()));
        for target in ["http://127.0.0.1:8791/api/local/plugins", "http://127.0.0.1:8928/",
            "http://localhost:8932/", "https://example.com/", "file:///C:/Windows/",
            "tauri://localhost/", "javascript:alert(1)", "http://user@127.0.0.1:8932/"] {
            assert!(!allows_navigation("wordcraft", &tauri::Url::parse(target).unwrap()), "{target}");
        }
        assert!(allows_download("wordcraft", &tauri::Url::parse("blob:http://127.0.0.1:8932/opaque").unwrap()));
        assert!(!allows_download("wordcraft", &tauri::Url::parse("blob:http://127.0.0.1:8791/opaque").unwrap()));
    }

    #[test]
    fn editor_has_no_native_commands_and_profiles_stay_separate() {
        assert!(allows_native_commands("main"));
        for label in ["craft-wordcraft", "craft-pdfcraft", "other", ""] {
            assert!(!allows_native_commands(label));
        }
        assert_eq!(profile_key("/cache/0123456789abcdef01234567").unwrap(), "0123456789abcdef01234567");
        for storage in ["/cache/..", "/cache/shared", "/cache/0123456789abcdef0123456Z", ""] {
            assert!(profile_key(storage).is_err());
        }
    }
}
