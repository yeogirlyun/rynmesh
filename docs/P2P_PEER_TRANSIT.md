# Rynmesh three-node P2P peer transit

Status: implementation contract for `rynmesh.peer-transit.v1`

## 1. Objective

Rynmesh must prefer a direct ICE/UDP path between a source peer and a target
peer.  When that path is unavailable or materially worse, it may route the
session through another ordinary Rynmesh peer:

```text
healthy:   peer 1 =============================== peer 3

degraded:  peer 1 ===== ICE/UDP ===== peer 2 ===== ICE/UDP ===== peer 3
                                      transit peer
```

Both `1 <-> 2` and `2 <-> 3` are separately negotiated ICE connections using
host or server-reflexive candidates.  The registry is a signed signaling
mailbox and STUN discovers public mappings.  Neither carries application data.
TURN candidates are not configured or accepted by this protocol.

Peer 2 is not a privileged cloud relay.  It advertises a normal signed
`rynmesh.peer-transit.v1` capacity and may be replaced by any eligible peer
that can establish both P2P legs.

An online target or transit worker refreshes its signed capacity every 15
minutes. Discovery treats records older than one hour as stale. A failed
refresh is retried on the next worker poll, and file-backed capacity records
are replaced atomically so readers never observe a half-written heartbeat.

## 2. Terminology

- **direct**: one ICE connection from source to target.
- **peer transit**: two direct ICE connections joined by an ordinary peer.
- **TURN relay**: an ICE candidate whose type is `relay`; prohibited here.
- **control plane**: signed capacity, offer, answer, and session metadata in the
  registry mailbox.
- **data plane**: encrypted application frames sent over nominated ICE pairs.

Evidence must use separate fields so that peer transit is never mistaken for
TURN:

```json
{
  "path_mode": "peer_transit",
  "ice_relay_candidate_used": false,
  "transit_peer_id": "<peer-2>"
}
```

## 3. Security model

### 3.1 Identity

Session-open records are canonical JSON signed by the source Ed25519 node key.
The target verifies that the signature key equals `source_peer_id`.  Session
responses are signed by the target and bound to `target_peer_id`.

### 3.2 End-to-end encryption

The source creates an ephemeral X25519 key for every transit session.  It
derives a session key with the target's advertised X25519 messaging public key.
The target derives the same key from its local messaging private key and the
source ephemeral public key.

Each data frame uses ChaCha20-Poly1305 with a nonce derived from direction and a
strictly increasing 64-bit sequence number.  Associated data binds:

- protocol version;
- session ID;
- source peer ID;
- target peer ID;
- direction;
- sequence number;
- final-frame flag.

Peer 2 forwards the encrypted frame unchanged.  It knows the two peer IDs,
session ID, timing and byte counts, but cannot decrypt request or response
payloads.

### 3.3 Abuse boundaries

- exactly one transit hop (`hop_limit=1`);
- no arbitrary IP, hostname, URL or port forwarding;
- the target is an authenticated Rynmesh peer ID;
- bounded frame size, session duration, buffered bytes and concurrent sessions;
- monotonic sequence validation and duplicate rejection;
- expired, mismatched, recursively relayed and unsigned sessions are rejected;
- peer 2 never writes payload frames to disk or application logs.

## 4. Protocol

The control capability is `rynmesh.peer-transit.v1`.

### 4.1 Capacity

Each node willing to receive peer-transit traffic advertises:

```json
{
  "capabilities": ["rynmesh.peer-transit.v1"],
  "metadata": {
    "protocol_version": "rynmesh.peer-transit.v1",
    "roles": ["target", "transit"],
    "messaging_public_key": "<base64 X25519 public key>",
    "max_concurrent": 8
  }
}
```

### 4.2 Source-to-transit order

The source submits a signed work order to peer 2 containing:

- session ID and target peer ID;
- source-to-transit ICE offer;
- source ephemeral X25519 public key;
- timeout and size limits;
- `hop_limit=1`.

Peer 2 publishes its ICE answer and creates a second signed order for peer 3.

### 4.3 Transit-to-target order

The second order contains the immutable source session identity, the
transit-to-target ICE offer and the original signed session-open record.  Peer 3
verifies the source signature rather than trusting peer 2 to describe peer 1.
It publishes its ICE answer to peer 2.

### 4.4 Data exchange

Once both pairs are nominated:

1. peer 1 sends encrypted request frames to peer 2;
2. peer 2 forwards each frame unchanged to peer 3;
3. peer 3 verifies/decrypts and produces encrypted response frames;
4. peer 2 forwards the response unchanged to peer 1;
5. all three peers emit body-free evidence.

Hop reliability uses bounded chunking, hash validation and acknowledgements.
Application streams are resumable at a verified chunk boundary; a half-written
artifact is never committed as complete.

## 5. Route selection

The route manager maintains these states:

```text
DIRECT -> DEGRADED -> PEER_TRANSIT -> RECOVERING -> DIRECT
```

Default policy, configurable through environment or local settings:

- three consecutive direct failures trigger transit immediately;
- direct loss above 8% for 30 seconds enters `DEGRADED`;
- direct P95 latency above the configured ceiling enters `DEGRADED`;
- transit is selected only when its score is at least 25% better;
- a selected transit path is held for at least 60 seconds;
- direct must pass five probes over at least 120 seconds before recovery;
- a cooldown prevents oscillation after any path change.

The initial implementation switches at request or verified chunk boundaries.
It does not silently move a partially authenticated frame between paths.

## 6. Scope

The first production slice covers generic Rynmesh request/response bytes and
streamed artifacts.  Content, preview and peer messaging call sites can then use
the routed transport.

The private LLM strict-public-P2P acceptance remains direct-only and continues
to require `relay_used=false`.  Enabling peer transit for LLM tasks requires an
explicit mode and separate evidence; it must not weaken the existing audit.
`net.egress` remains a separate SOCKS/overlay dataplane.

## 7. Acceptance contract

Automated evidence is necessary but physical three-network evidence is the
release gate for public NAT traversal.

### A. Healthy direct path

- source-to-target nominated candidates are `host`, `srflx` or `prflx` UDP;
- `path_mode=direct`;
- peer 2 data counters remain zero;
- source and target SHA-256 values match.

### B. Direct path blocked

- network policy blocks peer 1 from peer 3 but permits both peers to peer 2;
- peer 1 to peer 2 and peer 2 to peer 3 each nominate a non-TURN UDP pair;
- `path_mode=peer_transit` and `transit_peer_id` identifies peer 2;
- peer 2 ingress and egress counters cover the transferred payload;
- target output SHA-256 matches the source.

### C. Direct path degraded

- inject 250-350 ms delay, 50-100 ms jitter and 15-20% loss only on `1 <-> 3`;
- route manager changes to peer transit within 30 seconds when it is at least
  25% better;
- requests finish or retry idempotently without corruption or duplication.

### D. Recovery

- removing impairment causes a hysteresis-controlled return to direct;
- no route flapping occurs;
- transit byte counters stop increasing after the return.

### E. Transit failure

- terminating peer 2 is detected within the configured timeout;
- the request uses recovered direct connectivity, another eligible peer, or a
  bounded explicit failure;
- no permanent wait or invalid partial artifact occurs.

### F. No cloud payload relay

- no signaled or nominated candidate has ICE type `relay`;
- TURN server configuration is absent;
- signed remote signaling accepts only `host`, `srflx` or `prflx` UDP
  candidates; `relay`, non-direct and non-UDP candidates are rejected before
  they are added to the ICE agent, and programmatic signals are checked again;
- after ICE establishment, registry and STUN access can be blocked while the
  transfer continues;
- registry traffic remains control-sized and contains no application body;
- packet counters show payload volume only on `1 <-> 2` and `2 <-> 3`.

### G. Confidentiality and integrity

- a unique plaintext marker is absent from peer 2 logs, storage and captured
  data-plane packets;
- tampered ciphertext, replayed sequence numbers, forged identity, expired
  sessions and `hop_limit != 1` are rejected;
- peer 2 cannot request arbitrary network destinations.

### H. Resource and performance gates

- path change completes within 10 seconds after a hard direct failure;
- the adaptive client bounds its direct attempt to 8 seconds by default and a
  real target-policy failure test must complete through peer 2 within 10
  seconds; a state-machine-only timing assertion is insufficient evidence;
- a transit session establishes within 5 seconds on a healthy test topology;
- one GiB streams without hash mismatch and with traced Python peak memory no
  greater than 128 MiB, independent of the transferred file size;
- 20 concurrent sessions complete without deadlock;
- a 24-hour soak leaves no live-session or buffer leak;
- on a healthy local topology, protocol overhead is no more than 15% relative
  to the same two-hop forwarding path without application encryption.

## 8. Required evidence

The acceptance tool emits signed or independently derived JSON containing:

```json
{
  "source_peer_id": "<peer-1>",
  "transit_peer_id": "<peer-2>",
  "target_peer_id": "<peer-3>",
  "path_mode": "peer_transit",
  "hop_1": {"local_type": "host", "remote_type": "host"},
  "hop_2": {"local_type": "host", "remote_type": "host"},
  "ice_relay_candidate_used": false,
  "registry_payload_bytes": 0,
  "transit_rx_bytes": 0,
  "transit_tx_bytes": 0,
  "source_sha256": "sha256:<digest>",
  "target_sha256": "sha256:<digest>",
  "plaintext_found_on_transit": false,
  "result": "pass"
}
```

`scripts/audit_peer_transit.py` must reject missing evidence, TURN candidates,
identity discontinuity, byte-count contradictions, plaintext exposure and hash
mismatch.  The hermetic three-identity, real local-UDP/ICE scenario is the
deterministic CI gate; a separate physical run with three public egress networks
is required before claiming public-NAT acceptance.
