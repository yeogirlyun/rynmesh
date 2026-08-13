//! Ryn node lifecycle: spawn the local Rynmesh peer daemon as a managed child,
//! replicate the env logic from scripts/launch_ryn_node_webapp.zsh, poll
//! /health, and stop it gracefully (SIGTERM -> wait -> SIGKILL).
//!
//! Phase 3 runs against the system `rynmesh-peer` (or `python3 -c ...`).
//! The PyInstaller-bundled sidecar (Phase 2) will only change `build_command`.

use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

pub struct NodeState {
    pub child: Mutex<Option<Child>>,
    pub port: u16,
}

fn capture(program: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(program).args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

fn machine_name() -> String {
    capture("/usr/sbin/scutil", &["--get", "ComputerName"])
        .or_else(|| capture("/bin/hostname", &["-s"]))
        .unwrap_or_else(|| "ryn-node".to_string())
}

fn lan_ip() -> String {
    let iface = capture("/sbin/route", &["-n", "get", "default"]).and_then(|s| {
        s.lines().find_map(|l| {
            l.trim()
                .strip_prefix("interface:")
                .map(|v| v.trim().to_string())
        })
    });
    if let Some(iface) = iface {
        if let Some(ip) = capture("/usr/sbin/ipconfig", &["getifaddr", &iface]) {
            return ip;
        }
    }
    "127.0.0.1".to_string()
}

pub fn log_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let dir = PathBuf::from(home).join("Library/Logs/Rynmesh");
    let _ = create_dir_all(&dir);
    dir
}

fn env_or(key: &str, default: impl FnOnce() -> String) -> String {
    match std::env::var(key) {
        Ok(v) if !v.is_empty() => v,
        _ => default(),
    }
}

/// RYNMESH_* defaults mirror launch_ryn_node_webapp.zsh; any value already in
/// the process environment wins (operator override).
fn node_env(port: u16) -> Vec<(String, String)> {
    let mname = machine_name();
    let ip = lan_ip();
    let registry = env_or("RYNMESH_REGISTRY_URL", || {
        "https://registry.rynmesh.ai".to_string()
    });
    vec![
        ("RYNMESH_NODE_NAME".into(), env_or("RYNMESH_NODE_NAME", || mname.clone())),
        ("RYNMESH_MACHINE_NAME".into(), env_or("RYNMESH_MACHINE_NAME", || mname.clone())),
        ("RYNMESH_MACHINE_IP".into(), env_or("RYNMESH_MACHINE_IP", || ip.clone())),
        ("RYNMESH_DESKTOP_MODE".into(), "1".to_string()),
        ("RYNMESH_NETWORK_ID".into(), env_or("RYNMESH_NETWORK_ID", || "rynmesh-main".to_string())),
        ("RYNMESH_PEER_HOST".into(), env_or("RYNMESH_PEER_HOST", || "0.0.0.0".to_string())),
        ("RYNMESH_PEER_PORT".into(), env_or("RYNMESH_PEER_PORT", || port.to_string())),
        ("RYNMESH_PEER_PUBLIC_HOST".into(), env_or("RYNMESH_PEER_PUBLIC_HOST", || ip.clone())),
        ("RYNMESH_PEER_ENDPOINT".into(), env_or("RYNMESH_PEER_ENDPOINT", || format!("http://{ip}:{port}"))),
        ("RYNMESH_AUTO_REGISTER".into(), env_or("RYNMESH_AUTO_REGISTER", || "1".to_string())),
        ("RYNMESH_REGISTRY_URL".into(), registry.clone()),
        ("RYNMESH_RELAY_URL".into(), env_or("RYNMESH_RELAY_URL", || registry.clone())),
    ]
}

fn which(bin: &str) -> bool {
    Command::new("/usr/bin/which")
        .arg(bin)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn open_log() -> std::io::Result<File> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir().join("ryn-node.log"))
}

/// Resolution order: RYNMESH_PEER_CMD override -> `rynmesh-peer` on PATH ->
/// `$RYNMESH_PYTHON|python3 -c ...` with PYTHONPATH=$RYNMESH_REPO_DIR.
/// The bundled self-contained daemon: next to the app executable when
/// packaged, or src-tauri/binaries/rynmesh-peer-<triple> in dev.
fn sidecar_path() -> Option<std::path::PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("rynmesh-peer");
            if p.is_file() {
                return Some(p);
            }
        }
    }
    let bin_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("binaries");
    if let Ok(rd) = std::fs::read_dir(&bin_dir) {
        for e in rd.flatten() {
            if e.file_name().to_string_lossy().starts_with("rynmesh-peer") {
                return Some(e.path());
            }
        }
    }
    None
}

fn build_command(port: u16) -> Command {
    let mut cmd = if let Ok(custom) = std::env::var("RYNMESH_PEER_CMD") {
        let mut c = Command::new("/bin/sh");
        c.arg("-c").arg(custom);
        c
    } else if let Some(sidecar) = sidecar_path() {
        Command::new(sidecar)
    } else if which("rynmesh-peer") {
        Command::new("rynmesh-peer")
    } else {
        let py = env_or("RYNMESH_PYTHON", || "python3".to_string());
        let mut c = Command::new(py);
        c.arg("-c")
            .arg("from rynmesh.peer_http import main; raise SystemExit(main())");
        if let Ok(repo) = std::env::var("RYNMESH_REPO_DIR") {
            c.env("PYTHONPATH", &repo);
            c.current_dir(&repo);
        }
        c
    };
    for (k, v) in node_env(port) {
        cmd.env(k, v);
    }
    cmd
}

pub fn start(state: &NodeState) -> std::io::Result<()> {
    let mut guard = state.child.lock().unwrap();
    if guard.is_some() {
        return Ok(());
    }
    // A correctly configured login agent may already own the node. Reuse it
    // instead of spawning a child that can only fail with address-in-use.
    if health_ok(state.port) {
        return Ok(());
    }
    let log = open_log()?;
    let log_err = log.try_clone()?;
    let mut cmd = build_command(state.port);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));
    let child = cmd.spawn()?;
    *guard = Some(child);
    Ok(())
}

pub fn stop(state: &NodeState) {
    let mut guard = state.child.lock().unwrap();
    if let Some(mut child) = guard.take() {
        let pid = child.id() as i32;
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
        for _ in 0..30 {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                Err(_) => break,
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

pub fn restart(state: &NodeState) {
    stop(state);
    let _ = start(state);
}

fn health_ok(port: u16) -> bool {
    let addr = match format!("127.0.0.1:{port}").parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(600)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(600)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(600)));
    if stream
        .write_all(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut buf = Vec::with_capacity(2048);
    match stream.read_to_end(&mut buf) {
        Ok(n) if n > 0 => {
            let response = String::from_utf8_lossy(&buf);
            response.contains(" 200") && response.contains("\"desktop_managed\":true")
        }
        _ => false,
    }
}

/// ~12s budget (80 x 150ms), matching the launch script's health wait.
pub fn wait_healthy(port: u16) -> bool {
    for _ in 0..80 {
        if health_ok(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}
