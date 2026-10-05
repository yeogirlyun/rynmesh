# LAN discovery and preferred local AI connections

LAN-facing desktop nodes automatically query `239.255.87.91:48791` over IPv4
UDP multicast at startup, every 15 seconds, and on demand (at most once per
second). Multicast TTL is one hop. All usable IPv4 interfaces are queried; an
unsupported VPN adapter does not prevent other interfaces from working. Network
changes are detected at the next 15-second refresh. The responder advertises the
actual configured peer HTTP port, including non-default desktop ports.

When multicast does not produce a reply, a bounded background HTTP scan checks
the peer port on attached private `/24` ranges at startup and at most every two
minutes. Known signed endpoints are refreshed between full scans. The responding node signs a
fresh nonce and the exact probed address; the scanner verifies both before
using the endpoint. The scan runs without blocking the desktop API. The device
map receives these fresh LAN endpoints and shows them next to the current
device; a successful HTTP health probe marks the route as LAN direct.

An AI request in `auto` mode tries fresh discovered LAN addresses first, then its
existing advertised HTTP address, then the existing ICE/P2P path. Existing relay
configuration still controls any final relay fallback. `direct` mode tries the
two HTTP paths only; explicit `p2p` or `relay` modes do not attempt LAN HTTP.
Each HTTP connection attempt retains its three-second connect timeout. A cold
LAN lookup waits at most 400 ms, and ordinary requests reuse discovered addresses.
Duplicate addresses are skipped. A partial stream is never automatically replayed.

Successful LAN requests retain `transport=peer_http_direct` and add
`transport_evidence.route=lan` and `discovery_source=lan`. Only a completed,
authenticated HTTP request produces this evidence. LAN announcements alone are
not proof that the peer HTTP port is reachable.

Discovery replies are signed by the existing node identity, bound to a fresh
random query nonce, restricted to attached subnets, and checked against the UDP
source IP. They expire after 45 seconds and are never persisted as registry
records. When `RYNMESH_NETWORK_KEY` is set, queries and replies also require its
HMAC; the key is never sent. The same `RYNMESH_NETWORK_ID` is required. Discovery
does not add trusted peers, space members, or resource permissions. Existing
encrypted requests, signatures, membership checks and task deduplication remain.

`discover_peers` includes LAN-only peers and adds `lan_endpoints` to matching
registry/cache records, without replacing their signed metadata or endpoints.
The desktop initially reads cached and LAN-discovered peers without waiting for
the registry, then refreshes the full list in the background. During the first
30 seconds it also checks the local snapshot every two seconds so a peer found
after the window opens appears without leaving and reopening Home.
The AI transport uses this LAN cache directly before resolving a fallback HTTP
address. Personal-space requests now send the existing signed and encrypted
work order directly to a discovered coordinator on the LAN, and verify its
signed response. If that attempt fails, the existing registry mailbox retries
the request. AI service publication/discovery still uses the registry; this
does not implement a fully offline mesh.

Set `RYNMESH_LAN_DISCOVERY=0` to disable discovery. Loopback-only and IPv6-only
listeners do not advertise IPv4 endpoints. Both computers must run a build
containing this feature. Allow inbound UDP 48791 and the configured peer HTTP
port on the private network. Guest Wi-Fi/client isolation or blocked multicast
can prevent multicast discovery; the signed HTTP scan can still find peers
when their HTTP port is reachable. IPv6 mDNS is not implemented.

Validation includes protocol tampering, source-address and network-key checks,
expiry, registry merging/outage, lifecycle cleanup, LAN-first HTTP, HTTP fallback,
strict P2P and unchanged space permissions. On 2026-09-24 the packaged Windows
node passed a two-process multicast smoke test without a registry. The installed
Windows desktop (`172.16.8.191`) and installed Apple Silicon Mac desktop
(`172.16.8.117`) then exchanged signed LAN discovery replies and reached each
other's peer HTTP health endpoint. The Mac app reported its discovery worker
running with one peer and no error. A live cross-device AI inference request was
left to the user's functional test.

The Mac packaging script refreshes source timestamps before the setuptools
build and compares every installed Python module with the source before freezing.
This prevents an older `build/lib` copy from entering a new desktop package.
