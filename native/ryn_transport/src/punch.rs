//! UDP hole punching on one socket, before QUIC takes the same socket over.
//!
//! Both devices learn their candidates (host addresses and the public mapping
//! a STUN server sees), exchange them through signed signaling, and then send
//! authenticated probes to every candidate of the other side at the same time.
//! Outbound probes open each NAT for the peer's packets; the first address
//! from which an answer arrives is the path. The socket is then handed to
//! quinn unchanged, so the NAT mapping the probes created is the one QUIC uses.
//!
//! Probe datagram (30 bytes):
//! `"RYNP" | type | role | nonce(8) | HMAC-SHA256(key, preceding 14 bytes)[..16]`
//! with `key = SHA-256("ryn-punch-v1" | client secret | server secret)`. Only
//! the two devices that exchanged the signaling can produce or answer probes.

use std::collections::HashSet;
use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use rand::RngCore;
use ring::hmac;
use tokio::net::UdpSocket;
use tokio::time::Instant;

const MAGIC: &[u8; 4] = b"RYNP";
const PROBE: u8 = 1;
const ACK: u8 = 2;
const SELECT: u8 = 3;
const PACKET_LEN: usize = 30;
const STUN_COOKIE: u32 = 0x2112_A442;

pub const ROLE_CLIENT: u8 = 1;
pub const ROLE_SERVER: u8 = 2;

#[derive(Clone, Debug)]
pub struct Candidate {
    pub kind: String,
    pub addr: SocketAddr,
}

#[derive(Debug)]
pub struct Punched {
    pub remote: SocketAddr,
    pub remote_kind: String,
    pub elapsed: Duration,
}

/// A UDP socket with large buffers, ready for tokio, and whether it is dual
/// stack. Dual stack ([::] with IPV6_V6ONLY off) reaches IPv4 peers through
/// v4-mapped addresses and IPv6 peers directly, so one socket — one NAT
/// mapping — covers both; hosts without IPv6 get a plain IPv4 socket.
pub fn bind_socket(buffer_bytes: usize) -> io::Result<(std::net::UdpSocket, bool)> {
    let dual = (|| -> io::Result<socket2::Socket> {
        let sock = socket2::Socket::new(socket2::Domain::IPV6, socket2::Type::DGRAM, Some(socket2::Protocol::UDP))?;
        sock.set_only_v6(false)?;
        sock.bind(&SocketAddr::from((Ipv6Addr::UNSPECIFIED, 0)).into())?;
        Ok(sock)
    })();
    let (sock, is_dual) = match dual {
        Ok(sock) => (sock, true),
        Err(_) => {
            let sock = socket2::Socket::new(socket2::Domain::IPV4, socket2::Type::DGRAM, Some(socket2::Protocol::UDP))?;
            sock.bind(&SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0)).into())?;
            (sock, false)
        }
    };
    let _ = sock.set_recv_buffer_size(buffer_bytes);
    let _ = sock.set_send_buffer_size(buffer_bytes);
    sock.set_nonblocking(true)?;
    Ok((sock.into(), is_dual))
}

/// The form a peer address is compared and reported in (v4-mapped -> IPv4).
pub fn canonical(addr: SocketAddr) -> SocketAddr {
    match addr {
        SocketAddr::V6(v6) => match v6.ip().to_ipv4_mapped() {
            Some(v4) => SocketAddr::from((v4, v6.port())),
            None => addr,
        },
        SocketAddr::V4(_) => addr,
    }
}

/// The form a dual-stack socket sends to (IPv4 -> v4-mapped).
pub fn wire(addr: SocketAddr, dual: bool) -> SocketAddr {
    match addr {
        SocketAddr::V4(v4) if dual => SocketAddr::from((v4.ip().to_ipv6_mapped(), v4.port())),
        _ => addr,
    }
}

fn usable_v6(ip: &Ipv6Addr) -> bool {
    // No link-local (needs a scope id), multicast or unspecified addresses.
    !(ip.is_unspecified() || ip.is_multicast() || (ip.segments()[0] & 0xffc0) == 0xfe80 || ip.to_ipv4_mapped().is_some())
}

/// Local interface addresses with the socket's port. Loopback is listed last
/// so two nodes on one machine (tests, development) can still pair.
pub fn host_candidates(port: u16, dual: bool) -> Vec<Candidate> {
    let mut out: Vec<Candidate> = Vec::new();
    let mut loopback = Vec::new();
    let mut push = |candidate: Candidate, is_loopback: bool| {
        if is_loopback {
            loopback.push(candidate);
        } else if !out.iter().any(|c| c.addr == candidate.addr) {
            out.push(candidate);
        }
    };
    if let Ok(interfaces) = if_addrs::get_if_addrs() {
        for interface in interfaces {
            match interface.ip() {
                IpAddr::V4(ip) if !(ip.is_link_local() || ip.is_unspecified() || ip.is_multicast()) => {
                    push(Candidate { kind: "host".into(), addr: SocketAddr::from((ip, port)) }, ip.is_loopback());
                }
                IpAddr::V6(ip) if dual && usable_v6(&ip) => {
                    push(Candidate { kind: "host".into(), addr: SocketAddr::from((ip, port)) }, ip.is_loopback());
                }
                _ => {}
            }
        }
    }
    if !loopback.iter().any(|c| c.addr.is_ipv4()) {
        loopback.push(Candidate { kind: "host".into(), addr: SocketAddr::from((Ipv4Addr::LOCALHOST, port)) });
    }
    if dual && !loopback.iter().any(|c| c.addr.is_ipv6()) {
        loopback.push(Candidate { kind: "host".into(), addr: SocketAddr::from((Ipv6Addr::LOCALHOST, port)) });
    }
    // Global addresses first (IPv6 usually needs no NAT traversal), then IPv4.
    out.sort_by_key(|c| if c.addr.is_ipv6() { 0 } else { 1 });
    loopback.sort_by_key(|c| if c.addr.is_ipv4() { 0 } else { 1 });
    out.extend(loopback);
    out
}

fn is_stun(data: &[u8]) -> bool {
    data.len() >= 20 && u32::from_be_bytes([data[4], data[5], data[6], data[7]]) == STUN_COOKIE
}

fn stun_request(transaction: &[u8; 12]) -> [u8; 20] {
    let mut out = [0u8; 20];
    out[0..2].copy_from_slice(&1u16.to_be_bytes());
    out[4..8].copy_from_slice(&STUN_COOKIE.to_be_bytes());
    out[8..20].copy_from_slice(transaction);
    out
}

/// XOR-MAPPED-ADDRESS (or MAPPED-ADDRESS) of an IPv4 binding success.
fn stun_mapped(data: &[u8], transaction: &[u8; 12]) -> Option<SocketAddr> {
    if data.len() < 20 || data[0..2] != [0x01, 0x01] || &data[8..20] != transaction {
        return None;
    }
    let end = (20 + u16::from_be_bytes([data[2], data[3]]) as usize).min(data.len());
    let mut pos = 20;
    let mut plain = None;
    while pos + 4 <= end {
        let kind = u16::from_be_bytes([data[pos], data[pos + 1]]);
        let len = u16::from_be_bytes([data[pos + 2], data[pos + 3]]) as usize;
        let value = data.get(pos + 4..pos + 4 + len)?;
        if len >= 8 && value[1] == 1 {
            let port = u16::from_be_bytes([value[2], value[3]]);
            let ip = u32::from_be_bytes([value[4], value[5], value[6], value[7]]);
            match kind {
                0x0020 | 0x8020 => {
                    let ip = Ipv4Addr::from(ip ^ STUN_COOKIE);
                    return Some(SocketAddr::from((ip, port ^ (STUN_COOKIE >> 16) as u16)));
                }
                0x0001 => plain = Some(SocketAddr::from((Ipv4Addr::from(ip), port))),
                _ => {}
            }
        }
        pos += 4 + len + ((4 - len % 4) % 4);
    }
    plain
}

/// Server-reflexive candidates: asks every STUN server from this socket and
/// keeps the distinct public mappings that answer within `timeout`.
pub async fn stun_gather(sock: &UdpSocket, dual: bool, servers: &[String], timeout: Duration) -> Vec<Candidate> {
    let deadline = Instant::now() + timeout;
    let mut lookups = tokio::task::JoinSet::new();
    for server in servers.iter().cloned() {
        lookups.spawn(async move {
            let found = tokio::time::timeout(Duration::from_secs(2), tokio::net::lookup_host(server)).await;
            found.ok().and_then(|r| r.ok()).and_then(|mut addrs| addrs.find(SocketAddr::is_ipv4))
        });
    }
    let mut targets: Vec<(SocketAddr, [u8; 12])> = Vec::new();
    while let Ok(Some(joined)) = tokio::time::timeout_at(deadline, lookups.join_next()).await {
        if let Ok(Some(addr)) = joined {
            let mut transaction = [0u8; 12];
            rand::thread_rng().fill_bytes(&mut transaction);
            let _ = sock.send_to(&stun_request(&transaction), wire(addr, dual)).await;
            targets.push((addr, transaction));
        }
    }
    lookups.abort_all();
    let mut found: Vec<Candidate> = Vec::new();
    let mut first_answer: Option<Instant> = None;
    let mut resend_at = Instant::now() + Duration::from_millis(400);
    let mut buf = [0u8; 1500];
    while !targets.is_empty() {
        // After the first answer, give the others a short grace period only.
        let stop = first_answer.map(|t| (t + Duration::from_millis(150)).min(deadline)).unwrap_or(deadline);
        let wake = stop.min(resend_at);
        match tokio::time::timeout_at(wake, sock.recv_from(&mut buf)).await {
            Ok(Ok((n, _from))) => {
                let data = &buf[..n];
                if let Some(index) = targets.iter().position(|(_, t)| stun_mapped(data, t).is_some()) {
                    let (_, transaction) = targets.remove(index);
                    if let Some(mapped) = stun_mapped(data, &transaction) {
                        if !found.iter().any(|c| c.addr == mapped) {
                            found.push(Candidate { kind: "srflx".into(), addr: mapped });
                        }
                        first_answer.get_or_insert_with(Instant::now);
                    }
                }
            }
            Ok(Err(_)) => {}
            Err(_) => {
                if Instant::now() >= stop {
                    break;
                }
                for (addr, transaction) in &targets {
                    let _ = sock.send_to(&stun_request(transaction), wire(*addr, dual)).await;
                }
                resend_at = Instant::now() + Duration::from_millis(600);
            }
        }
    }
    found
}

pub fn punch_key(client_secret: &[u8], server_secret: &[u8]) -> hmac::Key {
    let mut ctx = ring::digest::Context::new(&ring::digest::SHA256);
    ctx.update(b"ryn-punch-v1");
    ctx.update(client_secret);
    ctx.update(server_secret);
    hmac::Key::new(hmac::HMAC_SHA256, ctx.finish().as_ref())
}

fn encode(key: &hmac::Key, kind: u8, role: u8, nonce: [u8; 8]) -> [u8; PACKET_LEN] {
    let mut out = [0u8; PACKET_LEN];
    out[0..4].copy_from_slice(MAGIC);
    out[4] = kind;
    out[5] = role;
    out[6..14].copy_from_slice(&nonce);
    let tag = hmac::sign(key, &out[..14]);
    out[14..30].copy_from_slice(&tag.as_ref()[..16]);
    out
}

fn decode(key: &hmac::Key, data: &[u8]) -> Option<(u8, u8, [u8; 8])> {
    if data.len() != PACKET_LEN || &data[0..4] != MAGIC {
        return None;
    }
    let tag = hmac::sign(key, &data[..14]);
    // Constant-time comparison of the truncated tag.
    if !constant_time_eq(&tag.as_ref()[..16], &data[14..30]) {
        return None;
    }
    let mut nonce = [0u8; 8];
    nonce.copy_from_slice(&data[6..14]);
    Some((data[4], data[5], nonce))
}

/// Opens a path to one of `remotes`. The client (QUIC initiator) picks the
/// first address that answers its probes and tells the server; the server
/// answers probes until it learns the choice (or sees QUIC arrive from an
/// address that already proved itself).
#[allow(clippy::too_many_arguments)]
pub async fn punch(
    sock: &UdpSocket,
    dual: bool,
    key: &hmac::Key,
    role: u8,
    remotes: &[Candidate],
    timeout: Duration,
) -> io::Result<Punched> {
    let started = Instant::now();
    let deadline = started + timeout;
    let mut targets: Vec<SocketAddr> = Vec::new();
    for candidate in remotes {
        let addr = canonical(candidate.addr);
        if (addr.is_ipv4() || dual) && !targets.contains(&addr) {
            targets.push(addr);
        }
    }
    if targets.is_empty() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "no usable remote candidates"));
    }
    let kind_of = |addr: SocketAddr| {
        remotes.iter().find(|c| canonical(c.addr) == addr).map(|c| c.kind.clone()).unwrap_or_else(|| "prflx".into())
    };
    let mut proven: HashSet<SocketAddr> = HashSet::new();
    let mut interval = Duration::from_millis(40);
    let mut next_send = Instant::now();
    let mut buf = [0u8; 2048];
    loop {
        let now = Instant::now();
        if now >= deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "hole punching timed out"));
        }
        if now >= next_send {
            for target in &targets {
                let mut nonce = [0u8; 8];
                rand::thread_rng().fill_bytes(&mut nonce);
                let _ = sock.send_to(&encode(key, PROBE, role, nonce), wire(*target, dual)).await;
            }
            next_send = now + interval;
            if now.duration_since(started) > Duration::from_secs(1) {
                interval = (interval * 3 / 2).min(Duration::from_millis(250));
            }
        }
        let (n, from) = match tokio::time::timeout_at(next_send.min(deadline), sock.recv_from(&mut buf)).await {
            Ok(Ok((n, from))) => (n, canonical(from)),
            Ok(Err(_)) | Err(_) => continue,
        };
        let data = &buf[..n];
        match decode(key, data) {
            Some((PROBE, sender, nonce)) if sender != role => {
                let _ = sock.send_to(&encode(key, ACK, role, nonce), wire(from, dual)).await;
                proven.insert(from);
            }
            Some((ACK, sender, _)) if sender != role => {
                proven.insert(from);
                if role == ROLE_CLIENT {
                    for _ in 0..3 {
                        let mut nonce = [0u8; 8];
                        rand::thread_rng().fill_bytes(&mut nonce);
                        let _ = sock.send_to(&encode(key, SELECT, role, nonce), wire(from, dual)).await;
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    }
                    return Ok(Punched { remote: from, remote_kind: kind_of(from), elapsed: started.elapsed() });
                }
            }
            Some((SELECT, sender, _)) if sender != role && role == ROLE_SERVER => {
                return Ok(Punched { remote: from, remote_kind: kind_of(from), elapsed: started.elapsed() });
            }
            None if role == ROLE_SERVER && proven.contains(&from) && !is_stun(data) => {
                // The SELECT was lost but QUIC already started on that path;
                // the client retransmits its Initial to quinn shortly.
                return Ok(Punched { remote: from, remote_kind: kind_of(from), elapsed: started.elapsed() });
            }
            _ => {}
        }
    }
}

/// Equal-length comparison whose time does not depend on where bytes differ.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub fn random_secret() -> [u8; 16] {
    let mut secret = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut secret);
    secret
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probes_authenticate_and_reject_tampering() {
        let key = punch_key(b"client-secret", b"server-secret");
        let packet = encode(&key, PROBE, ROLE_CLIENT, [7; 8]);
        assert_eq!(decode(&key, &packet), Some((PROBE, ROLE_CLIENT, [7; 8])));
        let mut tampered = packet;
        tampered[5] = ROLE_SERVER;
        assert_eq!(decode(&key, &tampered), None);
        let other = punch_key(b"client-secret", b"other");
        assert_eq!(decode(&other, &packet), None);
    }

    #[test]
    fn maps_between_ipv4_and_dual_stack_forms() {
        let v4: SocketAddr = "203.0.113.9:5000".parse().unwrap();
        let mapped = wire(v4, true);
        assert_eq!(mapped.to_string(), "[::ffff:203.0.113.9]:5000");
        assert_eq!(canonical(mapped), v4);
        assert_eq!(wire(v4, false), v4);
        let v6: SocketAddr = "[2001:db8::1]:6000".parse().unwrap();
        assert_eq!(canonical(v6), v6);
        assert!(!usable_v6(&"fe80::1".parse().unwrap()));
        assert!(usable_v6(&"2001:db8::1".parse().unwrap()));
    }

    #[test]
    fn parses_xor_mapped_address() {
        let transaction = [9u8; 12];
        let mut response = vec![0x01, 0x01, 0x00, 0x0c];
        response.extend_from_slice(&STUN_COOKIE.to_be_bytes());
        response.extend_from_slice(&transaction);
        let port = 50132u16 ^ (STUN_COOKIE >> 16) as u16;
        let ip = u32::from(Ipv4Addr::new(98, 158, 108, 218)) ^ STUN_COOKIE;
        response.extend_from_slice(&[0x00, 0x20, 0x00, 0x08, 0x00, 0x01]);
        response.extend_from_slice(&port.to_be_bytes());
        response.extend_from_slice(&ip.to_be_bytes());
        assert_eq!(stun_mapped(&response, &transaction), Some("98.158.108.218:50132".parse().unwrap()));
        assert!(is_stun(&response));
    }
}
