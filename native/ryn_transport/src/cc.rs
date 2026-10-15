//! Adaptive congestion control: CUBIC by default, BBR on random loss.
//!
//! Loss-based controllers (CUBIC, NewReno) treat every lost packet as
//! congestion. On Wi-Fi, mobile and long international paths a fraction of a
//! percent of *random* loss is normal, and measured on this project's bench it
//! cut CUBIC from ~90 Mbit/s to ~4 Mbit/s at 0.5 % loss. BBR models bandwidth
//! and RTT instead and keeps the link full, but quinn's BBR (BBRv1 style) also
//! overshoots shallow bottleneck queues and causes heavy loss there.
//!
//! This controller tells the two kinds of loss apart with the RTT:
//!
//! * congestion fills a queue first, so losses come with RTT inflation;
//! * random loss happens while RTT stays near its minimum.
//!
//! It starts as CUBIC (fair, well understood). When two of the last three
//! epochs show loss while the RTT stays near its floor, it hands over to BBR.
//! In BBR mode the window is capped at `gain` x the measured bandwidth-delay
//! product (bandwidth from recent epochs, RTT floor). The gain starts at 2,
//! shrinks by 0.75x when BBR overshoots the bottleneck queue (loss well above
//! the random loss CUBIC saw) and grows by 1.1x per clean epoch, between 1.1
//! and 2.5. quinn's BBR (v1 style) otherwise fills shallow queues and loses
//! up to half of what it sends. It returns to
//! CUBIC when the RTT shows a standing queue (competing traffic,
//! bufferbloat), where CUBIC is the better neighbour. Every switch keeps the
//! current window so the transfer does not restart from slow start.

use std::any::Any;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use quinn::congestion::{BbrConfig, Controller, ControllerFactory, CubicConfig};
use quinn_proto::RttEstimator;

#[derive(Debug, Clone)]
pub struct AdaptiveConfig {
    /// Loss share (bytes lost / bytes resolved) treated as significant.
    pub loss_threshold: f64,
    /// RTT / min RTT below which a loss is considered non-congestive.
    pub calm_inflation: f64,
    /// RTT / min RTT above which BBR is considered to be building a queue.
    pub queue_inflation: f64,
    /// Qualifying epochs among the last three before switching.
    pub epochs_to_switch: u32,
    /// BBR loss above max(this, `overshoot_factor` x baseline) is overshoot.
    pub overshoot_loss: f64,
    pub overshoot_factor: f64,
    /// BBR window cap as a multiple of bandwidth x min RTT: start, floor, ceiling.
    pub bbr_cap_gain: f64,
    pub bbr_cap_min: f64,
    pub bbr_cap_max: f64,
    /// Shortest measurement epoch (also at least 4 RTTs).
    pub min_epoch: Duration,
    /// Minimum time in a mode before switching again.
    pub min_dwell: Duration,
    /// Mirrors the current mode for stats (0 = CUBIC, 1 = BBR) and counts switches.
    pub telemetry: Arc<Telemetry>,
}

#[derive(Debug, Default)]
pub struct Telemetry {
    pub mode: AtomicU32,
    pub switches: AtomicU32,
}

impl Default for AdaptiveConfig {
    fn default() -> Self {
        Self {
            loss_threshold: 0.001,
            calm_inflation: 1.3,
            queue_inflation: 2.5,
            epochs_to_switch: 2,
            overshoot_loss: 0.03,
            overshoot_factor: 4.0,
            bbr_cap_gain: 2.0,
            bbr_cap_min: 1.1,
            bbr_cap_max: 2.5,
            min_epoch: Duration::from_millis(300),
            min_dwell: Duration::from_secs(1),
            telemetry: Arc::new(Telemetry::default()),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Cubic,
    Bbr,
}

pub struct Adaptive {
    cfg: Arc<AdaptiveConfig>,
    inner: Box<dyn Controller>,
    mode: Mode,
    mtu: u16,
    switched_at: Instant,
    epoch_start: Instant,
    epoch_acked: u64,
    epoch_lost: u64,
    rtt_sum: f64,
    rtt_samples: u32,
    srtt: Duration,
    min_rtt: Duration,
    /// Recent per-epoch delivery rates (bytes/s), newest last.
    rates: [f64; 4],
    /// Qualifying bits of the last epochs with enough traffic (bit 0 newest).
    history: u8,
    /// Random loss CUBIC saw before handing over to BBR.
    baseline_loss: f64,
    /// BBR window cap as a multiple of the measured BDP (BBR mode only).
    cap_gain: Option<f64>,
}

impl Adaptive {
    fn build(mode: Mode, now: Instant, mtu: u16, window: Option<u64>) -> Box<dyn Controller> {
        match mode {
            Mode::Cubic => {
                let mut config = CubicConfig::default();
                if let Some(window) = window {
                    config.initial_window(window);
                }
                Arc::new(config).build(now, mtu)
            }
            Mode::Bbr => {
                let mut config = BbrConfig::default();
                if let Some(window) = window {
                    config.initial_window(window);
                }
                Arc::new(config).build(now, mtu)
            }
        }
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }

    fn bdp_cap(&self) -> Option<u64> {
        let gain = self.cap_gain?;
        let rate = self.rates.iter().cloned().fold(0.0, f64::max);
        if rate <= 0.0 || self.min_rtt.is_zero() {
            return None;
        }
        let cap = rate * self.min_rtt.as_secs_f64() * gain;
        Some((cap as u64).max(16 * self.mtu as u64))
    }

    fn switch(&mut self, now: Instant, to: Mode) {
        let window = self.window().max(4 * self.mtu as u64);
        self.inner = Self::build(to, now, self.mtu, Some(window));
        self.mode = to;
        self.switched_at = now;
        self.history = 0;
        self.cap_gain = (to == Mode::Bbr).then_some(self.cfg.bbr_cap_gain);
        self.cfg.telemetry.mode.store(if to == Mode::Bbr { 1 } else { 0 }, Ordering::Relaxed);
        self.cfg.telemetry.switches.fetch_add(1, Ordering::Relaxed);
    }

    fn end_epoch(&mut self, now: Instant) {
        let elapsed = now.duration_since(self.epoch_start);
        let resolved = self.epoch_acked + self.epoch_lost;
        let loss = if resolved > 0 { self.epoch_lost as f64 / resolved as f64 } else { 0.0 };
        let mean_rtt = if self.rtt_samples > 0 { self.rtt_sum / self.rtt_samples as f64 } else { self.srtt.as_secs_f64() };
        let inflation = if self.min_rtt.is_zero() { 1.0 } else { mean_rtt / self.min_rtt.as_secs_f64() };
        self.rates.rotate_left(1);
        self.rates[3] = self.epoch_acked as f64 / elapsed.as_secs_f64().max(1e-3);
        // Too little traffic to judge: an idle or application-limited epoch.
        let enough = resolved >= 32 * self.mtu as u64;
        let dwelt = now.duration_since(self.switched_at) >= self.cfg.min_dwell;
        if enough {
            let qualifies = match self.mode {
                Mode::Cubic => loss >= self.cfg.loss_threshold && inflation < self.cfg.calm_inflation,
                Mode::Bbr => inflation > self.cfg.queue_inflation,
            };
            self.history = ((self.history << 1) | qualifies as u8) & 0b111;
            if self.mode == Mode::Cubic && qualifies {
                self.baseline_loss = if self.baseline_loss == 0.0 { loss } else { 0.5 * (self.baseline_loss + loss) };
            }
            if self.mode == Mode::Bbr {
                let overshoot = loss > self.cfg.overshoot_loss.max(self.cfg.overshoot_factor * self.baseline_loss);
                let gain = self.cap_gain.unwrap_or(self.cfg.bbr_cap_gain);
                let gain = if overshoot { gain * 0.75 } else { gain * 1.1 };
                self.cap_gain = Some(gain.clamp(self.cfg.bbr_cap_min, self.cfg.bbr_cap_max));
            }
        }
        let qualifying = self.history.count_ones();
        if std::env::var_os("RYN_CC_DEBUG").is_some() {
            eprintln!(
                "cc epoch mode={:?} loss={:.4} infl={:.2} rate={:.0}KB/s cap={:?} win={} q={:03b}",
                self.mode, loss, inflation, self.rates[3] / 1e3, self.bdp_cap(), self.inner.window(), self.history
            );
        }
        if dwelt && qualifying >= self.cfg.epochs_to_switch {
            let to = if self.mode == Mode::Cubic { Mode::Bbr } else { Mode::Cubic };
            self.switch(now, to);
        }
        self.epoch_start = now;
        self.epoch_acked = 0;
        self.epoch_lost = 0;
        self.rtt_sum = 0.0;
        self.rtt_samples = 0;
    }
}

impl Controller for Adaptive {
    fn on_sent(&mut self, now: Instant, bytes: u64, last_packet_number: u64) {
        self.inner.on_sent(now, bytes, last_packet_number);
    }

    fn on_ack(&mut self, now: Instant, sent: Instant, bytes: u64, app_limited: bool, rtt: &RttEstimator) {
        self.epoch_acked += bytes;
        self.srtt = rtt.get();
        self.min_rtt = rtt.min();
        self.rtt_sum += self.srtt.as_secs_f64();
        self.rtt_samples += 1;
        self.inner.on_ack(now, sent, bytes, app_limited, rtt);
    }

    fn on_end_acks(&mut self, now: Instant, in_flight: u64, app_limited: bool, largest: Option<u64>) {
        self.inner.on_end_acks(now, in_flight, app_limited, largest);
        let epoch = self.cfg.min_epoch.max(self.srtt * 4);
        if now.duration_since(self.epoch_start) >= epoch {
            self.end_epoch(now);
        }
    }

    fn on_congestion_event(&mut self, now: Instant, sent: Instant, persistent: bool, lost_bytes: u64) {
        self.epoch_lost += lost_bytes;
        self.inner.on_congestion_event(now, sent, persistent, lost_bytes);
    }

    fn on_mtu_update(&mut self, new_mtu: u16) {
        self.mtu = new_mtu;
        self.inner.on_mtu_update(new_mtu);
    }

    fn window(&self) -> u64 {
        let window = self.inner.window();
        match (self.mode, self.bdp_cap()) {
            (Mode::Bbr, Some(cap)) => window.min(cap),
            _ => window,
        }
    }

    fn clone_box(&self) -> Box<dyn Controller> {
        Box::new(Adaptive {
            cfg: self.cfg.clone(),
            inner: self.inner.clone_box(),
            mode: self.mode,
            mtu: self.mtu,
            switched_at: self.switched_at,
            epoch_start: self.epoch_start,
            epoch_acked: self.epoch_acked,
            epoch_lost: self.epoch_lost,
            rtt_sum: self.rtt_sum,
            rtt_samples: self.rtt_samples,
            srtt: self.srtt,
            min_rtt: self.min_rtt,
            rates: self.rates,
            history: self.history,
            baseline_loss: self.baseline_loss,
            cap_gain: self.cap_gain,
        })
    }

    fn initial_window(&self) -> u64 {
        self.inner.initial_window()
    }

    fn into_any(self: Box<Self>) -> Box<dyn Any> {
        self
    }
}

impl ControllerFactory for AdaptiveConfig {
    fn build(self: Arc<Self>, now: Instant, current_mtu: u16) -> Box<dyn Controller> {
        self.telemetry.mode.store(0, Ordering::Relaxed);
        Box::new(Adaptive {
            inner: Adaptive::build(Mode::Cubic, now, current_mtu, None),
            cfg: self,
            mode: Mode::Cubic,
            mtu: current_mtu,
            switched_at: now,
            epoch_start: now,
            epoch_acked: 0,
            epoch_lost: 0,
            rtt_sum: 0.0,
            rtt_samples: 0,
            srtt: Duration::ZERO,
            min_rtt: Duration::ZERO,
            rates: [0.0; 4],
            history: 0,
            baseline_loss: 0.0,
            cap_gain: None,
        })
    }
}

/// Congestion controller factory by name: "adaptive" (default), "cubic", "bbr", "newreno".
pub fn factory(name: &str, telemetry: Arc<Telemetry>) -> Arc<dyn ControllerFactory + Send + Sync> {
    match name {
        "cubic" => Arc::new(CubicConfig::default()),
        "bbr" => Arc::new(BbrConfig::default()),
        "newreno" => Arc::new(quinn::congestion::NewRenoConfig::default()),
        _ => Arc::new(AdaptiveConfig { telemetry, ..AdaptiveConfig::default() }),
    }
}
