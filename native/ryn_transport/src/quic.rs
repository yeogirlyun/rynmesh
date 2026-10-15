//! QUIC (quinn) on a socket that hole punching already opened.
//!
//! The server's identity is a per-session self-signed certificate whose
//! SHA-256 travels in the server's signed signaling answer; the client accepts
//! exactly that certificate (pinning), and still verifies the TLS 1.3
//! handshake signature, so only the holder of the key can complete it.

use std::io;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use quinn::crypto::rustls::{QuicClientConfig, QuicServerConfig};
use quinn::{Endpoint, EndpointConfig, IdleTimeout, TokioRuntime, TransportConfig, VarInt};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, SignatureScheme};

use crate::cc;

pub const ALPN: &[u8] = b"ryn-p2p/1";
pub const SERVER_NAME: &str = "ryn-p2p";

pub struct Identity {
    pub cert: CertificateDer<'static>,
    pub key: PrivatePkcs8KeyDer<'static>,
    pub fingerprint: [u8; 32],
}

pub fn self_signed() -> io::Result<Identity> {
    let generated = rcgen::generate_simple_self_signed(vec![SERVER_NAME.into()])
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;
    let cert = CertificateDer::from(generated.cert);
    let key = PrivatePkcs8KeyDer::from(generated.key_pair.serialize_der());
    let mut fingerprint = [0u8; 32];
    fingerprint.copy_from_slice(ring::digest::digest(&ring::digest::SHA256, cert.as_ref()).as_ref());
    Ok(Identity { cert, key, fingerprint })
}

fn provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

#[derive(Debug)]
struct Pinned {
    fingerprint: [u8; 32],
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for Pinned {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let seen = ring::digest::digest(&ring::digest::SHA256, end_entity.as_ref());
        if crate::punch::constant_time_eq(seen.as_ref(), &self.fingerprint) {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(rustls::Error::InvalidCertificate(rustls::CertificateError::ApplicationVerificationFailure))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

pub fn transport(cc_name: &str, telemetry: Arc<cc::Telemetry>) -> TransportConfig {
    let mut t = TransportConfig::default();
    t.stream_receive_window(VarInt::from_u32(32 << 20));
    t.receive_window(VarInt::from_u32(64 << 20));
    t.send_window(64 << 20);
    t.max_concurrent_bidi_streams(VarInt::from_u32(256));
    // Keeps the NAT mapping open and finds a dead path within the idle timeout.
    t.keep_alive_interval(Some(Duration::from_secs(10)));
    t.max_idle_timeout(Some(IdleTimeout::try_from(Duration::from_secs(60)).expect("idle timeout")));
    t.congestion_controller_factory(cc::factory(cc_name, telemetry));
    t
}

fn other(e: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::Other, e.to_string())
}

fn endpoint(socket: std::net::UdpSocket, server: Option<quinn::ServerConfig>) -> io::Result<Endpoint> {
    Endpoint::new(EndpointConfig::default(), server, socket, Arc::new(TokioRuntime))
}

pub async fn client(
    socket: std::net::UdpSocket,
    remote: SocketAddr,
    fingerprint: [u8; 32],
    transport: TransportConfig,
    timeout: Duration,
) -> io::Result<(Endpoint, quinn::Connection)> {
    let provider = provider();
    let mut tls = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(other)?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(Pinned { fingerprint, provider }))
        .with_no_client_auth();
    tls.alpn_protocols = vec![ALPN.to_vec()];
    let mut config = quinn::ClientConfig::new(Arc::new(QuicClientConfig::try_from(tls).map_err(other)?));
    config.transport_config(Arc::new(transport));
    let endpoint = endpoint(socket, None)?;
    let connecting = endpoint.connect_with(config, remote, SERVER_NAME).map_err(other)?;
    let connection = tokio::time::timeout(timeout, connecting)
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "QUIC handshake timed out"))?
        .map_err(other)?;
    Ok((endpoint, connection))
}

pub async fn server(
    socket: std::net::UdpSocket,
    remote: SocketAddr,
    identity: &Identity,
    transport: TransportConfig,
    timeout: Duration,
) -> io::Result<(Endpoint, quinn::Connection)> {
    let mut tls = rustls::ServerConfig::builder_with_provider(provider())
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(other)?
        .with_no_client_auth()
        .with_single_cert(vec![identity.cert.clone()], PrivateKeyDer::Pkcs8(identity.key.clone_key()))
        .map_err(other)?;
    tls.alpn_protocols = vec![ALPN.to_vec()];
    let mut config = quinn::ServerConfig::with_crypto(Arc::new(QuicServerConfig::try_from(tls).map_err(other)?));
    config.transport_config(Arc::new(transport));
    let endpoint = endpoint(socket, Some(config))?;
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let incoming = tokio::time::timeout_at(deadline, endpoint.accept())
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "QUIC handshake timed out"))?
            .ok_or_else(|| io::Error::new(io::ErrorKind::ConnectionAborted, "endpoint closed"))?;
        // Only the device that proved itself during hole punching.
        if crate::punch::canonical(incoming.remote_address()) != crate::punch::canonical(remote) {
            incoming.refuse();
            continue;
        }
        let connection = tokio::time::timeout_at(deadline, incoming)
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "QUIC handshake timed out"))?
            .map_err(other)?;
        // The server accepts one connection per punched socket.
        endpoint.set_server_config(None);
        return Ok((endpoint, connection));
    }
}
