# Personal-space connections — Windows P2P test build

Branch: `development/personal-first-implementation`.

This change connects the existing ICE/STUN implementation to the personal AI
chat's `auto` route. It does not introduce or deploy a relay server.

## What happens when you send a message

1. Look up the selected provider by its device identity. LAN-facing nodes now
   perform signed IPv4 multicast discovery; see [LAN discovery](LAN_DISCOVERY.md).
2. Try fresh discovered LAN HTTP addresses first, then the provider's original
   advertised endpoint. Each socket connection has a 3-second budget; model inference
   retains its own, longer timeout. Direct peer requests bypass HTTP proxies and
   do not follow redirects. Requests and responses remain signed and encrypted.
3. If the direct attempt fails, collect fresh ICE candidates and exchange them
   through the existing signed Registry mailbox. Attempt a direct UDP connection.
   Gathering, signaling and connection checks share a 20-second setup budget,
   configurable with `RYNMESH_P2P_CONNECT_TIMEOUT_S` (capped at 60 seconds).
4. The model runs on the selected provider. The same signed task ID is used across
   attempts so a lost HTTP reply does not cause duplicate inference or settlement.
5. Record the actual route and failure codes in local task history. The chat shows
   connection progress and labels completed replies as Direct or Peer-to-peer.

The default Windows package does not configure an AI relay. Failed hole punching
therefore produces a visible error. Existing explicit operator relay settings and
the legacy relay transport remain available; they were not deployed or enabled by
this work. Explicit `direct` and `p2p` modes retain their restrictions.

An interrupted stream that has already delivered content is not automatically
replayed. Network changes are handled by discovering fresh addresses on the next
request; resuming a partially generated answer is not implemented.

## Moving between networks

Windows-generated endpoints are refreshed from the current network interface on
registration, roughly every 30 seconds. Consumer nodes and paused providers also
refresh registration and retry after an offline startup. Configured fixed endpoints
are preserved. Each new ICE session gathers current interfaces and NAT mappings.
Device keys and personal-space membership remain unchanged.

The desktop sets `RYNMESH_AUTO_PEER_ENDPOINT=1` when it generated the endpoint.
An explicit `RYNMESH_PEER_ENDPOINT` or `RYNMESH_PEER_PUBLIC_HOST` disables that
default. Operators can explicitly set `RYNMESH_AUTO_PEER_ENDPOINT=0`.

## Three-computer field acceptance

1. Install the **personal-space-p2p** Windows build on all three computers.
2. Keep the home computer and laptop on one network. Put the third computer on
   a different internet connection (another broadband connection or phone hotspot).
3. Use the same reachable Registry and compatible network-key configuration.
   Create a space on the home computer and invite the other two computers.
4. Connect and publish the home computer's model service. Set its personal-space
   AI access to **My space devices**. Keep Ryn and the model running.
5. Send a message from the laptop. Confirm the answer is from the selected home
   device and note the Direct / Peer-to-peer label.
6. Send a message from the other network. Inspect the returned task's
   `transport_evidence` if distinguishing a public NAT path from a LAN path is
   necessary. A Peer-to-peer label alone does not prove public NAT traversal.
7. Change a device's network, allow discovery to refresh, and send a new message.
   Confirm that no new invitation is needed.
8. Block UDP in a controlled test and verify a connection error, no relay use and
   release of the task's reserved balance. Restore UDP before retrying.
9. Remove the third computer from the space and verify the provider rejects new
   requests after the membership change is applied.

Normal mixed-network use must not enable `RYNMESH_P2P_REQUIRE_PUBLIC` or
`RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC`: these are strict public-network acceptance
settings that deliberately exclude LAN paths. The default STUN server remains
`stun.l.google.com:19302`; operators can override `RYNMESH_P2P_STUN=host:port`.
The default service must be reachable over UDP for a public mapping to be obtained.

## Evidence and remaining limits

- 134 focused backend tests passed, including 9 new connectivity tests. Separate
  registration/discovery checks cover preservation of explicit IP configuration.
- 53 frontend tests passed; TypeScript/Vite and Windows NSIS builds passed.
- Three actual frozen Windows node processes, an isolated HTTP Registry, and a
  deterministic test model completed space creation/joining, two automatic HTTP
  failure → real ICE/UDP requests, and a restored HTTP direct request. Exactly
  three model executions were observed, without relay use.
- Tests also cover lost replies without duplicate inference/settlement, denial of
  removed members over P2P, UDP setup timeout, connection cleanup, fresh endpoint
  selection, and refusal to replay partially delivered streams.
- The development machine obtained a server-reflexive candidate from the default
  public STUN service. This proves mapping discovery, not remote connectivity.
- Historical public Windows ↔ V100 ICE/UDP evidence exists at
  `artifacts/gpu-provider-acceptance/p2p-final-result.json` (2026-09-07). That is
  evidence for the earlier transport, not acceptance of this new desktop build.
- New-build testing across two physical internet connections is still pending.
  Restrictive NAT or blocked UDP can prevent direct connections. No relay fallback
  is promised by the default package. File sharing, RDP and arbitrary LAN access
  are outside this implementation.
