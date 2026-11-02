//! Installer-provided local plugin runtimes. Web manifests cannot supply commands.
use std::{collections::{BTreeMap, BTreeSet}, fs, io::{BufRead, BufReader, Read, Write}, net::TcpStream,
    path::{Component, Path, PathBuf}, process::{Child, Command, Stdio},
    sync::{Mutex, atomic::{AtomicBool, Ordering}}, time::{Duration, Instant}};
use serde::Deserialize;
use tauri::Manager;

#[derive(Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Service {
    id: String,
    plugins: Vec<String>,
    program: PathBuf,
    port: u16,
    health_path: String,
    #[serde(default)]
    environment: BTreeMap<String, String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config { version: u32, services: Vec<Service> }

struct OwnedService {
    child: Child,
    spec: Service,
    unhealthy_since: Option<Instant>,
    #[cfg(windows)]
    job: usize,
}

impl Drop for OwnedService {
    fn drop(&mut self) {
        // The job owns the complete tree, including children on abnormal desktop exit.
        #[cfg(windows)]
        unsafe { let _ = windows::Win32::Foundation::CloseHandle(windows::Win32::Foundation::HANDLE(self.job as _)); }
        #[cfg(unix)]
        unsafe { libc::kill(-(self.child.id() as i32), libc::SIGTERM); }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[derive(Default)]
pub struct PluginServices {
    children: Mutex<BTreeMap<String, OwnedService>>,
    stopping: AtomicBool,
}

fn safe_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

fn parse(bytes: &[u8]) -> Result<Vec<Service>, String> {
    let config: Config = serde_json::from_slice(bytes).map_err(|_| "plugin_service_config_invalid")?;
    if config.version != 1 || config.services.len() > 32 { return Err("plugin_service_config_invalid".into()); }
    let mut ids = BTreeSet::new();
    let mut plugins = BTreeSet::new();
    let mut ports = BTreeSet::new();
    for s in &config.services {
        if !safe_id(&s.id) || !ids.insert(s.id.clone()) || s.plugins.is_empty() || s.plugins.len() > 100 ||
            s.plugins.iter().any(|id| !safe_id(id) || !plugins.insert(id.clone())) || s.port == 0 || !ports.insert(s.port) ||
            s.program.as_os_str().is_empty() || !s.program.components().all(|c| matches!(c, Component::Normal(_))) ||
            !s.health_path.starts_with('/') || s.health_path.len() > 2048 ||
            s.health_path.bytes().any(|b| b <= 32 || b >= 127) ||
            s.environment.len() > 32 || s.environment.iter().any(|(k,v)| k.is_empty() || k.contains(['=', '\0']) || v.contains('\0')) {
            return Err("plugin_service_config_invalid".into());
        }
    }
    Ok(config.services)
}

fn installed() -> Result<(PathBuf, Vec<Service>), String> {
    let exe = std::env::current_exe().map_err(|_| "plugin_service_directory_unavailable")?;
    let root = exe.parent().ok_or("plugin_service_directory_unavailable")?.join("plugin-services");
    let file = root.join("services.json");
    if !file.exists() { return Ok((root, vec![])); }
    if fs::metadata(&file).map_err(|_| "plugin_service_config_unreadable")?.len() > 65536 {
        return Err("plugin_service_config_invalid".into());
    }
    let services = parse(&fs::read(file).map_err(|_| "plugin_service_config_unreadable")?)?;
    Ok((root, services))
}

fn plugin_matches(plugin: &serde_json::Value, s: &Service) -> bool {
    if plugin["enabled"].as_bool() != Some(true) ||
        !s.plugins.iter().any(|id| Some(id.as_str()) == plugin["id"].as_str()) { return false; }
    plugin["web_app"]["url"].as_str().and_then(|url| tauri::Url::parse(url).ok()).is_some_and(|url|
        url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(s.port))
}

fn plugins(port: u16) -> Result<Vec<serde_json::Value>, String> {
    crate::node::request_bounded(port, "GET", "/api/local/agent/extensions", "", Duration::from_secs(5), 16 * 1024 * 1024)?["plugins"]
        .as_array().cloned().ok_or("plugin_overview_invalid".into())
}

fn listening(port: u16) -> bool {
    TcpStream::connect_timeout(&([127,0,0,1], port).into(), Duration::from_millis(200)).is_ok()
}

fn healthy(s: &Service) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&([127,0,0,1], s.port).into(), Duration::from_millis(200)) else { return false; };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    if stream.write_all(format!("GET {} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n", s.health_path, s.port).as_bytes()).is_err() { return false; }
    // TCP may split even the status line across reads.
    let mut line = String::new();
    BufReader::new(stream.take(128)).read_line(&mut line).is_ok()
        && line.ends_with('\n') && line.split_whitespace().nth(1) == Some("200")
}

fn spawn(root: &Path, service: &Service) -> Result<OwnedService, String> {
    let base = root.canonicalize().map_err(|_| "plugin_service_files_missing")?;
    let program = base.join(&service.program).canonicalize().map_err(|_| "plugin_service_files_missing")?;
    if !program.starts_with(&base) || !program.is_file() { return Err("plugin_service_program_invalid".into()); }
    let log = fs::OpenOptions::new().create(true).append(true).open(crate::node::log_dir().join(format!("plugin-{}.log", service.id)))
        .map_err(|_| "plugin_service_log_unavailable")?;
    let log_err = log.try_clone().map_err(|_| "plugin_service_log_unavailable")?;
    let mut command = Command::new(&program);
    command.current_dir(program.parent().unwrap()).envs(&service.environment)
        .stdin(Stdio::null()).stdout(log).stderr(log_err);
    #[cfg(windows)]
    { use std::os::windows::process::CommandExt; command.creation_flags(0x08000000); }
    #[cfg(unix)]
    { use std::os::unix::process::CommandExt; command.process_group(0); }
    let mut child = command.spawn().map_err(|_| "plugin_service_start_failed")?;
    #[cfg(windows)]
    let job = match own_tree(child.id()) {
        Ok(job) => job,
        Err(error) => { let _ = child.kill(); let _ = child.wait(); return Err(error); }
    };
    Ok(OwnedService { child, spec: service.clone(), unhealthy_since: None, #[cfg(windows)] job })
}

#[cfg(windows)]
fn own_tree(pid: u32) -> Result<usize, String> {
    use windows::Win32::{Foundation::CloseHandle, System::{JobObjects::*, Threading::*}};
    unsafe {
        let job = CreateJobObjectW(None, None).map_err(|_| "plugin_service_job_failed")?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let result = (|| {
            SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info as *const _ as _, std::mem::size_of_val(&info) as u32)?;
            let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, false, pid)?;
            let assigned = AssignProcessToJobObject(job, process);
            let _ = CloseHandle(process);
            assigned
        })();
        if result.is_err() { let _ = CloseHandle(job); return Err("plugin_service_job_failed".into()); }
        Ok(job.0 as usize)
    }
}

impl PluginServices {
    fn ensure(&self, root: &Path, s: &Service) -> Result<(), String> {
        let mut children = self.children.lock().unwrap();
        if self.stopping.load(Ordering::SeqCst) { return Err("plugin_service_stopping".into()); }
        if let Some(owned) = children.get_mut(&s.id) {
            if owned.spec == *s && matches!(owned.child.try_wait(), Ok(None)) {
                let ready = healthy(s);
                if ready { owned.unhealthy_since = None; return Ok(()); }
                // A slow request is not a crash. Recover only our own process,
                // after sustained failure, never an unrelated port owner.
                if !recovery_due(&mut owned.unhealthy_since, Instant::now()) {
                    return Err("plugin_service_unhealthy".into());
                }
                log::warn!("plugin service {}: restarting unresponsive owned process", s.id);
            }
            children.remove(&s.id);
        }
        // Never adopt/terminate a foreign listener just because it uses the desired port.
        if listening(s.port) { return Err("plugin_service_port_in_use".into()); }
        let mut owned = spawn(root, s)?;
        let deadline = Instant::now() + Duration::from_secs(30);
        while Instant::now() < deadline && !self.stopping.load(Ordering::SeqCst) {
            if !matches!(owned.child.try_wait(), Ok(None)) { return Err("plugin_service_exited".into()); }
            if healthy(s) { children.insert(s.id.clone(), owned); return Ok(()); }
            std::thread::sleep(Duration::from_millis(200));
        }
        Err("plugin_service_start_timeout".into())
    }

    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        self.children.lock().unwrap().clear();
    }
}

#[tauri::command]
pub async fn ensure_plugin_service(app: tauri::AppHandle, plugin_id: String) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let (root, services) = installed()?;
        let Some(service) = services.iter().find(|s| s.plugins.contains(&plugin_id)) else { return Ok(false); };
        let live = plugins(app.state::<crate::node::NodeState>().port)?;
        if !live.iter().any(|p| p["id"].as_str() == Some(&plugin_id) && plugin_matches(p, service)) {
            return Err("plugin_service_disabled".into());
        }
        app.state::<PluginServices>().ensure(&root, service).map_err(|error| {
            log::warn!("plugin service {}: {error}", service.id);
            error
        })?;
        Ok(true)
    }).await.map_err(|_| "plugin_service_start_failed")?
}

fn recovery_due(since: &mut Option<Instant>, now: Instant) -> bool {
    now.duration_since(*since.get_or_insert(now)) >= Duration::from_secs(90)
}

pub fn watch(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut last_attempt = BTreeMap::<String, Instant>::new();
        loop {
            let state = app.state::<PluginServices>();
            if state.stopping.load(Ordering::SeqCst) { break; }
            // Installers atomically replace the registry. Do not keep launching
            // the old runtime after an update or miss a newly installed service.
            if let Ok((root, services)) = installed() {
                state.children.lock().unwrap().retain(|id, _| services.iter().any(|s| &s.id == id));
                last_attempt.retain(|id, _| services.iter().any(|s| &s.id == id));
                if let Ok(live) = plugins(app.state::<crate::node::NodeState>().port) {
                    for service in &services {
                        if live.iter().any(|p| plugin_matches(p, service)) {
                            if last_attempt.get(&service.id).is_none_or(|last| last.elapsed() >= Duration::from_secs(30)) {
                                last_attempt.insert(service.id.clone(), Instant::now());
                                if let Err(error) = state.ensure(&root, service) { log::warn!("plugin service {}: {error}", service.id); }
                            }
                        } else { state.children.lock().unwrap().remove(&service.id); last_attempt.remove(&service.id); }
                    }
                }
            }
            for _ in 0..50 { if state.stopping.load(Ordering::SeqCst) { return; } std::thread::sleep(Duration::from_millis(100)); }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn occupied_foreign_port_is_never_adopted_or_stopped() {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let mut service = parse(&serde_json::to_vec(&config()).unwrap()).unwrap().remove(0);
        service.port = listener.local_addr().unwrap().port();
        let state = PluginServices::default();
        assert_eq!(state.ensure(Path::new("missing"), &service), Err("plugin_service_port_in_use".into()));
        assert!(state.children.lock().unwrap().is_empty());
        assert!(listening(service.port));
    }
    #[test]
    fn health_accepts_fragmented_status_line() {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let mut service = parse(&serde_json::to_vec(&config()).unwrap()).unwrap().remove(0);
        service.port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = [0; 4096];
            socket.read(&mut request).unwrap();
            socket.write_all(b"HTTP/1.1 2").unwrap();
            std::thread::sleep(Duration::from_millis(30));
            socket.write_all(b"00 OK\r\nContent-Length: 0\r\n\r\n").unwrap();
        });
        assert!(healthy(&service));
        server.join().unwrap();
    }
    #[test]
    fn transient_failures_do_not_restart_but_sustained_failure_does() {
        let now = Instant::now();
        let mut since = None;
        assert!(!recovery_due(&mut since, now));
        assert!(!recovery_due(&mut since, now + Duration::from_secs(30)));
        assert!(recovery_due(&mut since, now + Duration::from_secs(90)));
        since = None; // a successful probe resets the grace period
        assert!(!recovery_due(&mut since, now + Duration::from_secs(100)));
    }
    fn config() -> serde_json::Value { serde_json::json!({"version":1,"services":[{"id":"reports","plugins":["reports","charts"],"program":"reports/service.exe","port":18140,"health_path":"/health"}]}) }
    #[test]
    fn validates_installer_config_and_confined_entrypoints() {
        assert!(parse(&serde_json::to_vec(&config()).unwrap()).is_ok());
        for path in ["../outside.exe", "/bin/sh", ""] {
            let mut c = config(); c["services"][0]["program"] = path.into();
            assert!(parse(&serde_json::to_vec(&c).unwrap()).is_err());
        }
        let mut c = config(); c["services"][0]["health_path"] = "/health\r\nInjected: true".into();
        assert!(parse(&serde_json::to_vec(&c).unwrap()).is_err());
        let mut c = config(); c["services"][0]["command"] = "arbitrary command".into();
        assert!(parse(&serde_json::to_vec(&c).unwrap()).is_err());
        let mut c = config(); let duplicate = c["services"][0].clone(); c["services"].as_array_mut().unwrap().push(duplicate);
        assert!(parse(&serde_json::to_vec(&c).unwrap()).is_err());
    }
    #[test]
    fn requires_enabled_installed_plugin_and_exact_loopback_port() {
        let service = parse(&serde_json::to_vec(&config()).unwrap()).unwrap().remove(0);
        let mut plugin = serde_json::json!({"id":"reports","enabled":true,"web_app":{"url":"http://127.0.0.1:18140/app"}});
        assert!(plugin_matches(&plugin, &service));
        plugin["enabled"] = false.into(); assert!(!plugin_matches(&plugin, &service));
        plugin["enabled"] = true.into(); plugin["web_app"]["url"] = "https://external.example:18140/app".into(); assert!(!plugin_matches(&plugin, &service));
        plugin["web_app"]["url"] = "http://127.0.0.1:18141/app".into(); assert!(!plugin_matches(&plugin, &service));
    }
}
