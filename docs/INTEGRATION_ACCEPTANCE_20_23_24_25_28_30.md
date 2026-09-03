# Issues 30, 25, 24, 23, and 20 local integration acceptance

Date: 2026-09-03

Branch: `codex/integration-issues-20-23-24-25-30`

## Decision

Issues 30, 25, 24, and 23 are **accepted for local source-build development**.
Issue 20 is **development complete and locally contract-tested**; its two explicitly
remote release criteria (Linux CI lifecycle verification and publishing the `.deb`
plus checksum through the release workflow) remain pending release evidence.

Remote Git, a push, or a pull request is not required for the functional acceptance
recorded here. The remaining remote items are release verification, not known product
implementation defects and not blockers for continuing local development.

Issue 28 is included only because its authenticated Transport changes are a prerequisite
for Issues 23 and 30. It was rechecked with the combined feature suite.

## Included changes

- Issue 30: Friend Mesh invite review/join/revoke, endpoint pinning, friend credentials,
  friends-only Private AI access, privacy erasure, and desktop deep-link handling.
- Issue 25: grounded Ask handoff, bounded and visibly truncated context, context removal,
  draft restoration, and stable conversation ordering.
- Issue 24: provider/model switching inside a conversation, visibly separated provider
  histories, discovery refresh recovery, and encrypted-storage failure recovery.
- Issue 23: encrypted direct streaming, SSE delivery, Stop, sequence recovery, snapshot
  replacement, capability fallback, poll fallback, terminal-only persistence, and
  exactly-once settlement.
- Issue 20: Linux desktop process management, XDG paths, `.deb` packaging contracts,
  install smoke workflow, documentation, and managed-node shutdown.
- Issue 28 prerequisite: authenticated Transport POST and error hardening.

## Real local end-to-end evidence

### Issue 23: four-process Private AI

`python scripts/llm_e2e.py local-run` starts an isolated Registry, adapter, Provider,
and Consumer on dynamic loopback ports. On the combined branch it passed both profiles:

- direct `stream-v1`: 3 delta events, first delta at 141 ms, terminal state at 907 ms;
- capability fallback: Provider advertised only `complete-v1`, effective mode was
  `complete-v1`, and no delta was emitted;
- duplicate submission reused the same terminal task;
- Consumer and Provider each persisted one task, with one hold, one settlement, and
  one earning;
- all four processes stopped;
- prompt/output markers were absent from process logs and all 20 scanned persistent
  files in each profile.

The run also exposed and fixed a real incremental-transport defect: `read(64 KiB)`
could wait for EOF on a small live stream. Stdlib and fronted transports now use
incremental `read1`, with delayed real-HTTP regression coverage.

Dedicated-Relay and distinct-public-network strict-P2P routes were not available on
this host and are not represented as having passed.

### Issue 30: two real node processes

`python scripts/issue30_two_node_e2e.py` starts two nodes with independent homes,
identities, ports, and a real local HTTP Registry. The exact combined code HEAD passed:

- offline invite review did not consume the invite;
- join produced active relationships on both nodes;
- the invited friend discovered and completed the published friends-only Private AI
  service over direct peer HTTP;
- online revoke converged on both nodes and the next order was rejected before model
  inference;
- after rejoining, stopping node B, revoking on node A, and restarting node B with the
  same home, retry delivery converged to revoked and the next order was again rejected
  before inference;
- DNS/literal resolution, the reviewed endpoint, and the connected socket address were
  checked and recorded in sanitized form;
- invite-link/secret/privacy-export scans found zero sensitive occurrences, relationship
  keys were erased after convergence, and all child processes stopped.

The canonical #30 harness was stable for 10 consecutive runs after waiting for the real
Registry capacity refresh. On the integration branch, the Friend HMAC is additionally
bound to the exact `/api/peer/llm/tasks/stream` route and body; focused tests verify that
cross-route replay and post-revocation stream access are rejected before LLM dispatch.

This proves source-build behavior on one host using its private-LAN interface. Installed
desktop QR/deep-link dispatch remains release QA, not a failed local feature test.

### Issue 25: browser acceptance

A real local Consumer and Vite app were driven through Reader -> Ask -> visible context
truncation -> grounded response -> Remove context. Four screenshots and sanitized JSON
records are under `docs/evidence/issue-25/`. The console contained zero warnings/errors,
request evidence stored hashes rather than prompt bodies, and the final URL retained only
peer/service/network routing fields.

## Combined regression results

- Selected combined backend suite: `177 passed, 1 skipped`.
- Issue 23 focused suite before integration: `104 passed`; after integration its focused
  Transport/stream/LLM suite: `107 passed`.
- Issue 30 canonical focused suite: `49 passed`; expanded Friend/Transport/LLM suite:
  `101 passed, 1 skipped`.
- Web full suite: `17 files`, `85 passed`.
- Web TypeScript lint: passed.
- Web production build: passed (`1775 modules`).
- Ruff on the changed Issue 23 and Issue 30 files: passed.
- Issue 23 CI YAML: parsed successfully.
- Issue 30 harness: Ruff and `py_compile` passed.

The full repository suite is also run as a portability diagnostic. Platform-specific
failures from the Windows checkout are listed separately and do not overlap the selected
Issue 20/23/24/25/30 feature assertions.

With Python 3.12 UTF-8 mode, that diagnostic reached `599 passed, 3 skipped, 7 failed`.
The seven failures are Windows/POSIX incompatibilities in pre-existing tests: two NTFS
executable-bit assertions, one unavailable WSL `/bin/bash`, three POSIX `0600` mode
assertions, and one `select()`-on-pipe MCP smoke test unsupported on Windows. None is a
selected feature failure. Without UTF-8 mode, Windows' GBK locale adds decoding failures
for UTF-8 deployment examples, so UTF-8 mode is the meaningful portable result.

## Issue 20 acceptance boundary

The original Issue 20 criteria split naturally into development and release evidence:

- locally passed: selected/documented Linux format, bundled sidecar/webapp with no
  system Python/Node dependency, sidecar/package architecture contract, Linux install/
  update/requirements/limitations documentation, and preservation of macOS verification;
- release-only pending: run startup/health/UI/shutdown/restart in repository Linux CI,
  then publish the `.deb` and checksum through the existing release workflow.

No remote operation has been performed. The pending release checks should be executed
only when the branch is intentionally pushed for release verification.

## Non-blocking release follow-up

- Issue 20 Linux CI lifecycle job and release artifact/checksum publication.
- Installed-package deep-link/QR dispatch smoke tests for Issue 30.
- Dedicated Relay and distinct-network strict-P2P coverage for Issue 23 when that
  infrastructure is available.

These are release/route coverage items. They do not invalidate the local development
acceptance above.
