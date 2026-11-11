#[path = "../update_key_build.rs"]
mod update_key;

use base64::{engine::general_purpose::STANDARD, Engine};
use update_key::{resolve_public_key, OFFICIAL_KEY};

#[test]
fn ordinary_builds_keep_official_trust() {
    for supplied in [None, Some(""), Some(" \r\n"), Some(OFFICIAL_KEY)] {
        assert_eq!(resolve_public_key(supplied, None).unwrap(), OFFICIAL_KEY.trim());
    }
}

#[test]
fn malformed_key_fails_even_for_test_builds() {
    for key in ["invalid", "YWJj", ""] {
        if !key.is_empty() {
            assert!(resolve_public_key(Some(key), Some("http://localhost:8000")).is_err());
        }
    }
    for packet in [vec![0; 42], b"Edshort".to_vec()] {
        let key = STANDARD.encode(format!("untrusted comment: test\n{}\n", STANDARD.encode(packet)));
        assert!(resolve_public_key(Some(&key), Some("http://localhost:8000")).is_err());
    }
}

#[test]
fn alternate_trust_requires_explicit_test_endpoint() {
    let mut packet = vec![1; 42];
    packet[..2].copy_from_slice(b"Ed");
    let key = STANDARD.encode(format!("untrusted comment: test\n{}\n", STANDARD.encode(packet)));
    assert!(resolve_public_key(Some(&key), None).is_err());
    assert!(resolve_public_key(Some(&key), Some("  ")).is_err());
    assert_eq!(resolve_public_key(Some(&key), Some("http://localhost:8000")).unwrap(), key);
}
