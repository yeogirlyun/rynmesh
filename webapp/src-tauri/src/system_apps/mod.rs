//! Ryn's editors as apps of the operating system, the way an office suite installs Word and
//! Excel: "Ryn Excel" in the Windows Start menu or the macOS Applications folder opens only the
//! editor window, and its documents list it under "Open with". Ryn never claims a file type as
//! the default; the user chooses that in the system's own settings.
//!
//! Only installed and enabled editors and built-in apps are integrated. Each start syncs once the node is
//! ready, and the control window asks for a sync after it changes plugins.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use crate::{craft_browser, node::{self, NodeState}, preferences::DesktopState};

#[cfg(windows)]
mod win;
#[cfg(target_os = "macos")]
mod mac;
#[cfg(any(target_os = "macos", test))]
mod launcher_bundle;

/// The bundle identifier in tauri.conf.json; app identities derive from it.
pub const IDENTIFIER: &str = "ai.rynmesh.desktop";
/// Cold starts include the node itself, which may first migrate or verify its data.
const NODE_START: Duration = Duration::from_secs(120);
/// Starting an editor verifies every file of it before its origin is served.
const EDITOR_START: Duration = Duration::from_secs(60);
/// A launch faster than this shows no "starting" window.
const SPLASH_DELAY: Duration = Duration::from_millis(400);

pub struct Document {
    pub ext: &'static str,
    /// The macOS uniform type identifier, for Finder and the default-app choice.
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    pub uti: &'static str,
    pub kind: &'static str,
}

pub struct SystemApp {
    pub id: &'static str,
    pub name: &'static str,
    /// Types the editor opens and offers under "Open with"; never claimed as the default.
    pub documents: &'static [Document],
}

const fn doc(ext: &'static str, uti: &'static str, kind: &'static str) -> Document {
    Document { ext, uti, kind }
}

/// Every launchable app. FilmCraft is native-only and has no Ryn window to open.
pub const APPS: &[SystemApp] = &[
    SystemApp { id: "pdfcraft", name: "Ryn PDF", documents: &[doc("pdf", "com.adobe.pdf", "PDF document")] },
    SystemApp { id: "wordcraft", name: "Ryn Word", documents: &[
        doc("docx", "org.openxmlformats.wordprocessingml.document", "Word document"),
        doc("doc", "com.microsoft.word.doc", "Word 97-2003 document"),
        doc("odt", "org.oasis-open.opendocument.text", "OpenDocument text"),
        doc("rtf", "public.rtf", "Rich text document"),
    ] },
    SystemApp { id: "gridcraft", name: "Ryn Excel", documents: &[
        doc("xlsx", "org.openxmlformats.spreadsheetml.sheet", "Excel workbook"),
        doc("xls", "com.microsoft.excel.xls", "Excel 97-2003 workbook"),
        doc("ods", "org.oasis-open.opendocument.spreadsheet", "OpenDocument spreadsheet"),
        doc("csv", "public.comma-separated-values-text", "CSV file"),
    ] },
    SystemApp { id: "deckcraft", name: "Ryn PPT", documents: &[
        doc("pptx", "org.openxmlformats.presentationml.presentation", "PowerPoint presentation"),
        doc("ppsx", "org.openxmlformats.presentationml.slideshow", "PowerPoint slide show"),
    ] },
    SystemApp { id: "cadcraft", name: "Ryn CAD", documents: &[] },
    SystemApp { id: "designcraft", name: "Ryn Layout", documents: &[] },
    SystemApp { id: "effectcraft", name: "Ryn Effects", documents: &[] },
    SystemApp { id: "figcraft", name: "Ryn UI Design", documents: &[] },
    SystemApp { id: "lightcraft", name: "Ryn Photo", documents: &[] },
    SystemApp { id: "photocraft", name: "Ryn Image", documents: &[] },
    SystemApp { id: "soundcraft", name: "Ryn Audio", documents: &[] },
    SystemApp { id: "vectorcraft", name: "Ryn Vector", documents: &[] },
    SystemApp { id: "resume", name: "Ryn Resume", documents: &[] },
    SystemApp { id: "recruit", name: "Ryn Recruit", documents: &[] },
    SystemApp { id: "desk", name: "Ryn Desk", documents: &[] },
    SystemApp { id: "nas", name: "Ryn Drive", documents: &[] },
];

macro_rules! app_icons {
    ($($id:literal),*) => {
        /// The 64px window icon drawn by scripts/generate-app-icons.mjs.
        fn png(id: &str) -> Option<&'static [u8]> {
            match id { $($id => Some(include_bytes!(concat!("../../icons/apps/", $id, ".png"))),)* _ => None }
        }
        #[cfg(windows)]
        fn ico(id: &str) -> Option<&'static [u8]> {
            match id { $($id => Some(include_bytes!(concat!("../../icons/apps/", $id, ".ico"))),)* _ => None }
        }
        #[cfg(target_os = "macos")]
        fn icns(id: &str) -> Option<&'static [u8]> {
            match id { $($id => Some(include_bytes!(concat!("../../icons/apps/", $id, ".icns"))),)* _ => None }
        }
    };
}
app_icons!("cadcraft", "deckcraft", "designcraft", "effectcraft", "figcraft", "gridcraft",
    "lightcraft", "pdfcraft", "photocraft", "soundcraft", "vectorcraft", "wordcraft",
    "resume", "recruit", "desk", "nas");

impl SystemApp {
    /// Routes are owned by Ryn, never supplied by a plugin or command-line argument.
    fn builtin_route(&self) -> Option<&'static str> {
        match self.id {
            "resume" => Some("/apps/workspaces?app=resume"),
            "recruit" => Some("/apps/workspaces?app=recruit"),
            "desk" => Some("/apps/workspaces?app=desk"),
            "nas" => Some("/nas"),
            _ => None,
        }
    }
    pub fn opens(&self, path: &Path) -> bool {
        path.extension().and_then(|ext| ext.to_str())
            .is_some_and(|ext| self.documents.iter().any(|doc| doc.ext.eq_ignore_ascii_case(ext)))
    }
    /// The Windows taskbar identity shared by the app's shortcut, windows and file types.
    pub fn app_user_model_id(&self) -> String {
        format!("{IDENTIFIER}.{}", self.id)
    }
}

pub fn find(id: &str) -> Option<&'static SystemApp> {
    APPS.iter().find(|app| app.id == id)
}

#[derive(Debug)]
pub struct LaunchRequest {
    pub app: &'static SystemApp,
    pub document: Option<PathBuf>,
}

impl std::fmt::Debug for SystemApp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.id)
    }
}

/// `--app <id> [--open <path>]` from a shortcut, a launcher bundle or "Open with". A document of
/// a type the app does not open is dropped; the app still opens.
pub fn from_args(args: &[String]) -> Option<LaunchRequest> {
    let value = |flag: &str| args.iter().position(|arg| arg == flag).and_then(|index| args.get(index + 1));
    let app = find(value("--app")?)?;
    let document = value("--open").map(PathBuf::from).filter(|path| app.opens(path));
    Some(LaunchRequest { app, document })
}

/// A document the system handed to Ryn itself (macOS Finder): the app that opens its type.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn for_document(path: &Path) -> Option<LaunchRequest> {
    let app = APPS.iter().find(|app| app.opens(path))?;
    Some(LaunchRequest { app, document: Some(path.to_owned()) })
}

/// Set once the system asked for an app, so this start does not show the main window too.
static APP_REQUESTED: AtomicBool = AtomicBool::new(false);

/// Shows the main window at start unless an app was asked for instead. Finder delivers "Open
/// with" as an event just after start, so on macOS the decision waits a moment for it.
pub fn show_main_at_start(app: &tauri::AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let app = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            if !APP_REQUESTED.load(Ordering::SeqCst) { crate::focus_main(&app); }
        });
    }
    #[cfg(not(target_os = "macos"))]
    if !APP_REQUESTED.load(Ordering::SeqCst) { crate::focus_main(app); }
}

/// Launches on their way to a window. Ryn must not quit under them when the last editor closes.
struct LaunchTracker(AtomicUsize);

impl LaunchTracker {
    const fn new() -> Self { LaunchTracker(AtomicUsize::new(0)) }
    fn start(&'static self) -> Launching {
        self.0.fetch_add(1, Ordering::SeqCst);
        Launching(self)
    }
    fn pending(&self) -> usize { self.0.load(Ordering::SeqCst) }
}

/// One launch in flight, until its editor is open or its failure is on screen.
struct Launching(&'static LaunchTracker);

impl Drop for Launching {
    fn drop(&mut self) { self.0 .0.fetch_sub(1, Ordering::SeqCst); }
}

static LAUNCHES: LaunchTracker = LaunchTracker::new();

/// Opens the app's editor window (with its document) without the main window. When the editor
/// cannot open, for example after it was uninstalled, the main window opens on Apps instead.
pub fn launch(app: &tauri::AppHandle, request: LaunchRequest) {
    APP_REQUESTED.store(true, Ordering::SeqCst);
    // Counted before anything can close: an editor closing meanwhile must not end this launch.
    let launching = LAUNCHES.start();
    let app = app.clone();
    std::thread::spawn(move || {
        let splash = Splash::after(&app, request.app, SPLASH_DELAY);
        let id = request.app.id;
        let result = open(&app, request);
        splash.finish();
        // Window events run on the main thread, so finishing there orders this launch against any
        // editor that closed meanwhile: the failure page is shown before the quit check runs.
        let handle = app.clone();
        let finish = move || {
            if let Err(error) = result {
                log::warn!("could not open {id} for the system: {error}");
                show_main(&handle, "/apps");
            }
            drop(launching);
            // An editor that closed during this launch skipped the quit check; make it now.
            quit_if_idle(&handle, None);
        };
        if let Err(error) = app.run_on_main_thread(finish) {
            log::warn!("could not finish opening {id}: {error}");
        }
    });
}

fn open(app: &tauri::AppHandle, request: LaunchRequest) -> Result<(), String> {
    let port = app.state::<NodeState>().port;
    let id = request.app.id;
    let catalog = wait_for_catalog(port)?;
    if !integrated(&catalog).iter().any(|ready| ready.id == id) {
        return Err("plugin_not_ready".into());
    }
    if let Some(route) = request.app.builtin_route() {
        return open_builtin(app, request.app, route);
    }
    let preview = node::request_within(port, "POST", &format!("/api/local/craft-plugins/{id}/preview-launch"), "{}", EDITOR_START)?;
    let ticket = preview["ticket"].as_str().ok_or("invalid_response")?;
    let body = serde_json::json!({ "ticket": ticket }).to_string();
    let started = node::request_within(port, "POST", &format!("/api/local/craft-plugins/{id}/launch"), &body, EDITOR_START)?;
    if started["app"] != id {
        return Err("plugin_launch_rejected".into());
    }
    // open_editor verifies again that the node serves exactly this editor before loading it.
    tauri::async_runtime::block_on(craft_browser::open_editor(app.clone(), port, id.to_string(), request.document))
}

/// Built-in apps use Ryn's trusted frontend and the same NodeState as the control window.
/// External editor windows remain isolated from native commands in craft_browser.
fn open_builtin(app: &tauri::AppHandle, system_app: &'static SystemApp, route: &str) -> Result<(), String> {
    let label = format!("craft-{}", system_app.id);
    if let Some(window) = app.get_webview_window(&label) {
        return window.show().and_then(|_| window.unminimize()).and_then(|_| window.set_focus())
            .map_err(|_| "plugin_window_failed".to_string());
    }
    let frontend = app.get_webview_window("main").ok_or("plugin_window_failed")?
        .url().map_err(|_| "plugin_window_failed")?;
    // Use index.html so the packaged asset exists, then set the route before React starts.
    let route = serde_json::Value::from(route).to_string();
    let mut builder = WebviewWindowBuilder::new(app, &label, WebviewUrl::App("index.html".into()))
        .title(system_app.name).inner_size(1280.0, 850.0).min_inner_size(960.0, 640.0)
        .visible(false).devtools(false)
        .initialization_script(format!("history.replaceState(null, '', {route});"))
        .on_navigation(move |target| target.scheme() == frontend.scheme() && target.host_str() == frontend.host_str()
            && target.port_or_known_default() == frontend.port_or_known_default()
            && target.username().is_empty() && target.password().is_none())
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny);
    if let Some(icon) = window_icon(system_app.id) { builder = builder.icon(icon).map_err(|_| "plugin_window_failed")?; }
    let window = builder.build().map_err(|error| {
        log::error!("could not open built-in app {}: {error}", system_app.id);
        "plugin_window_failed".to_string()
    })?;
    identify(&window, system_app);
    window.show().and_then(|_| window.set_focus()).map_err(|_| "plugin_window_failed".to_string())
}

fn wait_for_catalog(port: u16) -> Result<serde_json::Value, String> {
    let deadline = Instant::now() + NODE_START;
    loop {
        match node::request(port, "GET", "/api/local/craft-plugins", "") {
            Ok(catalog) => return Ok(catalog),
            Err(error) if Instant::now() >= deadline => return Err(error),
            Err(_) => std::thread::sleep(Duration::from_millis(250)),
        }
    }
}

fn show_main(app: &tauri::AppHandle, path: &str) {
    if let Some(window) = app.get_webview_window("main") {
        if let Some(url) = window.url().ok().and_then(|current| current.join(path).ok()) {
            let _ = window.navigate(url);
        }
    }
    crate::focus_main(app);
}

/// Apps this catalog has installed and enabled, which get system entries. Built-ins must
/// match both the trusted adapter kind and its fixed route, not just a familiar ID.
pub(crate) fn integrated(catalog: &serde_json::Value) -> Vec<&'static SystemApp> {
    let rows = catalog["plugins"].as_array().map(Vec::as_slice).unwrap_or_default();
    APPS.iter().filter(|app| rows.iter().any(|row| row["id"] == app.id
        && row["installed"] == true && row["enabled"] == true
        && match app.builtin_route() {
            Some(route) => row["kind"] == "builtin_adapter" && row["launch_path"] == route,
            None => row["web"] == true,
        })).collect()
}

/// A small "starting Ryn Excel" window while the node or the editor is still starting.
struct Splash {
    label: String,
    app: tauri::AppHandle,
    finished: Arc<Mutex<bool>>,
}

impl Splash {
    fn after(app: &tauri::AppHandle, system_app: &'static SystemApp, delay: Duration) -> Self {
        let splash = Splash { label: format!("starting-{}", system_app.id), app: app.clone(), finished: Arc::new(Mutex::new(false)) };
        let (app, label, finished) = (app.clone(), splash.label.clone(), splash.finished.clone());
        std::thread::spawn(move || {
            std::thread::sleep(delay);
            let finished = finished.lock().unwrap();
            if *finished { return; }
            let page = serde_json::json!({ "name": system_app.name, "icon": png(system_app.id).map(png_data_url) });
            let built = WebviewWindowBuilder::new(&app, &label, WebviewUrl::App("app-launch.html".into()))
                .title(system_app.name).inner_size(360.0, 220.0).resizable(false).maximizable(false)
                .center().visible(false)
                .initialization_script(format!("window.__rynLaunch = {page};"))
                .build();
            match built {
                Ok(window) => {
                    if let Some(icon) = window_icon(system_app.id) { let _ = window.set_icon(icon); }
                    identify(&window, system_app);
                    let _ = window.show();
                }
                Err(error) => log::warn!("could not show the starting window of {}: {error}", system_app.id),
            }
        });
        splash
    }

    fn finish(self) {
        *self.finished.lock().unwrap() = true;
        if let Some(window) = self.app.get_webview_window(&self.label) { let _ = window.close(); }
    }
}

fn png_data_url(bytes: &[u8]) -> String {
    use base64::Engine;
    format!("data:image/png;base64,{}", base64::engine::general_purpose::STANDARD.encode(bytes))
}

/// The app's own window icon, for its editor and starting windows.
pub fn window_icon(id: &str) -> Option<tauri::image::Image<'static>> {
    let mut reader = png::Decoder::new(png(id)?).read_info().ok()?;
    let mut rgba = vec![0; reader.output_buffer_size()];
    let frame = reader.next_frame(&mut rgba).ok()?;
    if frame.color_type != png::ColorType::Rgba || frame.bit_depth != png::BitDepth::Eight { return None; }
    rgba.truncate(frame.buffer_size());
    Some(tauri::image::Image::new_owned(rgba, frame.width, frame.height))
}

/// Gives a window of the app its own taskbar button on Windows, pinned and relaunched as the app.
pub fn identify(window: &tauri::WebviewWindow, system_app: &'static SystemApp) {
    #[cfg(windows)]
    {
        let (Ok(hwnd), Ok(exe)) = (window.hwnd(), std::env::current_exe()) else { return };
        let Ok(icon) = win::icon_file(window.app_handle(), system_app) else { return };
        // The handle is a plain number; the window stays alive while its event loop runs this.
        let hwnd = hwnd.0 as isize;
        let _ = window.run_on_main_thread(move || {
            let hwnd = ::windows::Win32::Foundation::HWND(hwnd as *mut _);
            if let Err(error) = win::identify_window(hwnd, &exe, &icon, system_app) {
                log::warn!("could not set the taskbar identity of {}: {error}", system_app.id);
            }
        });
    }
    #[cfg(not(windows))]
    let _ = (window, system_app);
}

/// Called on the window event thread while the native window still exists.
pub fn clear_identity(window: &tauri::Window) {
    #[cfg(windows)]
    if let Ok(hwnd) = window.hwnd() {
        win::clear_window_identity(::windows::Win32::Foundation::HWND(hwnd.0));
    }
    #[cfg(not(windows))]
    let _ = window;
}

/// Called on the main thread when an editor window is gone.
pub fn editor_closed(app: &tauri::AppHandle, closed: &str) {
    quit_if_idle(app, Some(closed));
}

/// Runs on the main thread. `closing` is an editor still listed while its own close is handled.
fn quit_if_idle(app: &tauri::AppHandle, closing: Option<&str>) {
    let main_visible = app.get_webview_window("main").and_then(|window| window.is_visible().ok()).unwrap_or(true);
    let editors_open = app.webview_windows().keys()
        .any(|label| label.starts_with("craft-") && Some(label.as_str()) != closing);
    let background = app.state::<DesktopState>().prefs.lock().unwrap().background;
    if should_quit(APP_REQUESTED.load(Ordering::SeqCst), main_visible, editors_open, background, LAUNCHES.pending()) {
        app.exit(0);
    }
}

/// Started for an app alone, Ryn ends with that app's last window unless it runs in the
/// background, or another app is still starting.
fn should_quit(app_requested: bool, main_visible: bool, editors_open: bool, background: bool, launching: usize) -> bool {
    app_requested && !main_visible && !editors_open && !background && launching == 0
}

/// Brings the system's app entries in line with the installed, enabled editors. Fails when any
/// wanted app could not be set up; what happened to each app is kept for `system_app_info`.
pub fn sync(app: &tauri::AppHandle) -> Result<(), String> {
    // A development build would point the Start menu at target/debug; opt in to test it.
    if cfg!(debug_assertions) && std::env::var_os("RYN_SYSTEM_APPS").is_none() {
        let error = "system_apps_disabled_in_development".to_string();
        lock(&REPORT).error = Some(error.clone());
        return Err(error);
    }
    sync_with(
        &REPORT,
        || node::request(app.state::<NodeState>().port, "GET", "/api/local/craft-plugins", ""),
        |wanted| {
            #[cfg(windows)]
            return win::apply(app, wanted);
            #[cfg(target_os = "macos")]
            return mac::apply(wanted);
            #[cfg(not(any(windows, target_os = "macos")))]
            { let _ = wanted; Vec::new() }
        },
    )
}

/// What a sync did for one wanted app.
type Outcome = (&'static str, Result<(), String>);

/// What the last sync did. The UI reports this and what is really on the computer, never what
/// should be there because a plugin is installed.
pub(crate) struct Report {
    /// Syncs asked for and not finished, including one waiting for another to end.
    running: usize,
    /// Why the last sync could not run at all.
    error: Option<String>,
    /// Wanted apps the last sync could not set up, with the reason.
    failed: Vec<(&'static str, String)>,
}

impl Report {
    const fn new() -> Self { Report { running: 0, error: None, failed: Vec::new() } }
}

static REPORT: Mutex<Report> = Mutex::new(Report::new());

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// A sync counts as running from the moment it is asked for until it has reported.
struct Running(&'static Mutex<Report>);

impl Running {
    fn start(report: &'static Mutex<Report>) -> Self {
        lock(report).running += 1;
        Running(report)
    }
}

impl Drop for Running {
    fn drop(&mut self) { lock(self.0).running -= 1; }
}

// Include the catalog read in the lock: an older snapshot must never be applied after a
// newer sync, including a startup sync racing a plugin install, disable or uninstall.
static SYNC: Mutex<()> = Mutex::new(());

fn sync_with(
    report: &'static Mutex<Report>,
    catalog: impl FnOnce() -> Result<serde_json::Value, String>,
    apply: impl FnOnce(&[&'static SystemApp]) -> Vec<Outcome>,
) -> Result<(), String> {
    let _running = Running::start(report);
    let _sync = SYNC.lock().map_err(|_| "system_apps_failed".to_string())?;
    let wanted = match catalog() {
        Ok(catalog) => integrated(&catalog),
        Err(error) => {
            lock(report).error = Some(error.clone());
            return Err(error);
        }
    };
    let failed: Vec<_> = apply(&wanted).into_iter()
        .filter_map(|(id, result)| result.err().map(|error| (id, error))).collect();
    let first = failed.first().map(|(id, error)| format!("{id}: {error}"));
    let mut report = lock(report);
    report.error = None;
    report.failed = failed;
    first.map_or(Ok(()), Err)
}

/// The start-up sync, once the node answers.
pub fn sync_when_ready(app: &tauri::AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let result = wait_for_catalog(app.state::<NodeState>().port).and_then(|_| sync(&app));
        if let Err(error) = result { log::warn!("could not sync system apps: {error}"); }
    });
}

/// What the UI may say about an app: "ready" only when its entries are really in place.
fn status(report: &Report, id: &str, present: bool) -> (&'static str, Option<String>) {
    if report.running > 0 {
        return ("pending", None);
    }
    let failure = report.error.clone()
        .or_else(|| report.failed.iter().find(|(failed, _)| *failed == id).map(|(_, error)| error.clone()));
    match (failure, present) {
        (Some(error), _) => ("failed", Some(error)),
        (None, true) => ("ready", None),
        (None, false) => ("missing", None),
    }
}

/// Whether the app's Start menu or Applications entry and its file types exist right now.
fn present(app: &'static SystemApp) -> bool {
    #[cfg(windows)]
    return win::present(app);
    #[cfg(target_os = "macos")]
    return mac::present(app);
    #[cfg(not(any(windows, target_os = "macos")))]
    { let _ = app; false }
}

#[derive(serde::Serialize)]
pub struct SystemAppInfo {
    name: &'static str,
    /// ".xlsx" and the other types offered under "Open with".
    documents: Vec<String>,
    /// "start_menu" or "applications": where the app appears.
    place: &'static str,
    /// "settings": Windows opens its default-apps page. "direct": macOS sets the app as default.
    default_choice: &'static str,
    /// "ready": the entries exist. "pending": a sync is running. "failed": the last sync could not
    /// set the app up, see `error`. "missing": not on the computer (yet).
    state: &'static str,
    error: Option<String>,
}

#[tauri::command]
pub async fn system_app_info(app_id: String) -> Option<SystemAppInfo> {
    let app = find(&app_id)?;
    let (place, default_choice) = if cfg!(windows) { ("start_menu", "settings") }
        else if cfg!(target_os = "macos") { ("applications", "direct") } else { return None };
    let present = tauri::async_runtime::spawn_blocking(move || present(app)).await.unwrap_or(false);
    let (state, error) = status(&lock(&REPORT), app.id, present);
    Some(SystemAppInfo {
        name: app.name,
        documents: app.documents.iter().map(|doc| format!(".{}", doc.ext)).collect(),
        place,
        default_choice,
        state,
        error,
    })
}

#[tauri::command]
pub async fn sync_system_apps(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || sync(&app)).await.map_err(|_| "system_apps_failed".to_string())?
}

/// The user asked to make the app the default for its documents.
#[tauri::command]
pub async fn choose_default_system_app(app_id: String) -> Result<(), String> {
    let app = find(&app_id).filter(|app| !app.documents.is_empty()).ok_or("system_app_unknown")?;
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(windows)]
        return win::choose_default(app);
        #[cfg(target_os = "macos")]
        return mac::choose_default(app);
        #[cfg(not(any(windows, target_os = "macos")))]
        { let _ = app; Err("system_apps_unsupported".to_string()) }
    }).await.map_err(|_| "system_apps_failed".to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn apps_are_known_editors_with_unique_names_and_icons() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../../tauri.conf.json")).unwrap();
        assert_eq!(config["identifier"], IDENTIFIER);
        let mut names: Vec<_> = APPS.iter().map(|app| app.name.to_ascii_lowercase()).collect();
        names.sort();
        names.dedup();
        assert_eq!(names.len(), APPS.len());
        for app in APPS {
            assert!(craft_browser::is_editor(app.id) || app.builtin_route().is_some(), "{}", app.id);
            assert!(app.name.starts_with("Ryn ") && app.name.is_ascii(), "{}", app.name);
            let icon = window_icon(app.id).unwrap_or_else(|| panic!("{} has no window icon", app.id));
            assert_eq!((icon.width(), icon.height()), (64, 64));
            #[cfg(windows)]
            assert!(ico(app.id).is_some());
        }
        assert!(find("filmcraft").is_none() && find("").is_none());
        assert_eq!(find("gridcraft").unwrap().name, "Ryn Excel");
    }

    #[test]
    fn each_document_type_belongs_to_one_app() {
        let mut exts: Vec<_> = APPS.iter().flat_map(|app| app.documents.iter().map(|doc| doc.ext)).collect();
        let total = exts.len();
        exts.sort();
        exts.dedup();
        assert_eq!(exts.len(), total);
        assert_eq!(for_document(Path::new("/Users/a/Q3 report.XLSX")).unwrap().app.id, "gridcraft");
        assert_eq!(for_document(Path::new("C:\\a\\notes.docx")).unwrap().app.id, "wordcraft");
        assert!(for_document(Path::new("C:\\a\\tool.exe")).is_none());
        assert!(for_document(Path::new("C:\\a\\xlsx")).is_none());
    }

    #[test]
    fn launch_arguments_name_a_known_app_and_one_of_its_documents() {
        let request = from_args(&args(&["Ryn.exe", "--app", "gridcraft", "--open", "C:\\Data\\Q3 report.xlsx"])).unwrap();
        assert_eq!(request.app.id, "gridcraft");
        assert_eq!(request.document.unwrap(), PathBuf::from("C:\\Data\\Q3 report.xlsx"));
        assert!(from_args(&args(&["Ryn.exe", "--app", "gridcraft"])).unwrap().document.is_none());
        // A document of another type does not travel with the app.
        assert!(from_args(&args(&["Ryn.exe", "--app", "gridcraft", "--open", "C:\\x\\a.docx"])).unwrap().document.is_none());
        for rejected in [&["Ryn.exe"][..], &["Ryn.exe", "--app"], &["Ryn.exe", "--app", "filmcraft"],
            &["Ryn.exe", "--app", "../gridcraft"], &["Ryn.exe", "--background"], &["Ryn.exe", "--open", "C:\\a.xlsx"]] {
            assert!(from_args(&args(rejected)).is_none(), "{rejected:?}");
        }
    }

    #[test]
    fn only_installed_enabled_web_editors_are_integrated() {
        let catalog = serde_json::json!({ "plugins": [
            { "id": "gridcraft", "web": true, "installed": true, "enabled": true },
            { "id": "wordcraft", "web": true, "installed": true, "enabled": false },
            { "id": "deckcraft", "web": true, "installed": false, "enabled": true },
            { "id": "filmcraft", "web": false, "installed": true, "enabled": true },
            { "id": "resume", "web": false, "installed": true, "enabled": true },
        ] });
        let ids: Vec<_> = integrated(&catalog).iter().map(|app| app.id).collect();
        assert_eq!(ids, ["gridcraft"]);
        assert!(integrated(&serde_json::json!({})).is_empty());
    }

    #[test]
    fn builtin_menu_entries_require_an_enabled_install_and_the_trusted_route() {
        let catalog = serde_json::json!({ "plugins": [
            { "id": "resume", "kind": "builtin_adapter", "launch_path": "/apps/workspaces?app=resume", "installed": true, "enabled": true },
            { "id": "desk", "kind": "builtin_adapter", "launch_path": "/apps/workspaces?app=desk", "installed": true, "enabled": true },
            { "id": "recruit", "kind": "builtin_adapter", "launch_path": "/apps/workspaces?app=recruit", "installed": false, "enabled": true },
            { "id": "nas", "kind": "builtin_adapter", "launch_path": "/nas", "installed": true, "enabled": false },
        ] });
        assert_eq!(integrated(&catalog).iter().map(|app| app.id).collect::<Vec<_>>(), ["resume", "desk"]);
        let mut rejected = catalog.clone();
        rejected["plugins"][0]["launch_path"] = "https://example.com".into();
        rejected["plugins"][1]["kind"] = "web_editor".into();
        assert!(integrated(&rejected).is_empty());
        for id in ["resume", "recruit", "desk", "nas"] {
            let launch = from_args(&args(&["Ryn.exe", "--app", id])).unwrap();
            assert_eq!(launch.app.id, id);
            assert!(launch.app.builtin_route().is_some());
            assert!(launch.document.is_none());
        }
    }

    #[test]
    fn a_newer_sync_cannot_be_overwritten_by_an_older_catalog() {
        use std::sync::mpsc;
        let catalog = Arc::new(Mutex::new(serde_json::json!({ "plugins": [
            { "id": "gridcraft", "web": true, "installed": true, "enabled": true }
        ] })));
        let applied = Arc::new(Mutex::new(Vec::<&str>::new()));
        let (started, started_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let old_catalog = catalog.clone();
        let old_applied = applied.clone();
        static REPORT: Mutex<Report> = Mutex::new(Report::new());
        let older = std::thread::spawn(move || sync_with(
            &REPORT,
            || Ok(old_catalog.lock().unwrap().clone()),
            |wanted| {
                started.send(()).unwrap();
                release_rx.recv().unwrap();
                *old_applied.lock().unwrap() = wanted.iter().map(|app| app.id).collect();
                Vec::new()
            },
        ));
        started_rx.recv().unwrap();
        *catalog.lock().unwrap() = serde_json::json!({ "plugins": [] });
        let new_applied = applied.clone();
        let (selected, selected_rx) = mpsc::channel();
        let newer = std::thread::spawn(move || sync_with(
            &REPORT,
            || {
                selected.send(()).unwrap();
                Ok(catalog.lock().unwrap().clone())
            },
            |wanted| {
                *new_applied.lock().unwrap() = wanted.iter().map(|app| app.id).collect();
                Vec::new()
            },
        ));
        let selected_while_old_sync_was_applying = selected_rx.recv_timeout(Duration::from_millis(100)).is_ok();
        release.send(()).unwrap();
        older.join().unwrap().unwrap();
        newer.join().unwrap().unwrap();
        assert!(!selected_while_old_sync_was_applying);
        assert!(applied.lock().unwrap().is_empty(), "the disabled app must stay unregistered");
    }

    #[test]
    fn ryn_itself_claims_no_document_types() {
        // Each launcher declares its own types and disappears with its editor; Ryn.app declares none,
        // so a turned-off editor never stays under "Open With".
        let config: serde_json::Value = serde_json::from_str(include_str!("../../tauri.conf.json")).unwrap();
        assert!(config["bundle"]["fileAssociations"].is_null());
        let mac = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tauri.macos.conf.json")).unwrap_or_default();
        assert!(!mac.contains("fileAssociations"));
    }

    #[test]
    fn closing_the_last_editor_quits_only_when_nothing_else_is_coming() {
        // app requested, main visible, editors open, background, launches in flight
        assert!(should_quit(true, false, false, false, 0));
        assert!(!should_quit(true, false, false, false, 1), "a launch still on its way keeps Ryn running");
        assert!(!should_quit(true, false, false, false, 3));
        assert!(!should_quit(false, false, false, false, 0), "a normal start keeps its own close behaviour");
        assert!(!should_quit(true, true, false, false, 0));
        assert!(!should_quit(true, false, true, false, 0));
        assert!(!should_quit(true, false, false, true, 0));
    }

    #[test]
    fn every_launch_is_counted_until_it_finishes_or_fails() {
        static TRACKER: LaunchTracker = LaunchTracker::new();
        let first = TRACKER.start();
        let second = TRACKER.start();
        assert_eq!(TRACKER.pending(), 2);
        drop(first); // opened its window
        assert_eq!(TRACKER.pending(), 1);
        assert!(!should_quit(true, false, false, false, TRACKER.pending()));
        drop(second); // failed, its error page shown
        assert_eq!(TRACKER.pending(), 0);
        assert!(should_quit(true, false, false, false, TRACKER.pending()));
        let launches: Vec<_> = (0..8).map(|_| std::thread::spawn(|| {
            let launching = TRACKER.start();
            std::thread::sleep(Duration::from_millis(5));
            drop(launching);
        })).collect();
        for launch in launches { launch.join().unwrap(); }
        assert_eq!(TRACKER.pending(), 0, "concurrent launches all end");
    }

    #[test]
    fn the_ui_hears_what_really_happened() {
        static REPORT: Mutex<Report> = Mutex::new(Report::new());
        let catalog = || Ok(serde_json::json!({ "plugins": [
            { "id": "gridcraft", "web": true, "installed": true, "enabled": true },
            { "id": "wordcraft", "web": true, "installed": true, "enabled": true },
        ] }));
        let result = sync_with(&REPORT, catalog, |wanted| wanted.iter()
            .map(|app| (app.id, if app.id == "gridcraft" { Err("start_menu_failed: denied".to_string()) } else { Ok(()) }))
            .collect());
        assert_eq!(result.unwrap_err(), "gridcraft: start_menu_failed: denied");
        let report = lock(&REPORT);
        assert_eq!(status(&report, "gridcraft", true), ("failed", Some("start_menu_failed: denied".into())));
        assert_eq!(status(&report, "wordcraft", true), ("ready", None));
        assert_eq!(status(&report, "wordcraft", false), ("missing", None), "an installed plugin is not proof");
        drop(report);

        // A retry that works clears the failure.
        sync_with(&REPORT, catalog, |wanted| wanted.iter().map(|app| (app.id, Ok(()))).collect()).unwrap();
        assert_eq!(status(&lock(&REPORT), "gridcraft", true), ("ready", None));

        // A sync that cannot read the catalog fails every app until the next good one.
        assert!(sync_with(&REPORT, || Err("node_unavailable".to_string()), |_| unreachable!()).is_err());
        assert_eq!(status(&lock(&REPORT), "gridcraft", true), ("failed", Some("node_unavailable".into())));
    }

    #[test]
    fn a_running_sync_is_reported_as_pending() {
        static REPORT: Mutex<Report> = Mutex::new(Report::new());
        let (applying, applying_rx) = std::sync::mpsc::channel();
        let (release, release_rx) = std::sync::mpsc::channel::<()>();
        let sync = std::thread::spawn(move || sync_with(&REPORT,
            || Ok(serde_json::json!({ "plugins": [] })),
            |_| { applying.send(()).unwrap(); release_rx.recv().unwrap(); Vec::new() }));
        applying_rx.recv().unwrap();
        assert_eq!(status(&lock(&REPORT), "gridcraft", true), ("pending", None));
        release.send(()).unwrap();
        sync.join().unwrap().unwrap();
        assert_eq!(status(&lock(&REPORT), "gridcraft", false), ("missing", None));
    }

}
