use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};
use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri::webview::{DownloadEvent, NewWindowResponse, PageLoadEvent};
use crate::node::{self, NodeState};
use crate::system_apps;

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

// Downloaded editor content gets no native commands. Built-ins load the exact same
// bundled, trusted frontend as main and need its node-port and desktop commands.
pub(crate) fn allows_native_commands(label: &str) -> bool {
    matches!(label, "main" | "craft-resume" | "craft-recruit" | "craft-desk" | "craft-nas")
}

// Only the trusted control window may supply a known app ID. URLs, executable
// paths and commands are never accepted from an editor, friend or model.
#[tauri::command]
pub async fn open_craft_browser(app: tauri::AppHandle, state: State<'_, NodeState>, app_id: String) -> Result<(), String> {
    open_editor(app, state.port, app_id, None).await
}

#[cfg(test)]
pub(crate) fn is_editor(app_id: &str) -> bool { editor_url(app_id).is_ok() }

/// A document comes from "Open with" or Finder, by a path the system passed to Ryn.
pub(crate) const MAX_DOCUMENT: u64 = 128 * 1024 * 1024;
/// The final readiness check follows the file-verifying launch, which also allows sixty seconds.
const EDITOR_READY: Duration = Duration::from_secs(60);
/// Every document gets a window of its own, so opening one never replaces unsaved work.
static DOCUMENT_WINDOWS: AtomicU32 = AtomicU32::new(0);

fn ready_catalog(port: u16, requested: &str) -> Result<serde_json::Value, String> {
    let started = Instant::now();
    let catalog = node::request_within(port, "GET", "/api/local/craft-plugins", "", EDITOR_READY)
        .map_err(|error| {
            log::warn!("local editor readiness catalog request failed for {requested} after {} ms: {error}", started.elapsed().as_millis());
            error
        })?;
    let trusted = catalog["plugins"].as_array().is_some_and(|rows| rows.iter().any(|p|
        p["id"].as_str() == Some(requested) && p["web"] == true
            && p["installed"] == true && p["enabled"] == true && p["running"] == true));
    if !trusted { return Err("plugin_launch_rejected".to_string()); }
    Ok(catalog)
}

/// Opens the window of a running editor, with `document` in a new window when given.
pub async fn open_editor(app: tauri::AppHandle, port: u16, app_id: String, document: Option<PathBuf>) -> Result<(), String> {
    let url = editor_url(&app_id)?;
    let requested = app_id.clone();
    let catalog = tauri::async_runtime::spawn_blocking(move || ready_catalog(port, &requested))
        .await.map_err(|_| "plugin_window_failed".to_string())??;

    let label = match document {
        Some(_) => format!("craft-{app_id}-{}", DOCUMENT_WINDOWS.fetch_add(1, Ordering::SeqCst) + 1),
        None => format!("craft-{app_id}"),
    };
    if let (None, Some(window)) = (&document, app.get_webview_window(&label)) {
        if !window.url().is_ok_and(|current| allows_navigation(&app_id, &current)) {
            return Err("plugin_launch_rejected".into());
        }
        window.show().and_then(|_| window.unminimize()).and_then(|_| window.set_focus())
            .map_err(|_| "plugin_window_failed".to_string())?;
        return Ok(()); // Never reload an open document when its app is opened again.
    }
    // Read before any window exists, so a missing or oversized file opens nothing.
    let opened = document.as_deref().map(read_document).transpose()?;

    let node_key = profile_key(catalog["storage_path"].as_str().ok_or("plugin_launch_rejected")?)?;
    let profile = crate::deployment::native_profile_dir(&app).map_err(|_| "plugin_window_failed")?
        .join("editors").join(node_key).join(&app_id);
    std::fs::create_dir_all(&profile).map_err(|_| "plugin_window_failed")?;
    let navigation_app = app_id.clone();
    let download_app = app_id.clone();
    let system_app = system_apps::find(&app_id);
    let app_name = system_app.map(|system_app| system_app.name.to_string()).unwrap_or_else(|| format!("{} — Ryn",
        catalog["plugins"].as_array().unwrap().iter()
            .find(|row| row["id"].as_str() == Some(app_id.as_str()))
            .and_then(|row| row["name"].as_str()).unwrap_or(&app_id)));
    let title = match &opened { Some((name, _)) => format!("{name} — {app_name}"), None => app_name };
    // A document saves next to itself unless the user picks another folder.
    let save_folder = document.as_deref().and_then(Path::parent).map(Path::to_path_buf);

    let mut builder = WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(url))
        .title(title).inner_size(1280.0, 850.0).min_inner_size(800.0, 560.0)
        .visible(false) // Shown once it carries the app's taskbar identity.
        .data_directory(profile)
        .disable_drag_drop_handler() // Keep the editor's standard HTML file-drop UI.
        .devtools(false)
        .on_navigation(move |target| allows_navigation(&navigation_app, target))
        .on_new_window(|_, _| NewWindowResponse::Deny)
        .on_download(move |_webview, event| {
            if let DownloadEvent::Requested { url, destination } = event {
                if !allows_download(&download_app, &url) { return false; }
                let filename = destination.file_name().and_then(|name| name.to_str()).unwrap_or("document");
                let mut dialog = rfd::FileDialog::new().set_file_name(filename);
                if let Some(folder) = &save_folder { dialog = dialog.set_directory(folder); }
                if let Some(chosen) = dialog.save_file() {
                    *destination = chosen;
                    return true;
                }
                return false;
            }
            true
        });
    if let Some(icon) = system_apps::window_icon(&app_id) {
        builder = builder.icon(icon).map_err(|_| "plugin_window_failed".to_string())?;
    }
    if let Some((name, bytes)) = opened {
        let scripts = Mutex::new(Some(document_scripts(&name, &bytes)));
        let page_app = app_id.clone();
        builder = builder.on_page_load(move |window, payload| {
            if payload.event() != PageLoadEvent::Finished || !allows_navigation(&page_app, payload.url()) { return; }
            if let Some(scripts) = scripts.lock().unwrap().take() {
                for script in scripts { let _ = window.eval(&script); }
            }
        });
    }
    let window = builder.build().map_err(|error| {
        log::error!("could not open local editor {app_id}: {error}");
        "plugin_window_failed".to_string()
    })?;
    if let Some(system_app) = system_app { system_apps::identify(&window, system_app); }
    window.show().and_then(|_| window.set_focus()).map_err(|_| "plugin_window_failed".to_string())
}

fn read_document(path: &Path) -> Result<(String, Vec<u8>), String> {
    let name = path.file_name().and_then(|name| name.to_str()).ok_or("document_unreadable")?.to_owned();
    let metadata = std::fs::metadata(path).map_err(|_| "document_unreadable")?;
    if !metadata.is_file() { return Err("document_unreadable".into()); }
    if metadata.len() > MAX_DOCUMENT { return Err("document_too_large".into()); }
    let bytes = std::fs::read(path).map_err(|_| "document_unreadable")?;
    Ok((name, bytes))
}

/// Whole base64 quads per script, so each part decodes on its own.
const DOCUMENT_PART: usize = 3 * 512 * 1024;

/// The editors take files only from their page, as a drop on their canvas; a document from the
/// system arrives the same way. The parts load first; the drop is retried until
/// the editor's handler acknowledges it, so an early canvas cannot lose the file.
fn document_scripts(name: &str, bytes: &[u8]) -> Vec<String> {
    use base64::Engine;
    let mut scripts = vec!["window.__rynDocument = [];".to_string()];
    for part in bytes.chunks(DOCUMENT_PART) {
        let encoded = base64::engine::general_purpose::STANDARD.encode(part);
        scripts.push(format!("window.__rynDocument.push(Uint8Array.from(atob(\"{encoded}\"), (c) => c.charCodeAt(0)));"));
    }
    scripts.push(DROP_DOCUMENT.replace("__NAME__", &serde_json::Value::from(name).to_string()));
    scripts
}

const DROP_DOCUMENT: &str = r#"(() => {
  const parts = window.__rynDocument;
  delete window.__rynDocument;
  if (!parts) return;
  const file = new File(parts, __NAME__);
  const started = Date.now();
  const editor = () => {
    const canvas = document.querySelector("canvas");
    return canvas && !document.querySelector('[id$="_loading"]') ? canvas : null;
  };
  const failed = () => {
    const notice = document.createElement("div");
    notice.setAttribute("role", "alert");
    notice.style.cssText = "position:fixed;top:12px;left:12px;right:12px;z-index:2147483647;padding:12px;background:#fff4e5;color:#332200;font:14px system-ui,sans-serif";
    notice.textContent = navigator.language.toLowerCase().startsWith("zh")
      ? "文件未能自动打开。请在“文件”菜单中选择“打开”，再选择这份文件。"
      : "The file could not be opened automatically. Use File > Open to choose it.";
    document.body.appendChild(notice);
  };
  const drop = () => {
    if (Date.now() - started >= 60000) {
      failed();
      return;
    }
    const canvas = editor();
    if (canvas) {
      const data = new DataTransfer();
      data.items.add(file);
      for (const type of ["dragenter", "dragover"]) {
        canvas.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: data }));
      }
      const event = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: data });
      canvas.dispatchEvent(event);
      if (event.defaultPrevented) return;
    }
    setTimeout(drop, 100);
  };
  drop();
})();"#;

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog_reply(status: u16, catalog: serde_json::Value, delay: Duration) -> Result<serde_json::Value, String> {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                request.push(byte[0]);
            }
            assert!(request.starts_with(b"GET /api/local/craft-plugins HTTP/1.0\r\n"));
            std::thread::sleep(delay);
            let body = catalog.to_string();
            let response = format!("HTTP/1.0 {status} Reply\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            stream.write_all(response.as_bytes()).unwrap();
        });
        let result = ready_catalog(port, "pdfcraft");
        server.join().unwrap();
        result
    }

    fn running_catalog() -> serde_json::Value {
        serde_json::json!({"plugins": [{"id": "pdfcraft", "web": true, "installed": true, "enabled": true, "running": true}]})
    }

    #[test]
    fn readiness_catalog_allows_a_reply_after_the_quick_request_budget() {
        assert_eq!(EDITOR_READY, Duration::from_secs(60));
        let expected = running_catalog();
        let started = Instant::now();
        assert_eq!(catalog_reply(200, expected.clone(), Duration::from_millis(2200)).unwrap(), expected);
        assert!(started.elapsed() >= Duration::from_millis(2200));
    }

    #[test]
    fn readiness_catalog_still_requires_the_exact_enabled_running_editor() {
        let mut wrong_id = running_catalog();
        wrong_id["plugins"][0]["id"] = "wordcraft".into();
        assert_eq!(catalog_reply(200, wrong_id, Duration::ZERO).unwrap_err(), "plugin_launch_rejected");
        for field in ["web", "installed", "enabled", "running"] {
            let mut catalog = running_catalog();
            catalog["plugins"][0][field] = false.into();
            assert_eq!(catalog_reply(200, catalog, Duration::ZERO).unwrap_err(), "plugin_launch_rejected", "{field}");
        }
    }

    #[test]
    fn readiness_catalog_preserves_the_node_request_error() {
        assert_eq!(catalog_reply(503, running_catalog(), Duration::ZERO).unwrap_err(), "node_request_rejected");
    }

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
        for label in ["craft-resume", "craft-recruit", "craft-desk", "craft-nas"] {
            assert!(allows_native_commands(label));
        }
        for label in ["craft-wordcraft", "craft-pdfcraft", "other", ""] {
            assert!(!allows_native_commands(label));
        }
        assert_eq!(profile_key("/cache/0123456789abcdef01234567").unwrap(), "0123456789abcdef01234567");
        for storage in ["/cache/..", "/cache/shared", "/cache/0123456789abcdef0123456Z", ""] {
            assert!(profile_key(storage).is_err());
        }
    }

    #[test]
    fn documents_enter_the_editor_as_a_dropped_file() {
        use base64::Engine;
        let bytes: Vec<u8> = (0..(DOCUMENT_PART * 2 + 10)).map(|n| (n % 251) as u8).collect();
        let scripts = document_scripts("Q3 \"final\".xlsx", &bytes);
        assert_eq!(scripts.len(), 5); // start, three parts, drop
        let mut decoded = Vec::new();
        for script in &scripts[1..4] {
            let encoded = script.split('"').nth(1).unwrap();
            decoded.extend(base64::engine::general_purpose::STANDARD.decode(encoded).unwrap());
        }
        assert_eq!(decoded, bytes);
        let drop = scripts.last().unwrap();
        assert!(drop.contains(r#"new File(parts, "Q3 \"final\".xlsx")"#));
        assert!(!drop.contains("__NAME__"));
        assert_eq!(document_scripts("empty.csv", &[]).len(), 2);
    }

    #[test]
    fn only_regular_files_within_the_limit_are_read() {
        let folder = std::env::temp_dir().join(format!("ryn-document-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        let file = folder.join("sheet.csv");
        std::fs::write(&file, b"a,b\n1,2\n").unwrap();
        assert_eq!(read_document(&file).unwrap(), ("sheet.csv".to_string(), b"a,b\n1,2\n".to_vec()));
        assert_eq!(read_document(&folder).unwrap_err(), "document_unreadable");
        assert_eq!(read_document(&folder.join("missing.csv")).unwrap_err(), "document_unreadable");
        std::fs::remove_dir_all(&folder).unwrap();
    }
}
