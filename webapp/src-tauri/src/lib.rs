mod node;
mod preferences;
mod desktop;
mod localization;
mod local_model;
use localization::tr;

use node::NodeState;
use std::sync::{Mutex, atomic::{AtomicBool, Ordering}};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};

fn focus_main(app: &tauri::AppHandle) {
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
    let port = std::env::var("RYNMESH_PEER_PORT").ok().and_then(|v| v.parse().ok()).unwrap_or(8791);
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            open_requested_page(app, &argv);
            if !argv.iter().any(|arg| arg == "--background") { focus_main(app); }
        }))
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![preferences::get_desktop_preferences,
            preferences::set_desktop_preferences, preferences::desktop_node_port,
            desktop::desktop_status, desktop::desktop_action, localization::set_desktop_language,
            local_model::choose_local_model])
        .manage(preferences::DesktopState::default())
        .manage(desktop::RuntimeState::default())
        .manage(localization::LocalizedMenu::default())
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
            if !silent { focus_main(app.handle()); }
            open_requested_page(app.handle(), &std::env::args().collect::<Vec<_>>());
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Err(e) = node::start(handle.state::<NodeState>().inner()) { log::error!("failed to start Ryn node: {e}"); }
            });
            desktop::watchdog(app.handle().clone(), status, sharing);
            Ok(())
        })
        .on_window_event(|window, event| {
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
        if let RunEvent::ExitRequested { .. } = event {
            // Only permanent shutdown sets this flag; manual restart never stops
            // the watchdog. After resume its next tick rechecks the node.
            handle.state::<NodeState>().stopping.store(true, Ordering::SeqCst);
            node::stop(handle.state::<NodeState>().inner());
        }
    });
}
