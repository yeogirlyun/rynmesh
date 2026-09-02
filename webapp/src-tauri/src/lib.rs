mod node;

use node::NodeState;
use std::sync::atomic::AtomicBool;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};

fn focus_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let port: u16 = std::env::var("RYNMESH_PEER_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8791);

    let app = tauri::Builder::default()
        // Single-instance must be registered first; a second launch focuses
        // the running window instead of starting another node.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_main(app);
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_notification::init())
        .manage(NodeState {
            child: std::sync::Mutex::new(None),
            port,
            stopping: AtomicBool::new(false),
        })
        .setup(move |app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let open_i = MenuItem::with_id(app, "open", "Open Ryn", true, None::<&str>)?;
            let logs_i = MenuItem::with_id(app, "logs", "Open Logs", true, None::<&str>)?;
            let restart_i = MenuItem::with_id(app, "restart", "Restart Node", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&open_i, &logs_i, &restart_i, &sep, &quit_i])?;

            let mut tray = TrayIconBuilder::with_id("main").menu(&menu);
            if let Some(icon) = app.default_window_icon().cloned() {
                tray = tray.icon(icon);
            }
            tray.on_menu_event(|app, event| match event.id().as_ref() {
                "open" => focus_main(app),
                "logs" => {
                    if let Err(error) = node::open_log_dir() {
                        log::warn!("failed to open log directory: {error}");
                    }
                }
                "restart" => {
                    let app = app.clone();
                    std::thread::spawn(move || {
                        node::restart(app.state::<NodeState>().inner());
                    });
                }
                "quit" => {
                    node::stop(app.state::<NodeState>().inner());
                    app.exit(0);
                }
                _ => {}
            })
            .build(app)?;

            // Start the node off the UI thread; the webapp has its own
            // boot-wait so the window can paint immediately.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let state = handle.state::<NodeState>();
                if let Err(e) = node::start(state.inner()) {
                    log::error!("failed to start Ryn node: {e}");
                    return;
                }
                if node::wait_healthy(state.port) {
                    log::info!("Ryn node healthy on port {}", state.port);
                } else {
                    log::warn!("Ryn node did not become healthy within the boot window");
                }
            });

            // A laptop can sleep through daemon failure or lose the child to
            // an OS resource event. Three failed health checks trigger a
            // bounded restart; ordinary slow starts are left undisturbed.
            let watchdog = app.handle().clone();
            std::thread::spawn(move || {
                let mut failures = 0_u8;
                loop {
                    std::thread::sleep(Duration::from_secs(5));
                    let state = watchdog.state::<NodeState>();
                    if state.stopping.load(std::sync::atomic::Ordering::SeqCst) {
                        break;
                    }
                    if node::health_ok(state.port) {
                        failures = 0;
                    } else {
                        failures = failures.saturating_add(1);
                        if failures >= 3 {
                            let _ = node::recover_if_unhealthy(state.inner());
                            failures = 0;
                        }
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            // Tray app: closing the window hides it; the node keeps running.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|app_handle, event| {
        match event {
            RunEvent::Resumed => {
                let handle = app_handle.clone();
                std::thread::spawn(move || {
                    let _ = node::recover_if_unhealthy(handle.state::<NodeState>().inner());
                });
            }
            RunEvent::ExitRequested { .. } => {
                node::stop(app_handle.state::<NodeState>().inner());
            }
            _ => {}
        }
    });
}
