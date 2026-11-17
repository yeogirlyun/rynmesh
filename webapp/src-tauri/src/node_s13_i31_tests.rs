use super::*;

#[test]
fn s13_i31_launch_token_is_random_and_wired_to_child_and_http() {
    let token = local_token();
    use base64::Engine;
    assert_eq!(base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(&token).unwrap().len(), 32);
    assert_eq!(token, local_token());
    assert!(node_env(12345).contains(&("RYNMESH_LOCAL_TOKEN".to_string(), token.clone())));
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut bytes = [0; 8192];
        let size = stream.read(&mut bytes).unwrap();
        assert!(String::from_utf8_lossy(&bytes[..size]).contains(&format!("X-Ryn-Local-Token: {token}")));
        stream.write_all(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}").unwrap();
    });
    request(port, "GET", "/api/local/node/status", "").unwrap();
    server.join().unwrap();
}
