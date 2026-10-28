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
const LAUNCHABLE: &[&str] = &[
    "exe", "com", "scr", "pif", "msi", "msp", "msix", "appx", "appxbundle", "bat", "cmd", "ps1",
    "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "lnk", "url", "reg", "cpl", "msc",
    "jar", "application", "appref-ms", "gadget", "inf", "sct", "chm", "scf", "sh", "command",
    "tool", "desktop", "run", "appimage", "app", "pkg", "py", "pyw",
];

fn launchable(path: &std::path::Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| LAUNCHABLE.contains(&ext.to_ascii_lowercase().as_str()))
        .unwrap_or(false)
}

/// Opens or reveals a received path. Returns what was done ("open" | "reveal");
/// anything executable is only revealed, never launched.
#[tauri::command]
pub async fn drop_open_path(path: String, reveal: bool) -> Result<String, String> {
    let target = std::path::PathBuf::from(&path);
    if !target.is_absolute() || path.contains('"') {
        return Err("invalid path".into());
    }
    if !target.exists() {
        return Err("The received file was moved or deleted.".into());
    }
    let reveal = reveal || launchable(&target);
    open_native(&target, reveal).map_err(|error| error.to_string())?;
    Ok(if reveal { "reveal" } else { "open" }.into())
}

#[cfg(windows)]
fn open_native(target: &std::path::Path, reveal: bool) -> std::io::Result<()> {
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
fn open_native(target: &std::path::Path, reveal: bool) -> std::io::Result<()> {
    let mut command = std::process::Command::new("open");
    if reveal { command.arg("-R"); }
    command.arg(target).spawn().map(|_| ())
}

#[cfg(all(unix, not(target_os = "macos")))]
fn open_native(target: &std::path::Path, reveal: bool) -> std::io::Result<()> {
    let shown = if reveal { target.parent().unwrap_or(target) } else { target };
    std::process::Command::new("xdg-open").arg(shown).spawn().map(|_| ())
}


/// Export a local file through the native OS drag protocol.
#[tauri::command]
pub async fn drag_local_file(window: tauri::WebviewWindow, path: String) -> Result<(), String> {
    let target = std::path::PathBuf::from(path);
    if !target.is_absolute() || !target.is_file() {
        return Err("The file was moved or deleted.".into());
    }
    let (sender, receiver) = std::sync::mpsc::channel();
    let owner = window.clone();
    window.run_on_main_thread(move || {
        let result = drag::start_drag(
            #[cfg(target_os = "linux")]
            &owner.gtk_window().expect("desktop GTK window"),
            #[cfg(not(target_os = "linux"))]
            &owner,
            drag::DragItem::Files(vec![target]),
            drag::Image::Raw(include_bytes!("../icons/32x32.png").to_vec()),
            |_, _| {},
            drag::Options::default(),
        ).map_err(|error| error.to_string());
        let _ = sender.send(result);
    }).map_err(|error| error.to_string())?;
    tauri::async_runtime::spawn_blocking(move || receiver.recv().map_err(|error| error.to_string())?)
        .await.map_err(|error| error.to_string())?
}
