# Friend mesh connection diagnostics (#63)

## Contract

Friends → Connection checks probes one to five selected active friendships from
this node. A fresh challenge and signed response authenticate each directed link.
The check sends no message, content card, prompt, or service order. It neither
grants AI access nor infers connectivity between other friends. Queued-delivery
retry is a separate explicit action using the existing delivery mechanism.

The peer POST uses existing friendship authentication and replay checks, a 2 KiB
request limit and a 4 KiB response limit. Local selection requests are owner-only,
limited to 1 KiB, and results use no-store. At most five workers and one batch run
per node. The default HTTP timeout is five seconds; a seven-second batch deadline
returns a bounded result. A transport exceeding its deadline reserves that batch
until its workers finish, rather than accumulating retries. Results exist only in
memory, become stale after 60 seconds, expire after five minutes, and are invalidated
by endpoint or relationship changes. Restart requires a new check.

Errors report observed categories and recovery steps, never raw transport exception
text. A timeout cannot diagnose NAT or firewall configuration. A failed direct check
does not establish that mailbox delivery is unavailable. Old nodes return an
unsupported-check result; this is not a conclusion about ordinary messaging.

## Automated evidence (2026-09-17)

- Backend: 1563 passed, 29 skipped. Eight new tests cover authenticated checks,
  challenge replay, wrong identity, owner authorization, revoked relationships,
  unsafe endpoints, limits, parallel execution, timeout reservation and safe errors.
- Real loopback HTTP: fresh three-node and five-node meshes check every directed
  link. Stopping one node isolates its failures; restarting the same node restores
  the links without new pairing. Message histories stay empty.
- Frontend on Node 22: 327 passed; final layout adjustment retested all four new
  component tests. Typecheck/build and Ruff passed.
- Built and installed wheel served its own UI. Browser verified three reachable
  peers and one stopped peer, then used Recheck after restart to verify recovery.
  The final compiled layout was copied into that installed package and rechecked.
  [Recovery screenshot](recovered.png) contains synthetic node identities only.

## Repeatable manual acceptance

1. Start three, then five fresh packaged nodes and pair them using the ordinary
   invitation workflow. Keep temporary fixture identities separate from real ones.
2. Open Friends on each node, select its active friends, and check connections.
   Record direction, safe state code and latency only. Expect verified direct links.
3. Stop one node. Repeat checks elsewhere. Confirm only links to the stopped node
   fail and that each failure has recovery guidance; no conversation is created.
4. Restart it at the same advertised address. Recheck that friend and confirm a
   verified link without re-pairing. A stale result is labeled until checked again.
5. Revoke a friendship, refresh Friends and confirm it is absent from diagnostics.
   Restart the checking node and confirm results begin unchecked.
6. Repeat across distinct public networks before claiming a physical mesh gate.

The recorded run uses real TCP sockets on one Windows host. It does not claim
distinct-public-egress, physical NAT traversal, relay diagnosis, model availability,
or a completed P2 milestone gate.
