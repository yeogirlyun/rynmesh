use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::Manager;

// Device drop pickers return absolute local paths. The daemon streams the
// files from disk; nothing is read into or uploaded through the WebView.
#[tauri::command]
pub async fn choose_drop_paths(kind: String) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let dialog = rfd::FileDialog::new();
        let picked = match kind.as_str() {
            "folder" => dialog.pick_folders(),
            "files" => dialog.pick_files(),
            other => return Err(format!("unsupported picker kind: {other}")),
        };
        Ok(picked
            .unwrap_or_default()
            .into_iter()
            .map(|path| path.to_string_lossy().into_owned())
            .collect())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn choose_drop_dir() -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        rfd::FileDialog::new()
            .pick_folder()
            .map(|path| path.to_string_lossy().into_owned())
    })
    .await
    .map_err(|error| error.to_string())
}

// Received files are opened by the shell, not the node sidecar: the click comes
// from this foreground process, so Windows lets the opened app or Explorer
// window come to the front instead of flashing behind Ryn.
// Only passive document, image, audio, video and archive formats are opened
// directly; macro-enabled Office formats, HTML/SVG, scripts and binaries are
// revealed in their containing folder instead.
const OPENABLE: &[&str] = &[
    "pdf", "txt", "md", "csv", "tsv", "json",
    "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "mp3", "wav", "m4a", "flac", "ogg",
    "mp4", "mov", "webm", "mkv",
    "docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf",
    "zip",
];

pub(crate) fn is_openable(path: &Path) -> bool {
    path.is_file()
        && path
            .extension()
            .and_then(|ext| ext.to_str())
            .map(|ext| OPENABLE.contains(&ext.to_ascii_lowercase().as_str()))
            .unwrap_or(false)
}

#[derive(Default)]
pub struct OpenRootsState {
    pub roots: Mutex<Vec<PathBuf>>,
}

pub fn parse_open_roots_payload(value: &serde_json::Value) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Some(items) = value.get("roots").and_then(|v| v.as_array()) {
        for item in items {
            if let Some(raw) = item.as_str() {
                let p = PathBuf::from(raw.trim());
                if p.is_absolute() && !out.contains(&p) {
                    out.push(p);
                }
            }
        }
    }
    out
}

pub fn fetch_open_roots(port: u16) -> Result<Vec<PathBuf>, String> {
    let value = crate::node::request(port, "GET", "/api/local/desktop/open-roots", "")?;
    Ok(parse_open_roots_payload(&value))
}

pub fn refresh_open_roots(port: u16, state: &OpenRootsState) -> Result<Vec<PathBuf>, String> {
    let fresh = fetch_open_roots(port)?;
    *state.roots.lock().unwrap() = fresh.clone();
    Ok(fresh)
}

pub fn ensure_open_roots(port: u16, state: &OpenRootsState) -> Vec<PathBuf> {
    if let Ok(fresh) = refresh_open_roots(port, state) {
        return fresh;
    }
    state.roots.lock().unwrap().clone()
}

pub fn sync_open_roots_when_ready(app: &tauri::AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let port = app.state::<crate::node::NodeState>().port;
        if crate::node::wait_healthy(port) {
            if let Err(error) = refresh_open_roots(port, app.state::<OpenRootsState>().inner()) {
                log::warn!("could not sync desktop open-roots: {error}");
            }
        }
    });
}

#[tauri::command]
pub async fn refresh_drop_open_roots(
    node: tauri::State<'_, crate::node::NodeState>,
    roots: tauri::State<'_, OpenRootsState>,
) -> Result<Vec<String>, String> {
    let port = node.port;
    let updated = tauri::async_runtime::spawn_blocking(move || fetch_open_roots(port))
        .await
        .map_err(|error| error.to_string())??;
    *roots.roots.lock().unwrap() = updated.clone();
    Ok(updated.into_iter().map(|p| p.to_string_lossy().into_owned()).collect())
}

#[cfg(windows)]
fn strip_verbatim_prefix(path: &Path) -> PathBuf {
    let s = path.to_string_lossy();
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = s.strip_prefix(r"\\?\") {
        if rest.as_bytes().get(1) == Some(&b':') {
            return PathBuf::from(rest);
        }
    }
    path.to_path_buf()
}

#[cfg(not(windows))]
fn strip_verbatim_prefix(path: &Path) -> PathBuf {
    path.to_path_buf()
}

fn is_within_canonical_root(target: &Path, root: &Path) -> bool {
    #[cfg(windows)]
    {
        let mut target_parts = target.components();
        for root_part in root.components() {
            let Some(target_part) = target_parts.next() else {
                return false;
            };
            if !target_part
                .as_os_str()
                .to_string_lossy()
                .eq_ignore_ascii_case(&root_part.as_os_str().to_string_lossy())
            {
                return false;
            }
        }
        target_parts.next().is_some()
    }
    #[cfg(not(windows))]
    {
        target != root && target.starts_with(root)
    }
}

fn resolve_allowed_target(
    path: &str,
    allowed_roots: &[PathBuf],
    missing_message: &'static str,
    require_file: bool,
) -> Result<PathBuf, String> {
    let candidate = PathBuf::from(path);
    if path.is_empty() || !candidate.is_absolute() || path.contains('"') {
        return Err("invalid path".into());
    }
    let canonical = std::fs::canonicalize(&candidate).map_err(|_| String::from(missing_message))?;
    if require_file && !canonical.is_file() {
        return Err(missing_message.into());
    }
    let within_allowed = allowed_roots
        .iter()
        .filter_map(|root| std::fs::canonicalize(root).ok())
        .filter(|root| root.is_dir())
        .any(|root| is_within_canonical_root(&canonical, &root));
    if !within_allowed {
        return Err("path outside allowed directories".into());
    }
    Ok(strip_verbatim_prefix(&canonical))
}

pub(crate) fn open_path_with_launcher(
    path: &str,
    reveal: bool,
    allowed_roots: &[PathBuf],
    launcher: impl FnOnce(&Path, bool) -> std::io::Result<()>,
) -> Result<String, String> {
    let resolved = resolve_allowed_target(
        path,
        allowed_roots,
        "The received file was moved or deleted.",
        false,
    )?;
    let should_reveal = reveal || !is_openable(&resolved);
    launcher(&resolved, should_reveal).map_err(|error| error.to_string())?;
    Ok(if should_reveal { "reveal" } else { "open" }.into())
}

pub(crate) fn validate_drag_file(path: &str, allowed_roots: &[PathBuf]) -> Result<PathBuf, String> {
    resolve_allowed_target(path, allowed_roots, "The file was moved or deleted.", true)
}

/// Opens or reveals a received path. Returns what was done ("open" | "reveal");
/// non-allowlisted formats are only revealed, and paths outside allowed roots are rejected.
#[tauri::command]
pub async fn drop_open_path(
    node: tauri::State<'_, crate::node::NodeState>,
    roots: tauri::State<'_, OpenRootsState>,
    path: String,
    reveal: bool,
) -> Result<String, String> {
    let port = node.port;
    let allowed = tauri::async_runtime::spawn_blocking({
        let cached = roots.roots.lock().unwrap().clone();
        move || fetch_open_roots(port).unwrap_or(cached)
    })
    .await
    .map_err(|error| error.to_string())?;
    *roots.roots.lock().unwrap() = allowed.clone();
    open_path_with_launcher(&path, reveal, &allowed, open_native)
}

#[cfg(windows)]
fn open_native(target: &Path, reveal: bool) -> std::io::Result<()> {
    use std::os::windows::process::CommandExt;
    let mut command = std::process::Command::new("explorer.exe");
    if reveal {
        // explorer parses its own command line; "/select,"path"" keeps commas intact.
        command.raw_arg(format!("/select,\"{}\"", target.display()));
    } else {
        command.arg(target);
    }
    command.spawn().map(|_| ())
}

#[cfg(target_os = "macos")]
fn open_native(target: &Path, reveal: bool) -> std::io::Result<()> {
    let mut command = std::process::Command::new("open");
    if reveal {
        command.arg("-R");
    }
    command.arg(target).spawn().map(|_| ())
}

#[cfg(all(unix, not(target_os = "macos")))]
fn open_native(target: &Path, reveal: bool) -> std::io::Result<()> {
    let shown = if reveal {
        target.parent().unwrap_or(target)
    } else {
        target
    };
    std::process::Command::new("xdg-open").arg(shown).spawn().map(|_| ())
}

/// Export a local file through the native OS drag protocol.
#[tauri::command]
pub async fn drag_local_file(
    window: tauri::WebviewWindow,
    node: tauri::State<'_, crate::node::NodeState>,
    roots: tauri::State<'_, OpenRootsState>,
    path: String,
) -> Result<(), String> {
    let port = node.port;
    let allowed = tauri::async_runtime::spawn_blocking({
        let cached = roots.roots.lock().unwrap().clone();
        move || fetch_open_roots(port).unwrap_or(cached)
    })
    .await
    .map_err(|error| error.to_string())?;
    *roots.roots.lock().unwrap() = allowed.clone();
    let target = validate_drag_file(&path, &allowed)?;
    let (sender, receiver) = std::sync::mpsc::channel();
    let owner = window.clone();
    window
        .run_on_main_thread(move || {
            let result = drag::start_drag(
                #[cfg(target_os = "linux")]
                &owner.gtk_window().expect("desktop GTK window"),
                #[cfg(not(target_os = "linux"))]
                &owner,
                drag::DragItem::Files(vec![target]),
                drag::Image::Raw(include_bytes!("../icons/32x32.png").to_vec()),
                |_, _| {},
                drag::Options::default(),
            )
            .map_err(|error| error.to_string());
            let _ = sender.send(result);
        })
        .map_err(|error| error.to_string())?;
    tauri::async_runtime::spawn_blocking(move || receiver.recv().map_err(|error| error.to_string())?)
        .await
        .map_err(|error| error.to_string())?
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct DropPathInfo {
    pub path: String,
    pub size: u64,
    pub kind: &'static str,
}

pub(crate) fn inspect_path(path: String) -> DropPathInfo {
    let target = PathBuf::from(&path);
    if !target.is_absolute() {
        return DropPathInfo {
            path,
            size: 0,
            kind: "missing",
        };
    }
    match std::fs::metadata(&target) {
        Ok(meta) if meta.is_dir() => DropPathInfo {
            path,
            size: 0,
            kind: "dir",
        },
        Ok(meta) if meta.is_file() => DropPathInfo {
            path,
            size: meta.len(),
            kind: "file",
        },
        _ => DropPathInfo {
            path,
            size: 0,
            kind: "missing",
        },
    }
}

/// Read-only stat for dropped or picked local paths before staging composer attachments.
#[tauri::command]
pub async fn inspect_drop_paths(paths: Vec<String>) -> Result<Vec<DropPathInfo>, String> {
    tauri::async_runtime::spawn_blocking(move || paths.into_iter().map(inspect_path).collect())
        .await
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn inspects_file_size_directory_and_missing_path() {
        let dir = std::env::temp_dir().join(format!("ryn-drop-inspect-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("sample.txt");
        std::fs::write(&file, b"hello world").unwrap();

        let file_info = inspect_path(file.to_string_lossy().into_owned());
        assert_eq!(file_info.kind, "file");
        assert_eq!(file_info.size, 11);

        let dir_info = inspect_path(dir.to_string_lossy().into_owned());
        assert_eq!(dir_info.kind, "dir");
        assert_eq!(dir_info.size, 0);

        let missing_info = inspect_path(dir.join("absent.bin").to_string_lossy().into_owned());
        assert_eq!(missing_info.kind, "missing");
        assert_eq!(missing_info.size, 0);

        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn t1_macro_html_and_svg_only_reveal_location() {
        let base = std::env::temp_dir().join(format!("ryn-drop-t1-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        for name in [
            "a.docm",
            "a.xlsm",
            "a.pptm",
            "a.html",
            "a.htm",
            "a.svg",
            "a.iqy",
            "a.slk",
            "a.search-ms",
            "a.ps1xml",
            "a.dll",
            "a.dmg",
            "a.scpt",
        ] {
            let file = base.join(name);
            std::fs::write(&file, b"content").unwrap();
            let mut launched_reveal = None;
            let outcome = open_path_with_launcher(
                &file.to_string_lossy(),
                false,
                std::slice::from_ref(&base),
                |_target, is_reveal| {
                    launched_reveal = Some(is_reveal);
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(outcome, "reveal", "{name} must reveal only");
            assert_eq!(launched_reveal, Some(true), "{name} launcher reveal flag must be true");
        }

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn t2_pdf_opens_directly() {
        let base = std::env::temp_dir().join(format!("ryn-drop-t2-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        for name in ["a.pdf", "notes.txt", "readme.md", "photo.png", "sheet.xlsx", "deck.pptx", "archive.zip"] {
            let file = base.join(name);
            std::fs::write(&file, b"%PDF-1.4").unwrap();
            let mut launched_reveal = None;
            let outcome = open_path_with_launcher(
                &file.to_string_lossy(),
                false,
                std::slice::from_ref(&base),
                |_target, is_reveal| {
                    launched_reveal = Some(is_reveal);
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(outcome, "open", "{name} should open directly");
            assert_eq!(launched_reveal, Some(false), "{name} launcher reveal flag must be false");
        }

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn t3_paths_outside_allowed_roots_are_rejected() {
        let base = std::env::temp_dir().join(format!("ryn-drop-t3-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let recv_dir = base.join("recv");
        let ws_dir = base.join("workspace");
        let out_dir = base.join("agent-files");
        let outside_dir = base.join("outside");
        let sibling_prefix_dir = base.join("recv_evil");
        for d in [&recv_dir, &ws_dir, &out_dir, &outside_dir, &sibling_prefix_dir] {
            std::fs::create_dir_all(d).unwrap();
        }
        let outside_pdf = outside_dir.join("secret.pdf");
        let sibling_pdf = sibling_prefix_dir.join("trick.pdf");
        std::fs::write(&outside_pdf, b"secret").unwrap();
        std::fs::write(&sibling_pdf, b"trick").unwrap();
        let allowed = vec![recv_dir.clone(), ws_dir.clone(), out_dir.clone()];

        for denied in [&outside_pdf, &sibling_pdf] {
            assert!(
                open_path_with_launcher(&denied.to_string_lossy(), false, &allowed, |_, _| Ok(())).is_err(),
                "drop_open_path must reject {}",
                denied.display()
            );
            assert!(
                validate_drag_file(&denied.to_string_lossy(), &allowed).is_err(),
                "drag_local_file must reject {}",
                denied.display()
            );
        }

        // Also verify fetch_open_roots + refresh_open_roots against loopback /api/local/desktop/open-roots
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let recv_str = recv_dir.to_string_lossy().into_owned();
        let out_str = out_dir.to_string_lossy().into_owned();
        let server = std::thread::spawn(move || {
            for roots_json in [
                serde_json::json!({ "roots": [recv_str] }),
                serde_json::json!({ "roots": [out_str] }),
            ] {
                let (mut sock, _) = listener.accept().unwrap();
                let mut req = [0u8; 2048];
                let n = sock.read(&mut req).unwrap_or(0);
                let req_str = String::from_utf8_lossy(&req[..n]);
                assert!(req_str.starts_with("GET /api/local/desktop/open-roots "));
                let body = roots_json.to_string();
                let _ = sock.write_all(
                    format!("HTTP/1.0 200 OK\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes(),
                );
            }
        });

        let state = OpenRootsState::default();
        let initial = ensure_open_roots(port, &state);
        let recv_pdf = recv_dir.join("in_recv.pdf");
        let out_pdf = out_dir.join("in_out.pdf");
        std::fs::write(&recv_pdf, b"ok").unwrap();
        std::fs::write(&out_pdf, b"ok").unwrap();

        assert_eq!(
            open_path_with_launcher(&recv_pdf.to_string_lossy(), false, &initial, |_, _| Ok(())).unwrap(),
            "open"
        );
        assert!(open_path_with_launcher(&out_pdf.to_string_lossy(), false, &initial, |_, _| Ok(())).is_err());

        // Settings changed on node -> refresh replaces roots with out_dir
        let updated = ensure_open_roots(port, &state);
        assert!(open_path_with_launcher(&recv_pdf.to_string_lossy(), false, &updated, |_, _| Ok(())).is_err());
        assert_eq!(
            open_path_with_launcher(&out_pdf.to_string_lossy(), false, &updated, |_, _| Ok(())).unwrap(),
            "open"
        );
        assert!(validate_drag_file(&out_pdf.to_string_lossy(), &updated).is_ok());
        server.join().unwrap();

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn t4_parent_traversal_and_symlink_escape_are_rejected() {
        let base = std::env::temp_dir().join(format!("ryn-drop-t4-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let recv_dir = base.join("recv");
        let outside_dir = base.join("outside");
        std::fs::create_dir_all(&recv_dir).unwrap();
        std::fs::create_dir_all(&outside_dir).unwrap();
        let outside_file = outside_dir.join("escape.pdf");
        std::fs::write(&outside_file, b"outside").unwrap();
        let allowed = vec![recv_dir.clone()];

        // 1. `..` traversal escaping recv_dir
        let traversal = recv_dir.join("..").join("outside").join("escape.pdf");
        assert!(
            open_path_with_launcher(&traversal.to_string_lossy(), false, &allowed, |_, _| Ok(())).is_err(),
            ".. traversal must be rejected by drop_open_path"
        );
        assert!(
            validate_drag_file(&traversal.to_string_lossy(), &allowed).is_err(),
            ".. traversal must be rejected by drag_local_file"
        );

        // 2. Link / junction inside recv_dir pointing to outside_dir
        let link_dir = recv_dir.join("linked_out");
        #[cfg(windows)]
        {
            let status = std::process::Command::new("cmd")
                .args(["/C", "mklink", "/J"])
                .arg(&link_dir)
                .arg(&outside_dir)
                .output()
                .unwrap();
            assert!(status.status.success(), "mklink /J failed");
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside_dir, &link_dir).unwrap();
        }
        let escaped_via_link = link_dir.join("escape.pdf");
        assert!(escaped_via_link.exists());
        assert!(
            open_path_with_launcher(&escaped_via_link.to_string_lossy(), false, &allowed, |_, _| Ok(())).is_err(),
            "symlink/junction escape must be rejected by drop_open_path"
        );
        assert!(
            validate_drag_file(&escaped_via_link.to_string_lossy(), &allowed).is_err(),
            "symlink/junction escape must be rejected by drag_local_file"
        );

        #[cfg(windows)]
        let _ = std::fs::remove_dir(&link_dir);
        let _ = std::fs::remove_dir_all(&base);
    }
}
