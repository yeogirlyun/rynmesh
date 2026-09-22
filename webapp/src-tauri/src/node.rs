//! Cross-platform lifecycle for the self-contained local Ryn node.
//! Packaged builds use the bundled sidecar; development can use an override.

use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

fn hidden_command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    command
}

pub struct NodeState {
    pub child: Mutex<Option<Child>>,
    pub port: u16,
    pub stopping: AtomicBool,
    pub lifecycle: Mutex<()>,
}

#[cfg(not(windows))]
fn capture(program: &str, args: &[&str]) -> Option<String> {
    let out = hidden_command(program).args(args).output().ok()?;
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
    #[cfg(windows)]
    return std::env::var("COMPUTERNAME").unwrap_or_else(|_| "My PC".to_string());
    #[cfg(not(windows))]
    capture("/usr/sbin/scutil", &["--get", "ComputerName"])
        .or_else(|| capture("/bin/hostname", &["-s"]))
        .unwrap_or_else(|| "ryn-node".to_string())
}

fn lan_ip() -> String {
    // Connecting UDP chooses a local interface without sending any data.
    if let Ok(socket) = std::net::UdpSocket::bind("0.0.0.0:0") {
        if socket.connect("1.1.1.1:80").is_ok() {
            if let Ok(addr) = socket.local_addr() {
                return addr.ip().to_string();
            }
        }
    }
    #[cfg(target_os = "macos")]
    {
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
    }
    "127.0.0.1".to_string()
}

pub fn log_dir() -> PathBuf {
    #[cfg(windows)]
    let dir = PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_else(|| std::env::temp_dir().into_os_string())).join("Ryn/logs");
    #[cfg(not(windows))]
    let dir = {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join("Library/Logs/Rynmesh")
    };
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
        // Recompute the advertised LAN address after a network change. Explicit
        // operator endpoints remain fixed (public forwarding / overlay setups).
        ("RYNMESH_AUTO_PEER_ENDPOINT".into(), env_or("RYNMESH_AUTO_PEER_ENDPOINT", || {
            if std::env::var("RYNMESH_PEER_ENDPOINT").unwrap_or_default().is_empty()
                && std::env::var("RYNMESH_PEER_PUBLIC_HOST").unwrap_or_default().is_empty() {
                "1".to_string()
            } else { "0".to_string() }
        })),
        ("RYNMESH_AUTO_REGISTER".into(), env_or("RYNMESH_AUTO_REGISTER", || "1".to_string())),
        ("RYNMESH_REGISTRY_URL".into(), registry.clone()),
        ("RYNMESH_RELAY_URL".into(), env_or("RYNMESH_RELAY_URL", || registry.clone())),
    ]
}

fn which(bin: &str) -> bool {
    hidden_command(if cfg!(windows) { "where.exe" } else { "/usr/bin/which" })
        .arg(bin)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn open_log() -> std::io::Result<File> {
    let path = log_dir().join("ryn-node.log");
    if std::fs::metadata(&path).map(|m| m.len() > 5 * 1024 * 1024).unwrap_or(false) {
        let previous = path.with_extension("previous.log");
        let _ = std::fs::remove_file(&previous);
        let _ = std::fs::rename(&path, previous);
    }
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
            let p = dir.join(if cfg!(windows) { "rynmesh-peer.exe" } else { "rynmesh-peer" });
            if p.is_file() {
                return Some(p);
            }
        }
    }
    let bin_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("binaries");
    let path = bin_dir.join(format!("rynmesh-peer-{}{}", env!("RYN_TARGET"), if cfg!(windows) { ".exe" } else { "" }));
    path.is_file().then_some(path)
}

fn build_command(port: u16) -> Command {
    let mut cmd = if let Ok(custom) = std::env::var("RYNMESH_PEER_CMD") {
        let mut c = hidden_command(if cfg!(windows) { "cmd.exe" } else { "/bin/sh" });
        c.arg(if cfg!(windows) { "/C" } else { "-c" }).arg(custom);
        c
    } else if let Some(sidecar) = sidecar_path() {
        hidden_command(sidecar)
    } else if which("rynmesh-peer") {
        hidden_command("rynmesh-peer")
    } else {
        let py = env_or("RYNMESH_PYTHON", || if cfg!(windows) { "python" } else { "python3" }.to_string());
        let mut c = hidden_command(py);
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
    let _operation = state.lifecycle.lock().unwrap();
    start_inner(state)
}

fn start_inner(state: &NodeState) -> std::io::Result<()> {
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
    let mut cmd = build_command(state.port);
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
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        #[cfg(windows)]
        {
            // PyInstaller onefile owns a worker process: stop the owned tree.
            let _ = hidden_command("taskkill.exe")
                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                .output();
        }
        #[cfg(unix)]
        {
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
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

pub fn stop(state: &NodeState) {
    state.stopping.store(true, Ordering::SeqCst);
    let _operation = state.lifecycle.lock().unwrap();
    stop_child(state);
}

pub fn restart(state: &NodeState) -> std::io::Result<()> {
    let _operation = state.lifecycle.lock().unwrap();
    if state.stopping.load(Ordering::SeqCst) { return Ok(()); }
    if state.child.lock().unwrap().is_none() && health_ok(state.port) {
        return Err(std::io::Error::other("This node is managed by another process."));
    }
    stop_child(state);
    start_inner(state)
}

/// Control requests stay on loopback and honor an operator-provided local token.
pub fn request(port: u16, method: &str, path: &str, body: &str) -> Result<serde_json::Value, String> {
    let addr = format!("127.0.0.1:{port}").parse().map_err(|_| "invalid_port")?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(600)).map_err(|_| "node_unavailable")?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let token = std::env::var("RYNMESH_LOCAL_TOKEN").unwrap_or_default();
    if token.contains(['\r', '\n']) { return Err("invalid_local_token".into()); }
    let auth = if token.is_empty() { String::new() } else { format!("X-Ryn-Local-Token: {token}\r\n") };
    let request = format!("{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n{auth}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
    stream.write_all(request.as_bytes()).map_err(|_| "node_unavailable")?;
    let mut buf = String::new();
    stream.take(65536).read_to_string(&mut buf).map_err(|_| "node_unavailable")?;
    let (header, payload) = buf.split_once("\r\n\r\n").ok_or("invalid_response")?;
    if !header.lines().next().unwrap_or("").contains(" 200 ") { return Err("node_request_rejected".into()); }
    serde_json::from_str(payload).map_err(|_| "invalid_response".into())
}
pub fn health_ok(port: u16) -> bool {
    request(port, "GET", "/api/local/desktop/status", "")
        .map(|v| v["desktop_managed"] == true).unwrap_or(false)
}

/// Bound boot waiting by wall time, including socket timeouts.
pub fn wait_healthy(port: u16) -> bool {
    let deadline = std::time::Instant::now() + Duration::from_secs(20);
    while std::time::Instant::now() < deadline {
        if health_ok(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}
