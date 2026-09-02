//! Ryn node lifecycle: spawn the local Rynmesh peer daemon as a managed child,
//! replicate the env logic from scripts/launch_ryn_node_webapp.zsh, poll
//! /health, and stop it gracefully (SIGTERM -> wait -> SIGKILL).
//!
//! Phase 3 runs against the system `rynmesh-peer` (or `python3 -c ...`).
//! The PyInstaller-bundled sidecar (Phase 2) will only change `build_command`.

use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpStream, UdpSocket};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

pub struct NodeState {
    pub child: Mutex<Option<Child>>,
    pub port: u16,
    pub stopping: AtomicBool,
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

#[cfg(target_os = "macos")]
fn machine_name() -> String {
    capture("/usr/sbin/scutil", &["--get", "ComputerName"])
        .or_else(|| capture("hostname", &["-s"]))
        .unwrap_or_else(|| "ryn-node".to_string())
}

#[cfg(not(target_os = "macos"))]
fn machine_name() -> String {
    capture("hostname", &["-s"]).unwrap_or_else(|| "ryn-node".to_string())
}

fn lan_ip() -> String {
    // UDP connect does not send a packet, but lets the OS select the address
    // it would use for an external route. This avoids macOS/Linux command and
    // output-format differences.
    UdpSocket::bind("0.0.0.0:0")
        .and_then(|socket| {
            socket.connect("192.0.2.1:80")?;
            socket.local_addr()
        })
        .map(|addr| addr.ip().to_string())
        .unwrap_or_else(|_| "127.0.0.1".to_string())
}

#[cfg(target_os = "macos")]
pub fn log_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let dir = PathBuf::from(home).join("Library/Logs/Rynmesh");
    let _ = create_dir_all(&dir);
    dir
}

#[cfg(target_os = "linux")]
fn linux_log_dir(home: Option<&str>, xdg_state_home: Option<&str>) -> PathBuf {
    if let Some(state_home) = xdg_state_home
        .filter(|value| !value.is_empty())
        .filter(|value| Path::new(value).is_absolute())
    {
        return PathBuf::from(state_home).join("rynmesh");
    }
    PathBuf::from(home.filter(|value| !value.is_empty()).unwrap_or("."))
        .join(".local/state/rynmesh")
}

#[cfg(target_os = "linux")]
pub fn log_dir() -> PathBuf {
    let home = std::env::var("HOME").ok();
    let xdg = std::env::var("XDG_STATE_HOME").ok();
    let dir = linux_log_dir(home.as_deref(), xdg.as_deref());
    let _ = create_dir_all(&dir);
    dir
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn log_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let dir = PathBuf::from(home).join(".rynmesh/logs");
    let _ = create_dir_all(&dir);
    dir
}

pub fn open_log_dir() -> std::io::Result<()> {
    let dir = log_dir();
    #[cfg(target_os = "macos")]
    let mut command = Command::new("/usr/bin/open");
    #[cfg(target_os = "linux")]
    let mut command = Command::new("xdg-open");
    #[cfg(target_os = "windows")]
    let mut command = Command::new("explorer");
    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    let mut command = Command::new("xdg-open");
    command.arg(dir).spawn().map(|_| ())
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
    if Path::new(bin).components().count() > 1 {
        return executable_file(Path::new(bin));
    }
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths)
                .map(|dir| dir.join(bin))
                .any(|candidate| executable_file(&candidate))
        })
        .unwrap_or(false)
}

#[cfg(unix)]
fn executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn executable_file(path: &Path) -> bool {
    path.is_file()
}

fn open_log() -> std::io::Result<File> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir().join("ryn-node.log"))
}

/// Resolution order: RYNMESH_PEER_CMD override -> exact bundled sidecar ->
/// `rynmesh-peer` on PATH -> `$RYNMESH_PYTHON|python3 -c ...` with
/// PYTHONPATH=$RYNMESH_REPO_DIR. The bundled self-contained daemon lives next
/// to the app executable when packaged, or at
/// src-tauri/binaries/rynmesh-peer-<triple> in development.
fn target_triple() -> Option<&'static str> {
    #[cfg(all(target_arch = "x86_64", target_os = "linux"))]
    return Some("x86_64-unknown-linux-gnu");
    #[cfg(all(target_arch = "aarch64", target_os = "linux"))]
    return Some("aarch64-unknown-linux-gnu");
    #[cfg(all(target_arch = "x86_64", target_os = "macos"))]
    return Some("x86_64-apple-darwin");
    #[cfg(all(target_arch = "aarch64", target_os = "macos"))]
    return Some("aarch64-apple-darwin");
    #[allow(unreachable_code)]
    None
}

fn resolve_sidecar(app_exe: Option<&Path>, bin_dir: &Path, triple: Option<&str>) -> Option<PathBuf> {
    if let Some(dir) = app_exe.and_then(Path::parent) {
        let installed = dir.join("rynmesh-peer");
        if executable_file(&installed) {
            return Some(installed);
        }
    }
    let triple = triple?;
    let development = bin_dir.join(format!("rynmesh-peer-{triple}"));
    if executable_file(&development) {
        return Some(development);
    }
    None
}

fn sidecar_path() -> Option<PathBuf> {
    let app_exe = std::env::current_exe().ok();
    let bin_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("binaries");
    resolve_sidecar(app_exe.as_deref(), &bin_dir, target_triple())
}

fn build_command(port: u16) -> std::io::Result<Command> {
    let mut cmd = if let Ok(custom) = std::env::var("RYNMESH_PEER_CMD") {
        #[cfg(unix)]
        let mut c = Command::new("/bin/sh");
        #[cfg(windows)]
        let mut c = Command::new("cmd");
        #[cfg(unix)]
        c.arg("-c").arg(custom);
        #[cfg(windows)]
        c.arg("/C").arg(custom);
        c
    } else if let Some(sidecar) = sidecar_path() {
        Command::new(sidecar)
    } else if !cfg!(debug_assertions) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "the packaged rynmesh-peer sidecar is missing or not executable",
        ));
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
    Ok(cmd)
}

pub fn start(state: &NodeState) -> std::io::Result<()> {
    if state.stopping.load(Ordering::SeqCst) {
        return Ok(());
    }
    let mut guard = state.child.lock().unwrap();
    if let Some(child) = guard.as_mut() {
        match child.try_wait()? {
            None => return Ok(()),
            Some(status) => {
                log::warn!("managed Ryn node exited with {status}; starting a replacement");
                *guard = None;
            }
        }
    }
    // A correctly configured login agent may already own the node. Reuse it
    // instead of spawning a child that can only fail with address-in-use.
    if health_ok(state.port) {
        return Ok(());
    }
    let log = open_log()?;
    let log_err = log.try_clone()?;
    let mut cmd = build_command(state.port)?;
    cmd.stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));
    let child = cmd.spawn()?;
    *guard = Some(child);
    Ok(())
}

fn stop_child(state: &NodeState) {
    let mut guard = state.child.lock().unwrap();
    if let Some(mut child) = guard.take() {
        #[cfg(unix)]
        {
            let pid = child.id() as i32;
            unsafe {
                let _ = libc::kill(pid, libc::SIGTERM);
            }
        }
        #[cfg(not(unix))]
        {
            let _ = child.kill();
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn scratch(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "rynmesh-{name}-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn executable(path: &Path) {
        File::create(path).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        }
    }

    #[test]
    fn sidecar_resolution_requires_exact_target_name() {
        let root = scratch("sidecar-target");
        let bin = root.join("binaries");
        fs::create_dir_all(&bin).unwrap();
        executable(&bin.join("rynmesh-peer-wrong-target"));
        assert_eq!(resolve_sidecar(None, &bin, Some("expected-target")), None);
        let expected = bin.join("rynmesh-peer-expected-target");
        executable(&expected);
        assert_eq!(
            resolve_sidecar(None, &bin, Some("expected-target")),
            Some(expected)
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn installed_sidecar_takes_precedence() {
        let root = scratch("sidecar-installed");
        let app = root.join("Ryn");
        executable(&app);
        let installed = root.join("rynmesh-peer");
        executable(&installed);
        let bin = root.join("binaries");
        fs::create_dir_all(&bin).unwrap();
        let development = bin.join("rynmesh-peer-expected-target");
        executable(&development);
        assert_eq!(
            resolve_sidecar(Some(&app), &bin, Some("expected-target")),
            Some(installed)
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_logs_follow_xdg_state_directory() {
        assert_eq!(
            linux_log_dir(Some("/home/ryn"), Some("/run/user/1000/state")),
            PathBuf::from("/run/user/1000/state/rynmesh")
        );
        assert_eq!(
            linux_log_dir(Some("/home/ryn"), None),
            PathBuf::from("/home/ryn/.local/state/rynmesh")
        );
        assert_eq!(
            linux_log_dir(Some("/home/ryn"), Some("relative/state")),
            PathBuf::from("/home/ryn/.local/state/rynmesh")
        );
    }
}

pub fn stop(state: &NodeState) {
    state.stopping.store(true, Ordering::SeqCst);
    stop_child(state);
}

pub fn restart(state: &NodeState) {
    state.stopping.store(true, Ordering::SeqCst);
    stop_child(state);
    state.stopping.store(false, Ordering::SeqCst);
    let _ = start(state);
}

pub fn health_ok(port: u16) -> bool {
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

pub fn recover_if_unhealthy(state: &NodeState) -> bool {
    if state.stopping.load(Ordering::SeqCst) || health_ok(state.port) {
        return false;
    }
    log::warn!("managed Ryn node is unhealthy; restarting it");
    restart(state);
    wait_healthy(state.port)
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
