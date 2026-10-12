fn validate_url(value: &str) -> Result<tauri::Url, String> {
    let url = tauri::Url::parse(value).map_err(|_| "unsupported_drive_url")?;
    if url.scheme() != "https" || !url.username().is_empty() || url.password().is_some()
        || url.port().is_some()
        || !matches!(url.host_str(), Some("console.cloud.google.com" | "accounts.google.com" | "pan.baidu.com" | "openapi.baidu.com" | "entra.microsoft.com" | "login.microsoftonline.com" | "login.microsoft.com" | "microsoft.com" | "www.microsoft.com"))
    {
        return Err("unsupported_drive_url".into());
    }
    Ok(url)
}

#[tauri::command]
pub async fn open_drive_browser(url: String) -> Result<(), String> {
    let url = validate_url(&url)?;
    tauri::async_runtime::spawn_blocking(move || launch(url.as_str()))
        .await.map_err(|_| "browser_open_failed".to_string())?
}

#[cfg(windows)]
fn launch(url: &str) -> Result<(), String> {
    #[link(name = "shell32")]
    extern "system" {
        fn ShellExecuteW(window: isize, operation: *const u16, file: *const u16,
            parameters: *const u16, directory: *const u16, show: i32) -> isize;
    }
    let operation: Vec<u16> = "open\0".encode_utf16().collect();
    let target: Vec<u16> = url.encode_utf16().chain(Some(0)).collect();
    // Pass the validated URL directly to the registered HTTPS handler, never a shell command.
    let result = unsafe { ShellExecuteW(0, operation.as_ptr(), target.as_ptr(),
        std::ptr::null(), std::ptr::null(), 1) };
    if result > 32 { Ok(()) } else { Err("browser_open_failed".into()) }
}

#[cfg(not(windows))]
fn launch(url: &str) -> Result<(), String> {
    let status = std::process::Command::new(if cfg!(target_os = "macos") { "/usr/bin/open" } else { "xdg-open" })
        .arg(url).status().map_err(|_| "browser_open_failed")?;
    if status.success() { Ok(()) } else { Err("browser_open_failed".into()) }
}

#[cfg(test)]
mod tests {
    use super::validate_url;
    #[test]
    fn only_official_https_destinations() {
        for value in ["https://console.cloud.google.com/apis/library/drive.googleapis.com",
            "https://accounts.google.com/o/oauth2/v2/auth?state=a&redirect_uri=http%3A%2F%2F127.0.0.1%3A1234",
            "https://pan.baidu.com/union/console/applist", "https://openapi.baidu.com/device", "https://microsoft.com/devicelogin", "https://login.microsoft.com/device", "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"] {
            assert!(validate_url(value).is_ok());
        }
        for value in ["file:///C:/Windows/cmd.exe", "http://accounts.google.com", "javascript:alert(1)",
            "https://accounts.google.com.evil.example/", "https://accounts.google.com@evil.example/",
            "https://user@accounts.google.com/", "https://login.microsoft.com.evil.example/device", "https://pan.baidu.com:8080/", "--help"] {
            assert!(validate_url(value).is_err());
        }
    }
}
