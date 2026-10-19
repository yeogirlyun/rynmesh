# Device drop: files, folders and node messages

Implementation contract for the confirmed design in
`docs/design/ryn-drop-final/` (HANDOFF.md is the behaviour contract). This file
defines the daemon API shared by the backend (`rynmesh/drop/`) and the webapp.

## Principles

- A device is its Ed25519 identity (`peer_id`). Names and addresses are display
  and addressing only.
- Every peer request is an Ed25519-signed envelope from the sender identity and,
  when it carries content, sealed to the receiver's X25519 messaging key taken
  from its *signed* peer record (registry or LAN discovery). File bytes are
  encrypted with a per-transfer key: X25519(sender ephemeral, receiver messaging
  key) -> HKDF-SHA256 -> ChaCha20-Poly1305 frames.
- Direct routes (LAN direct, public direct) have no artificial size limit.
  Offsets and sizes are Python ints / SQLite INTEGER (64-bit). JSON byte values
  are numbers when <= 2^53-1, otherwise decimal strings. The webapp must accept
  both (`bytesValue()`).
- Relay is a separate capability. It is reported as unavailable until a relay
  service with quotas exists. A direct transfer never switches to relay silently.
- Offline queues stay on the sender's machine. No server-side offline storage is
  promised for drop messages or files.

## Local API (control surface, `local_control`)

All paths are under `/api/local/drop`. `peer_id` is always a query/body field,
never a path segment (base64 contains `/`).

| Method | Path | Body / query | Result |
| --- | --- | --- | --- |
| GET | `/capabilities` | | `Capabilities` |
| GET | `/peers` | | `PeerMeta[]` |
| GET | `/snapshot` | | `{seq, transfers: Transfer[], peers: PeerMeta[], incoming: number}` |
| GET | `/events?since=seq` | | SSE, `data: Event` |
| GET | `/conversation?peer_id=` | `before?`, `limit?` | `{peer_id, items: ConversationItem[]}` |
| POST | `/conversation/read` | `{peer_id}` | `{ok}` |
| POST | `/messages` | `{peer_id, text, client_id}` | `Message` (idempotent on `client_id`) |
| POST | `/messages/retry` | `{message_id}` | `Message` |
| POST | `/preflight` | `{peer_id, paths?: string[], upload_ids?: string[]}` | `Preflight` |
| POST | `/transfers` | `{peer_id, paths? , upload_ids?, note?, client_id}` | `Transfer` (idempotent on `client_id`) |
| PUT | `/uploads?name=&rel=&batch=` | raw request body (streamed) | `{upload_id, name, rel_path, size}` |
| GET | `/transfers?peer_id=&filter=all|active|done` | | `Transfer[]` |
| GET | `/transfers/{id}` | | `Transfer` with `files` |
| POST | `/transfers/{id}/pause` `/resume` `/cancel` `/retry` | | `Transfer` (idempotent) |
| POST | `/transfers/{id}/accept` | `{save_dir?, auto_accept?: boolean}` | `Transfer` |
| POST | `/transfers/{id}/reject` | | `Transfer` |
| POST | `/transfers/{id}/open` `/reveal` | | `{ok}` (completed incoming only) |
| GET/PUT | `/prefs` | `{receive_dir?, peers?: {peer_id: {auto_accept: boolean}}}` | `Prefs` |
| POST | `/peers/verify` | `{peer_id, accept: boolean}` | `PeerMeta` (nearby verification) |

`paths` are only accepted from the desktop runtime picker (absolute local paths
on this machine). The browser uses `/uploads` (streamed, never base64) and is
told that resume after browser restart needs re-selection.

### Types

```ts
type Bytes = number | string;            // decimal string above 2^53-1
type Route = "lan" | "public" | "relay";

type Capabilities = {
  direct_size_limit: null;               // no artificial limit on direct routes
  routes: Route[];                       // routes this build can actually use
  nat_traversal: boolean;                // ICE hole punching + QUIC (see Internet path)
  relay: { available: boolean; reason: string; max_bytes: Bytes | null; quota_bytes: Bytes | null };
  folders: true;
  path_send: boolean;                    // daemon accepts local paths (desktop)
  staged_upload: boolean;                // browser streaming upload
  receive_dir: string;
  receive_free_bytes: Bytes | null;
};

type PeerMeta = {
  peer_id: string;
  relationship: "own" | "friend" | "nearby" | "other";
  name: string;                          // advertised name (display only)
  online: boolean | null;                // null = not checked yet
  route: Route | null;                   // route a transfer would use now
  address: string;                       // technical detail, connection details only
  latency_ms: number | null;
  sendable: boolean;
  send_mode: "send" | "queue" | "verify" | "unavailable";
  reason: string;                        // why unavailable / queued (code)
  verification: { required: boolean; code: string | null; verified: boolean };
  auto_accept: boolean;
  unread: number;
  last: { kind: "text" | "link" | "system" | "transfer"; text: string; at: string } | null;
};

type Message = {
  message_id: string; peer_id: string; direction: "out" | "in";
  kind: "text" | "link" | "system"; text: string; created_at: string;
  status: "waiting_online" | "sending" | "delivered" | "failed";
  delivered_at: string | null; error: string; version: number;
};

type TransferStatus =
  | "connecting" | "waiting_online" | "awaiting_accept" | "transferring" | "paused"
  | "reconnecting" | "verifying" | "completed" | "insufficient_space" | "rejected"
  | "cancelled" | "source_changed" | "verify_failed" | "failed" | "relay_confirmation";

type Transfer = {
  transfer_id: string; offer_id: string; peer_id: string;
  direction: "out" | "in"; kind: "file" | "files" | "folder";
  name: string; file_count: number; total_bytes: Bytes; confirmed_bytes: Bytes;
  status: TransferStatus; route: Route | null;
  speed_bps: number | null; eta_s: number | null;
  error: { code: string; message: string; need_bytes?: Bytes; free_bytes?: Bytes } | null;
  note: string; save_dir: string; final_path: string;
  verification_code: string | null;      // nearby, unverified only
  actions: ("pause"|"resume"|"cancel"|"retry"|"accept"|"reject"|"open"|"reveal"|"send_again"|"reselect")[];
  created_at: string; updated_at: string; completed_at: string | null;
  version: number;                       // monotonic per transfer; never apply a lower version
  files_preview: { rel_path: string; size: Bytes }[];   // at most 50
};

type ConversationItem =
  | { type: "message"; at: string; message: Message }
  | { type: "transfer"; at: string; transfer: Transfer };

type Event =
  | { seq: number; type: "transfer"; transfer: Transfer }
  | { seq: number; type: "message"; message: Message }
  | { seq: number; type: "peer"; peer: PeerMeta }
  | { seq: number; type: "resync" };     // client must refetch /snapshot
```

Clients load `/snapshot`, then open `/events?since=<seq>`. Events with a
`version` lower than the stored one are ignored, so duplicate or out-of-order
events never roll back progress. Progress events are throttled server-side
(at most ~4/s per transfer).

### Status meanings and actions

| status | direction out | direction in |
| --- | --- | --- |
| connecting | cancel | — |
| waiting_online | retry, cancel | — |
| awaiting_accept | cancel | accept, reject |
| transferring | pause, cancel | cancel |
| paused | resume, cancel | — |
| reconnecting | retry, cancel | cancel |
| verifying | — | — |
| completed | — | open, reveal |
| insufficient_space | retry, cancel | accept (change location), reject |
| rejected / cancelled | send_again | — |
| source_changed | reselect | — |
| verify_failed | retry | — |
| failed | retry, cancel | — |
| relay_confirmation | cancel | — |

`completed` is only reported after the receiver re-read the committed file from
disk, matched every chunk hash and the sender's chunk-list root, and atomically
moved it to its final path.

## Peer protocol (`/api/peer/drop/*`)

Request bodies are `SignedPayload.to_dict()` envelopes signed by the sender
identity. The payload always contains `type`, `from`, `to`, `ts` and `nonce`;
receivers reject envelopes whose `to` is not themselves, whose signer is not
`from`, or whose timestamp is outside +-10 minutes.

| Path | Purpose |
| --- | --- |
| `POST /message` | sealed message; 200 only after the receiver persisted it (=delivered) |
| `POST /offer` | transfer offer: manifest, ephemeral key, note (sealed) |
| `POST /status` | sender polls decision, committed offsets, free space |
| `PUT /data/{transfer_id}/{file_id}` | encrypted frame stream starting at `offset` |
| `POST /complete` | sender's chunk-list root; receiver verifies from disk and commits |
| `POST /cancel` | either side cancels |

Data frames: `u32 length || ciphertext`. Plaintext is up to 1 MiB. Nonce is
`salt(4) || offset(8)` where `salt` is random per PUT request; associated data
binds transfer, file, offset and length. The receiver writes only at its
committed offset, hashes each 8 MiB chunk, fsyncs at chunk boundaries and
records the chunk hash. After a restart it truncates the `.part` file to the
last committed chunk.

## Internet path: ICE hole punching + QUIC (`rynmesh/drop/p2p_link.py`)

Used for own devices and friends when no HTTP endpoint answers (typically both
behind NAT). Nearby/unpaired devices never get this path.

1. **Signaling** reuses the registry work orders of private AI: the sender
   submits `drop.p2p_offer` (capability `rynmesh.drop.p2p.v1`) with its ICE
   offer; the receiver long-polls its open orders, checks the requester is an
   own device or friend, and answers with its ICE candidates and the SHA-256 of
   a per-session self-signed QUIC certificate. Orders and results are signed by
   the node identities, so the pinned fingerprint authenticates the receiver.
2. **Hole punching** is aioice with host/srflx/prflx candidates only (no TURN).
   STUN servers are queried in parallel and the first that answers is reused.
3. **QUIC** (aioquic, cubic congestion control) runs on the nominated UDP pair.
   Each stream is one peer request: `u32 len || {"m","p","h"} JSON || body`,
   answered with `u32 len || {"s","h"} JSON || body`. The receiver dispatches it
   into its own `/api/peer/drop/*` routes in-process; any other path is 404, and
   the ASGI client address is the real remote ICE address.
4. The probe endpoint is `ryn-p2p://<peer_id>`; `PeerMeta.address` shows
   `P2P · ip:port` and `route` is `lan` or `public` by that address.
5. Idle links send a QUIC PING every 15 s (NAT mapping, early loss detection)
   and close after 10 idle minutes. Failed dials back off 30 s → 10 min; a send
   may redial after 5 s.

When both devices have the native extension, the offer/answer also carry
`native` info and the whole data plane (hole punching + QUIC) runs in Rust; see
`docs/NATIVE_P2P_TRANSPORT.md`. Older devices keep the aioice + aioquic path.
`PeerMeta`/link details then report `native`, the congestion mode and RTT.

`Capabilities.nat_traversal` is `true` while this path is running. Disable it
with `RYNMESH_DROP_P2P=0`. `RYNMESH_P2P_STUN` takes a comma-separated list.
