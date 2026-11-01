//! A portable deployment keeps its node and desktop profile beside the executable.
//! Start menu / taskbar launches do not inherit the launcher script's environment.

use std::{collections::BTreeMap, path::{Path, PathBuf}};
use serde::Deserialize;
use tauri::Manager;

const FILE: &str = "ryn-desktop.json";
const PATHS: &[&str] = &["RYNMESH_HOME", "WEBVIEW2_USER_DATA_FOLDER", "RYNMESH_WEBUI_DIR",
    "LOCALAPPDATA", "RYNMESH_PYTHON", "RYN_DESKTOP_CONFIG_DIR", "RYN_DESKTOP_PROFILE_DIR"];
const FLAGS: &[&str] = &["RYNMESH_AUTO_REGISTER", "RYN_SYSTEM_APPS"];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Deployment {
    version: u32,
    environment: BTreeMap<String, String>,
}

fn parse(bytes: &[u8]) -> Result<BTreeMap<String, String>, String> {
    let config: Deployment = serde_json::from_slice(bytes).map_err(|e| format!("invalid {FILE}: {e}"))?;
    if config.version != 1 { return Err(format!("unsupported {FILE} version")); }
    for required in ["RYNMESH_HOME", "WEBVIEW2_USER_DATA_FOLDER", "RYNMESH_PEER_PORT", "RYN_DESKTOP_CONFIG_DIR"] {
        if !config.environment.contains_key(required) { return Err(format!("{FILE} missing {required}")); }
    }
    for (key, value) in &config.environment {
        if value.is_empty() || value.contains('\0') { return Err(format!("invalid {FILE} value for {key}")); }
        if PATHS.contains(&key.as_str()) {
            if !Path::new(value).is_absolute() { return Err(format!("{FILE} requires an absolute {key}")); }
        } else if FLAGS.contains(&key.as_str()) {
            if value != "0" && value != "1" { return Err(format!("invalid {FILE} flag {key}")); }
        } else if key == "RYNMESH_PEER_PORT" {
            if !value.parse::<u16>().is_ok_and(|port| port > 0) { return Err(format!("invalid {FILE} node port")); }
        } else { return Err(format!("unsupported {FILE} setting {key}")); }
    }
    Ok(config.environment)
}

fn read(path: &Path) -> Result<Option<BTreeMap<String, String>>, String> {
    let metadata = match std::fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("cannot read {FILE}: {error}")),
    };
    if !metadata.is_file() || metadata.len() > 64 * 1024 { return Err(format!("invalid {FILE} file")); }
    parse(&std::fs::read(path).map_err(|e| format!("cannot read {FILE}: {e}"))?).map(Some)
}

/// Called before the node port, WebView profile, single-instance plugin or any worker thread.
/// An invalid deployment fails startup instead of silently opening another node/profile.
pub fn initialize() -> Result<(), String> {
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    let path = executable.parent().ok_or("executable directory unavailable")?.join(FILE);
    if let Some(environment) = read(&path)? {
        // Validate the whole document before changing even one process setting.
        for (key, value) in environment { std::env::set_var(key, value); }
        // A desktop shortcut must run the deployed sidecar, never a shell's dev override.
        std::env::remove_var("RYNMESH_PEER_CMD");
        std::env::remove_var("PYTHONPATH");
    }
    Ok(())
}

pub fn desktop_config_dir() -> Option<PathBuf> {
    std::env::var_os("RYN_DESKTOP_CONFIG_DIR").filter(|value| !value.is_empty()).map(PathBuf::from)
}

/// Editor cookies/storage and menu icons must follow the same native profile even when
/// Explorer launches outside the package context that originally created the profile.
pub fn native_profile_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("RYN_DESKTOP_PROFILE_DIR").filter(|value| !value.is_empty()) {
        return Ok(PathBuf::from(path));
    }
    app.path().app_local_data_dir().map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> serde_json::Value {
        let folder = std::env::temp_dir().to_string_lossy().into_owned();
        serde_json::json!({ "version": 1, "environment": {
            "RYNMESH_HOME": folder, "WEBVIEW2_USER_DATA_FOLDER": folder,
            "RYN_DESKTOP_CONFIG_DIR": folder, "RYN_DESKTOP_PROFILE_DIR": folder,
            "RYNMESH_PEER_PORT": "8792", "RYN_SYSTEM_APPS": "1"
        } })
    }

    #[test]
    fn deployment_preserves_the_explicit_profile_and_port() {
        let parsed = parse(&serde_json::to_vec(&config()).unwrap()).unwrap();
        assert_eq!(parsed["RYNMESH_PEER_PORT"], "8792");
        assert_eq!(parsed["RYNMESH_HOME"], std::env::temp_dir().to_string_lossy());
        assert_eq!(parsed["RYN_DESKTOP_PROFILE_DIR"], std::env::temp_dir().to_string_lossy());
    }

    #[test]
    fn deployment_rejects_unknown_commands_versions_and_invalid_profiles() {
        for (key, value) in [("RYNMESH_PEER_CMD", "python dev.py"), ("PYTHONPATH", "D:/source"),
            ("UNKNOWN", "1"), ("RYNMESH_PEER_PORT", "0"), ("RYNMESH_PEER_PORT", "65536"),
            ("RYNMESH_HOME", "relative/profile"), ("RYN_DESKTOP_PROFILE_DIR", "relative/native"),
            ("RYN_SYSTEM_APPS", "yes"), ("LOCALAPPDATA", "")] {
            let mut value_config = config();
            value_config["environment"][key] = serde_json::Value::from(value);
            assert!(parse(&serde_json::to_vec(&value_config).unwrap()).is_err(), "{key}={value}");
        }
        let mut invalid = config(); invalid["version"] = 2.into();
        assert!(parse(&serde_json::to_vec(&invalid).unwrap()).is_err());
        let mut invalid = config(); invalid["unexpected"] = true.into();
        assert!(parse(&serde_json::to_vec(&invalid).unwrap()).is_err());
        let mut invalid = config(); invalid["environment"].as_object_mut().unwrap().remove("RYNMESH_HOME");
        assert!(parse(&serde_json::to_vec(&invalid).unwrap()).is_err());
    }

    #[test]
    fn absent_config_keeps_normal_startup_but_invalid_config_is_not_ignored() {
        let folder = std::env::temp_dir().join(format!("ryn-deployment-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        let path = folder.join(FILE);
        assert!(read(&path).unwrap().is_none());
        std::fs::write(&path, b"not json").unwrap();
        assert!(read(&path).is_err());
        std::fs::write(&path, serde_json::to_vec(&config()).unwrap()).unwrap();
        assert!(read(&path).unwrap().is_some());
        std::fs::remove_dir_all(folder).unwrap();
    }
}
