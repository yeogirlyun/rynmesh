const INVALID_URL: &str = "unsupported_mcp_auth_url";

fn validate_url(value: &str) -> Result<tauri::Url, String> {
    if value.len() > 8192 || value.chars().any(|c| c.is_control() || c.is_whitespace() || c == '\\') {
        return Err(INVALID_URL.into());
    }
    // Check the original authority before URL normalization can erase an empty
    // userinfo component or turn an alternate numeric host into loopback.
    let (scheme, remainder) = value.split_once("://").ok_or(INVALID_URL)?;
    let authority = remainder.split(['/', '?', '#']).next().ok_or(INVALID_URL)?;
    if authority.is_empty() || authority.contains('@') {
        return Err(INVALID_URL.into());
    }
    let url = tauri::Url::parse(value).map_err(|_| INVALID_URL)?;
    if url.host_str().is_none() || !url.username().is_empty() || url.password().is_some()
        || url.fragment().is_some() || url.port() == Some(0)
    {
        return Err(INVALID_URL.into());
    }
    match url.scheme() {
        "https" => {},
        "http" if scheme.eq_ignore_ascii_case("http") => {
            let host = authority.split(':').next().unwrap_or("");
            if !host.eq_ignore_ascii_case("localhost") && host != "127.0.0.1" {
                return Err(INVALID_URL.into());
            }
        },
        _ => return Err(INVALID_URL.into()),
    }
    for (key, _) in url.query_pairs() {
        if matches!(key.to_ascii_lowercase().as_str(),
            "access_token" | "refresh_token" | "id_token" | "client_secret" | "code"
            | "code_verifier" | "token" | "secret" | "password" | "authorization")
        {
            return Err(INVALID_URL.into());
        }
    }
    Ok(url)
}

#[tauri::command]
pub async fn open_mcp_auth_browser(url: String) -> Result<(), String> {
    let url = validate_url(&url)?;
    tauri::async_runtime::spawn_blocking(move || crate::drive_browser::launch(url.as_str()))
        .await.map_err(|_| "browser_open_failed".to_string())?
}

#[cfg(test)]
mod tests {
    use super::{validate_url, INVALID_URL};

    #[test]
    fn accepts_authorization_requests_and_exact_http_loopback() {
        for value in [
            "https://login.example/auth?client_id=desktop&state=synthetic&code_challenge=synthetic&code_challenge_method=S256&scope=read&resource=https%3A%2F%2Fapi.example%2Fmcp&redirect_uri=http%3A%2F%2F127.0.0.1%3A1245%2Fcallback",
            "https://login.example:8443/authorize",
            "http://localhost:1245/authorize?state=synthetic",
            "http://127.0.0.1:1245/authorize",
            "HTTP://LOCALHOST:1245/authorize",
        ] {
            assert!(validate_url(value).is_ok());
        }
    }

    #[test]
    fn rejects_unsafe_authorities_schemes_and_shell_arguments() {
        for value in [
            "file:///C:/Windows/cmd.exe", "javascript:alert(1)", "--help", "//login.example/auth",
            "http://login.example/auth", "http://localhost.evil.example/auth", "http://localhost./auth",
            "http://127.1/auth", "http://2130706433/auth", "http://0177.0.0.1/auth", "http://[::1]/auth",
            "http://127.0.0.1:0/auth", "https://user:synthetic@login.example/auth",
            "https://@login.example/auth", "https://login.example@evil.example/auth",
            "https://login.example/auth#fragment", "https://login.example/auth#",
            "https://login.example/auth\n--help", "https://login.example/auth\0", "https://login.example/auth --help",
            "https://login.example\\@evil.example/auth", "https:/login.example/auth", "https://[invalid/auth",
        ] {
            assert_eq!(validate_url(value).unwrap_err(), INVALID_URL);
        }
    }

    #[test]
    fn rejects_decoded_credential_query_keys_without_echoing_values() {
        for key in ["access_token", "refresh_token", "id_token", "client_secret", "code",
            "code_verifier", "token", "secret", "password", "authorization", "CLIENT_SECRET", "%63ode"] {
            let value = format!("https://login.example/auth?{key}=synthetic-private");
            assert_eq!(validate_url(&value).unwrap_err(), INVALID_URL);
        }
    }

    #[test]
    fn enforces_input_length_before_launch() {
        let prefix = "https://login.example/auth?state=";
        let allowed = format!("{}{}", prefix, "x".repeat(8192 - prefix.len()));
        assert!(validate_url(&allowed).is_ok());
        assert_eq!(validate_url(&(allowed + "x")).unwrap_err(), INVALID_URL);
    }
}
