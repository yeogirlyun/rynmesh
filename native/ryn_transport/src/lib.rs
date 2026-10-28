//! Native P2P transport for Rynmesh: UDP hole punching + QUIC (quinn).
//!
//! Python's job stays the same as with the aioice/aioquic path: carry the
//! signaling (`Session.info()` out, the peer's info in) through the registry,
//! then speak its own protocol over streams. Everything per packet — STUN,
//! probes, QUIC, congestion control, pacing — runs here, off the GIL.

pub mod cc;
pub mod punch;
pub mod quic;

#[cfg(feature = "python")]
mod python;
