//! Python bindings. Every I/O method returns an asyncio awaitable; timeouts
//! are applied here (not by cancelling the awaitable), so a timed-out read
//! never loses bytes that quinn already handed over.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyConnectionError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use pyo3_async_runtimes::tokio::future_into_py;
use tokio::sync::Mutex as AsyncMutex;

use crate::{cc, punch, quic};

const SOCKET_BUFFER: usize = 8 << 20;
const READ_CHUNK_MAX: usize = 4 << 20;

fn io_error(e: std::io::Error) -> PyErr {
    if e.kind() == std::io::ErrorKind::TimedOut {
        PyTimeoutError::new_err(e.to_string())
    } else {
        PyConnectionError::new_err(e.to_string())
    }
}

fn connection_error(e: impl std::fmt::Display) -> PyErr {
    PyConnectionError::new_err(e.to_string())
}

async fn bounded<T, F>(timeout_s: Option<f64>, what: &'static str, fut: F) -> PyResult<T>
where
    F: std::future::Future<Output = PyResult<T>>,
{
    match timeout_s {
        Some(seconds) if seconds > 0.0 => tokio::time::timeout(Duration::from_secs_f64(seconds), fut)
            .await
            .unwrap_or_else(|_| Err(PyTimeoutError::new_err(format!("{what} timed out")))),
        _ => fut.await,
    }
}

fn role_name(role: u8) -> &'static str {
    if role == punch::ROLE_CLIENT {
        "client"
    } else {
        "server"
    }
}

/// One UDP socket: gathered candidates, then (once) hole punching and QUIC.
#[pyclass(module = "ryn_transport", frozen)]
struct Session {
    role: u8,
    dual: bool,
    secret: [u8; 16],
    candidates: Vec<punch::Candidate>,
    identity: Option<Arc<quic::Identity>>,
    socket: Mutex<Option<tokio::net::UdpSocket>>,
    gather_ms: u64,
}

/// Binds a socket and gathers its candidates. `role` is "client" (dials,
/// pins the server certificate) or "server" (answers, presents one).
#[pyfunction]
#[pyo3(signature = (stun_servers, gather_timeout_s, role))]
fn open_session<'py>(py: Python<'py>, stun_servers: Vec<String>, gather_timeout_s: f64, role: &str) -> PyResult<Bound<'py, PyAny>> {
    let role = match role {
        "client" => punch::ROLE_CLIENT,
        "server" => punch::ROLE_SERVER,
        _ => return Err(PyValueError::new_err("role must be 'client' or 'server'")),
    };
    future_into_py(py, async move {
        let started = Instant::now();
        let (std_socket, dual) = punch::bind_socket(SOCKET_BUFFER).map_err(io_error)?;
        let socket = tokio::net::UdpSocket::from_std(std_socket).map_err(io_error)?;
        let port = socket.local_addr().map_err(io_error)?.port();
        let mut candidates = punch::host_candidates(port, dual);
        if !stun_servers.is_empty() && gather_timeout_s > 0.0 {
            let found =
                punch::stun_gather(&socket, dual, &stun_servers, Duration::from_secs_f64(gather_timeout_s)).await;
            // Public mappings first: they work across networks.
            for candidate in found.into_iter().rev() {
                candidates.insert(0, candidate);
            }
        }
        let identity = if role == punch::ROLE_SERVER {
            Some(Arc::new(quic::self_signed().map_err(io_error)?))
        } else {
            None
        };
        Ok(Session {
            role,
            dual,
            secret: punch::random_secret(),
            candidates,
            identity,
            socket: Mutex::new(Some(socket)),
            gather_ms: started.elapsed().as_millis() as u64,
        })
    })
}

fn parse_candidates(value: &Bound<'_, PyAny>) -> PyResult<Vec<punch::Candidate>> {
    let mut out = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        let kind: String = item.get_item("type")?.extract()?;
        let addr: String = item.get_item("addr")?.extract()?;
        let addr: SocketAddr = addr.parse().map_err(|_| PyValueError::new_err("invalid candidate address"))?;
        if out.len() >= 32 {
            return Err(PyValueError::new_err("too many candidates"));
        }
        out.push(punch::Candidate { kind: kind.chars().take(8).collect(), addr });
    }
    Ok(out)
}

fn parse_hex<const N: usize>(value: &str, what: &str) -> PyResult<[u8; N]> {
    let bytes = hex::decode(value).map_err(|_| PyValueError::new_err(format!("{what} must be hex")))?;
    bytes.try_into().map_err(|_| PyValueError::new_err(format!("{what} has the wrong length")))
}

#[pymethods]
impl Session {
    /// Signaling payload for the other device.
    fn info<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("v", 1)?;
        out.set_item("role", role_name(self.role))?;
        out.set_item("secret", hex::encode(self.secret))?;
        let candidates = PyList::empty(py);
        for candidate in &self.candidates {
            let item = PyDict::new(py);
            item.set_item("type", &candidate.kind)?;
            item.set_item("addr", candidate.addr.to_string())?;
            candidates.append(item)?;
        }
        out.set_item("candidates", candidates)?;
        out.set_item("fingerprint", self.identity.as_ref().map(|id| hex::encode(id.fingerprint)))?;
        out.set_item("gather_ms", self.gather_ms)?;
        out.set_item("dual_stack", self.dual)?;
        Ok(out)
    }

    /// Punches a path to the peer described by its `info()` and runs the QUIC
    /// handshake on it. Usable once.
    #[pyo3(signature = (remote, cc = "adaptive".to_string(), timeout_s = 20.0))]
    fn connect<'py>(&self, py: Python<'py>, remote: &Bound<'py, PyAny>, cc: String, timeout_s: f64) -> PyResult<Bound<'py, PyAny>> {
        let remote_secret: [u8; 16] = parse_hex(&remote.get_item("secret")?.extract::<String>()?, "secret")?;
        let candidates = parse_candidates(&remote.get_item("candidates")?)?;
        let fingerprint: Option<[u8; 32]> = match remote.get_item("fingerprint") {
            Ok(value) if !value.is_none() => Some(parse_hex(&value.extract::<String>()?, "fingerprint")?),
            _ => None,
        };
        if self.role == punch::ROLE_CLIENT && fingerprint.is_none() {
            return Err(PyValueError::new_err("the server fingerprint is required"));
        }
        let socket = self
            .socket
            .lock()
            .map_err(|_| PyValueError::new_err("session lock poisoned"))?
            .take()
            .ok_or_else(|| PyValueError::new_err("session already used"))?;
        let role = self.role;
        let dual = self.dual;
        let secret = self.secret;
        let identity = self.identity.clone();
        future_into_py(py, async move {
            let deadline = Duration::from_secs_f64(timeout_s.max(1.0));
            let started = Instant::now();
            let key = if role == punch::ROLE_CLIENT {
                punch::punch_key(&secret, &remote_secret)
            } else {
                punch::punch_key(&remote_secret, &secret)
            };
            let punched =
                punch::punch(&socket, dual, &key, role, &candidates, deadline).await.map_err(io_error)?;
            let local = socket.local_addr().map_err(io_error)?;
            let std_socket = socket.into_std().map_err(io_error)?;
            let telemetry = Arc::new(cc::Telemetry::default());
            let transport = quic::transport(&cc, telemetry.clone());
            let remaining = deadline.saturating_sub(started.elapsed()).max(Duration::from_secs(3));
            let handshake_started = Instant::now();
            let (endpoint, connection) = if role == punch::ROLE_CLIENT {
                quic::client(std_socket, punched.remote, fingerprint.expect("checked above"), transport, remaining).await
            } else {
                let identity = identity.ok_or_else(|| PyValueError::new_err("server session without identity"))?;
                quic::server(std_socket, punched.remote, &identity, transport, remaining).await
            }
            .map_err(io_error)?;
            Ok(Connection {
                connection,
                _endpoint: endpoint,
                telemetry,
                cc,
                remote: punched.remote,
                remote_kind: punched.remote_kind,
                local,
                punch_ms: punched.elapsed.as_millis() as u64,
                handshake_ms: handshake_started.elapsed().as_millis() as u64,
            })
        })
    }

    fn close(&self) {
        if let Ok(mut socket) = self.socket.lock() {
            socket.take();
        }
    }
}

#[pyclass(module = "ryn_transport", frozen)]
struct Connection {
    connection: quinn::Connection,
    _endpoint: quinn::Endpoint,
    telemetry: Arc<cc::Telemetry>,
    cc: String,
    remote: SocketAddr,
    remote_kind: String,
    local: SocketAddr,
    punch_ms: u64,
    handshake_ms: u64,
}

fn stream(send: quinn::SendStream, recv: quinn::RecvStream) -> Stream {
    Stream { id: u64::from(send.id()), send: Arc::new(AsyncMutex::new(send)), recv: Arc::new(AsyncMutex::new(recv)) }
}

#[pymethods]
impl Connection {
    #[pyo3(signature = (timeout_s = None))]
    fn open_stream<'py>(&self, py: Python<'py>, timeout_s: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let connection = self.connection.clone();
        future_into_py(py, async move {
            bounded(timeout_s, "opening a stream", async {
                let (send, recv) = connection.open_bi().await.map_err(connection_error)?;
                Ok(stream(send, recv))
            })
            .await
        })
    }

    /// Next stream the peer opened; None once the connection is closed.
    #[pyo3(signature = (timeout_s = None))]
    fn accept_stream<'py>(&self, py: Python<'py>, timeout_s: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let connection = self.connection.clone();
        future_into_py(py, async move {
            bounded(timeout_s, "accepting a stream", async {
                match connection.accept_bi().await {
                    Ok((send, recv)) => Ok(Some(stream(send, recv))),
                    Err(_) => Ok(None),
                }
            })
            .await
        })
    }

    /// Resolves with the close reason once the connection is gone.
    fn wait_closed<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let connection = self.connection.clone();
        future_into_py(py, async move { Ok(connection.closed().await.to_string()) })
    }

    #[pyo3(signature = (code = 0, reason = ""))]
    fn close(&self, code: u32, reason: &str) {
        self.connection.close(quinn::VarInt::from_u32(code), reason.as_bytes());
    }

    fn is_closed(&self) -> bool {
        self.connection.close_reason().is_some()
    }

    #[getter]
    fn remote_addr(&self) -> String {
        self.remote.to_string()
    }

    #[getter]
    fn path_kind(&self) -> String {
        self.remote_kind.clone()
    }

    fn congestion_mode(&self) -> String {
        if self.cc == "adaptive" {
            match self.telemetry.mode.load(std::sync::atomic::Ordering::Relaxed) {
                1 => "bbr".into(),
                _ => "cubic".into(),
            }
        } else {
            self.cc.clone()
        }
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = self.connection.stats();
        let out = PyDict::new(py);
        out.set_item("rtt_ms", stats.path.rtt.as_secs_f64() * 1000.0)?;
        out.set_item("cwnd", stats.path.cwnd)?;
        out.set_item("sent_packets", stats.path.sent_packets)?;
        out.set_item("lost_packets", stats.path.lost_packets)?;
        out.set_item("lost_bytes", stats.path.lost_bytes)?;
        out.set_item("mtu", stats.path.current_mtu)?;
        out.set_item("udp_tx_bytes", stats.udp_tx.bytes)?;
        out.set_item("udp_rx_bytes", stats.udp_rx.bytes)?;
        out.set_item("congestion", self.congestion_mode())?;
        out.set_item("congestion_switches", self.telemetry.switches.load(std::sync::atomic::Ordering::Relaxed))?;
        Ok(out)
    }

    /// Body-free evidence in the shape of the aioice path's `selected_pair`.
    fn evidence<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        // Same transport label as the aioice path (direct UDP, no relay), so
        // UI, audits and settlement read it unchanged; the data plane differs.
        out.set_item("transport", "ice_udp_direct")?;
        out.set_item("data_plane", "quic")?;
        out.set_item("relay_used", false)?;
        out.set_item("native", true)?;
        out.set_item("peer_public_mapping_nominated", matches!(self.remote_kind.as_str(), "srflx" | "prflx"))?;
        out.set_item(
            "path_kind",
            match self.remote_kind.as_str() {
                "host" => "host_direct",
                "srflx" => "server_reflexive",
                "prflx" => "peer_reflexive",
                _ => "direct_udp",
            },
        )?;
        let remote = PyDict::new(py);
        remote.set_item("type", &self.remote_kind)?;
        remote.set_item("transport", "udp")?;
        remote.set_item("host", self.remote.ip().to_string())?;
        remote.set_item("port", self.remote.port())?;
        out.set_item("remote", remote)?;
        let local = PyDict::new(py);
        local.set_item("transport", "udp")?;
        local.set_item("host", self.local.ip().to_string())?;
        local.set_item("port", self.local.port())?;
        out.set_item("local", local)?;
        out.set_item("punch_ms", self.punch_ms)?;
        out.set_item("handshake_ms", self.handshake_ms)?;
        out.set_item("congestion", self.congestion_mode())?;
        Ok(out)
    }
}

/// A bidirectional QUIC stream. Reads and writes may run concurrently.
#[pyclass(module = "ryn_transport", frozen)]
struct Stream {
    id: u64,
    send: Arc<AsyncMutex<quinn::SendStream>>,
    recv: Arc<AsyncMutex<quinn::RecvStream>>,
}

#[pymethods]
impl Stream {
    #[getter]
    fn id(&self) -> u64 {
        self.id
    }

    /// Writes all of `data`, waiting for flow-control credit (backpressure).
    #[pyo3(signature = (data, timeout_s = None))]
    fn write<'py>(&self, py: Python<'py>, data: &[u8], timeout_s: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let data = data.to_vec();
        let send = self.send.clone();
        future_into_py(py, async move {
            bounded(timeout_s, "stream write", async {
                send.lock().await.write_all(&data).await.map_err(connection_error)
            })
            .await
        })
    }

    /// Ends the sending side; the peer reads EOF after the data.
    fn finish<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let send = self.send.clone();
        future_into_py(py, async move { send.lock().await.finish().map_err(connection_error) })
    }

    /// Up to `max_bytes`; b"" at the end of the stream.
    #[pyo3(signature = (max_bytes = 262144, timeout_s = None))]
    fn read<'py>(&self, py: Python<'py>, max_bytes: usize, timeout_s: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let recv = self.recv.clone();
        let size = max_bytes.clamp(1, READ_CHUNK_MAX);
        future_into_py(py, async move {
            let mut buf = vec![0u8; size];
            // quinn's read is cancel-safe: a timeout here never drops data.
            let n = bounded(timeout_s, "stream read", async {
                recv.lock().await.read(&mut buf).await.map_err(connection_error)
            })
            .await?;
            let n = n.unwrap_or(0);
            Ok(Python::attach(|py| PyBytes::new(py, &buf[..n]).unbind()))
        })
    }

    /// Aborts both directions (an interrupted upload reaches the peer as an error).
    #[pyo3(signature = (code = 0))]
    fn reset<'py>(&self, py: Python<'py>, code: u32) -> PyResult<Bound<'py, PyAny>> {
        let send = self.send.clone();
        let recv = self.recv.clone();
        future_into_py(py, async move {
            let _ = send.lock().await.reset(quinn::VarInt::from_u32(code));
            let _ = recv.lock().await.stop(quinn::VarInt::from_u32(code));
            Ok(())
        })
    }
}

#[pymodule]
fn ryn_transport(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open_session, m)?)?;
    m.add_class::<Session>()?;
    m.add_class::<Connection>()?;
    m.add_class::<Stream>()?;
    m.add("PROTOCOL", "ryn-native-p2p/1")?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
