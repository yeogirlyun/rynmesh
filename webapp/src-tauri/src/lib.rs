mod node;
mod preferences;
mod desktop;
mod localization;
mod local_model;
mod drop;
mod drive_browser;
mod mcp_auth_browser;
mod craft_browser;
mod system_apps;
mod deployment;
mod updates;
use localization::tr;

use node::NodeState;
use std::sync::{Mutex, atomic::{AtomicBool, Ordering}};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};

pub(crate) fn focus_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show(); let _ = w.unminimize(); let _ = w.set_focus();
    }
}
fn open_requested_page(app: &tauri::AppHandle, args: &[String]) {
    if args.iter().any(|arg| arg == "--ai-sources") {
        if let Some(w) = app.get_webview_window("main") {
            if let Ok(current) = w.url() {
                if let Ok(url) = current.join("/services/sources?preset=zai") {
                    let _ = w.navigate(url);
                }
            }
        }
    }
}
fn dispatch(app: &tauri::AppHandle, action: &str) {
    let app = app.clone(); let action = action.to_string();
    std::thread::spawn(move || {
        if let Err(error) = desktop::run_action(&app, &action) { desktop::message(&error, false); }
    });
}
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    if let Err(error) = deployment::initialize() {
        desktop::message(&format!("Ryn deployment configuration error\n\n{error}"), false);
        return;
    }
    let port = std::env::var("RYNMESH_PEER_PORT").ok().and_then(|v| v.parse().ok()).unwrap_or(8791);
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            // "Ryn Excel" from the Start menu, a launcher or "Open with": only that app's window.
            if let Some(request) = system_apps::from_args(&argv) { system_apps::launch(app, request); return; }
            open_requested_page(app, &argv);
            if !argv.iter().any(|arg| arg == "--background") { focus_main(app); }
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(|invoke| {
            if !craft_browser::allows_native_commands(invoke.message.webview_ref().label()) {
                invoke.resolver.reject("plugin_native_access_denied");
                return true;
            }
            let handler: fn(tauri::ipc::Invoke<tauri::Wry>) -> bool = tauri::generate_handler![preferences::get_desktop_preferences,
            preferences::set_desktop_preferences, preferences::desktop_node_port,
            updates::desktop_update_status, updates::desktop_update_check, updates::desktop_update_download,
            updates::desktop_update_install, updates::desktop_update_preferences,
            desktop::desktop_status, desktop::desktop_action, localization::set_desktop_language,
            local_model::choose_local_model, drive_browser::open_drive_browser,
            mcp_auth_browser::open_mcp_auth_browser,
            craft_browser::open_craft_browser, system_apps::sync_system_apps, system_apps::system_app_info,
            system_apps::choose_default_system_app,
            drop::choose_drop_paths, drop::choose_drop_dir, drop::drop_open_path];
            handler(invoke)
        })
        .manage(preferences::DesktopState::default())
        .manage(desktop::RuntimeState::default())
        .manage(localization::LocalizedMenu::default())
        .manage(updates::UpdateState::default())
        .manage(NodeState { child: Mutex::new(None), port, stopping: AtomicBool::new(false), lifecycle: Mutex::new(()) })
        .setup(move |app| {
            localization::initialize(app.handle());
            let prefs = preferences::load(app.handle());
            let silent = prefs.silent_start && std::env::args().any(|arg| arg == "--background");
            *app.state::<preferences::DesktopState>().prefs.lock().unwrap() = prefs;
            app.handle().plugin(tauri_plugin_log::Builder::default().level(log::LevelFilter::Info).build())?;
            let status = MenuItem::with_id(app, "status", tr("Ryn - Starting node..."), false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", tr("Open Ryn"), true, None::<&str>)?;
            let sharing = MenuItem::with_id(app, "sharing", tr("Pause AI Sharing"), false, None::<&str>)?;
            let logs = MenuItem::with_id(app, "logs", tr("Open Logs"), true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart", tr("Restart Node..."), true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", tr("Quit Ryn..."), true, None::<&str>)?;
            *app.state::<localization::LocalizedMenu>().0.lock().unwrap() = vec![
                ("status", status.clone()), ("sharing", sharing.clone()), ("Open Ryn", open.clone()),
                ("Open Logs", logs.clone()), ("Restart Node...", restart.clone()), ("Quit Ryn...", quit.clone()),
            ];
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&status, &open, &sharing, &logs, &restart, &separator, &quit])?;
            let mut tray = TrayIconBuilder::with_id("main").menu(&menu);
            if let Some(icon) = app.default_window_icon().cloned() { tray = tray.icon(icon); }
            tray.on_menu_event(|app, event| match event.id().as_ref() {
                "open" => focus_main(app),
                "sharing" => {
                    let sharing = app.state::<desktop::RuntimeState>().status.lock().unwrap().sharing;
                    dispatch(app, if sharing { "pause" } else { "resume" });
                }
                action @ ("logs" | "restart" | "quit") => dispatch(app, action),
                _ => {}
            }).build(app)?;
            let args = std::env::args().collect::<Vec<_>>();
            if let Some(request) = system_apps::from_args(&args) { system_apps::launch(app.handle(), request); }
            else if !silent { system_apps::show_main_at_start(app.handle()); }
            open_requested_page(app.handle(), &args);
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Err(e) = node::start(handle.state::<NodeState>().inner()) { log::error!("failed to start Ryn node: {e}"); }
            });
            desktop::watchdog(app.handle().clone(), status, sharing);
            system_apps::sync_when_ready(app.handle());
            updates::initialize(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Plugin windows close normally; only the main window owns tray/quit behavior.
            if window.label() != "main" {
                if (window.label().starts_with("craft-") || window.label().starts_with("starting-"))
                    && matches!(event, WindowEvent::CloseRequested { .. }) {
                    system_apps::clear_identity(window);
                }
                if window.label().starts_with("craft-") && matches!(event, WindowEvent::Destroyed) {
                    system_apps::editor_closed(window.app_handle(), window.label());
                }
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let prefs = window.state::<preferences::DesktopState>().prefs.lock().unwrap().clone();
                if prefs.background {
                    let _ = window.hide();
                    if !prefs.close_notice_seen {
                        preferences::acknowledge_close(window.app_handle());
                        std::thread::spawn(|| { desktop::message(&tr("Ryn is still running in the system tray.\n\nYour shared services remain available while this computer is awake. Use the tray to reopen Ryn, pause AI sharing, or quit.\n\nChange this in Settings > Desktop."), false); });
                    }
                } else { dispatch(window.app_handle(), "quit"); }
            }
        })
        .build(tauri::generate_context!()).expect("error while running tauri application");
    app.run(|handle, event| {
        // Documents a launcher such as "Ryn Excel" passes on (open -b), or dropped on Ryn itself:
        // each opens in the app for its type.
        #[cfg(target_os = "macos")]
        if let RunEvent::Opened { urls } = &event {
            for path in urls.iter().filter_map(|url| url.to_file_path().ok()) {
                if let Some(request) = system_apps::for_document(&path) { system_apps::launch(handle, request); }
            }
        }
        if let RunEvent::ExitRequested { .. } = event {
            // Only permanent shutdown sets this flag; manual restart never stops
            // the watchdog. After resume its next tick rechecks the node.
            handle.state::<NodeState>().stopping.store(true, Ordering::SeqCst);
            node::stop(handle.state::<NodeState>().inner());
        }
    });
}
