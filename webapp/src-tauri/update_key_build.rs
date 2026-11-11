use base64::{engine::general_purpose::STANDARD, Engine};

pub const OFFICIAL_KEY: &str = include_str!("desktop-update.pub");

fn validate(key: &str) -> Result<(), &'static str> {
    let decoded = STANDARD.decode(key).map_err(|_| "Public key must be base64")?;
    let text = std::str::from_utf8(&decoded).map_err(|_| "Public key must contain minisign text")?;
    let mut lines = text.lines();
    if !lines.next().is_some_and(|line| line.starts_with("untrusted comment: ")) {
        return Err("Public key must have a minisign comment");
    }
    let packet = STANDARD.decode(lines.next().ok_or("Missing public key packet")?)
        .map_err(|_| "Invalid public key packet")?;
    if packet.len() != 42 || &packet[..2] != b"Ed" || lines.any(|line| !line.trim().is_empty()) {
        return Err("Expected an Ed25519 minisign public key, not a private key");
    }
    Ok(())
}

/// All build entry points retain official update trust, even without CI environment variables.
/// Alternate keys require the explicit test endpoint already used by updater VM tests.
pub fn resolve_public_key(supplied: Option<&str>, test_endpoint: Option<&str>) -> Result<String, &'static str> {
    let official = OFFICIAL_KEY.trim();
    validate(official)?;
    let key = supplied.map(str::trim).filter(|key| !key.is_empty()).unwrap_or(official);
    validate(key)?;
    if key != official && !test_endpoint.is_some_and(|endpoint| !endpoint.trim().is_empty()) {
        return Err("Override differs from desktop-update.pub; update the pinned public key deliberately or use an explicit test endpoint");
    }
    Ok(key.to_owned())
}
