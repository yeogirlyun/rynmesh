# Issues 20, 23, 24, 25, 28, and 30 integration acceptance

Date: 2026-09-03

Branch: `codex/integration-issues-20-23-24-25-30`

Validated implementation/evidence HEAD: `254f550`

## Decision

The combined branch is **locally accepted for integration**. Issue 20, 23, 24, 25,
28, and 30 changes coexist without a known feature regression in the available
Windows development environment. Issue 24 and Issue 25 meet their local acceptance
standards. Issue 20, Issue 23, Issue 28, and Issue 30 remain explicitly unaccepted
until their external gates below have primary evidence.

Release acceptance remains **pending external evidence**. The remaining gates need
remote CI or hardware that is not available on this host; they are not unresolved
implementation defects found by the local integration suite.

## Included changes

- Issue 28: authenticated Transport POST and error hardening.
- Issue 24: Private AI provider switching, per-provider conversation buckets, and
  storage-failure recovery.
- Issue 25: grounded Ask handoff, bounded context, draft restoration, and stable
  conversation ordering when two conversations are created in the same millisecond;
  a reproducible local-Consumer browser fixture and sanitized evidence bundle are
  included.
- Issue 23: encrypted streaming, SSE delivery, Stop, sequence recovery, snapshot
  replacement, poll fallback, terminal-only assistant persistence, and a two-node
  `stream-run` verifier that records first-delta/terminal timing and exactly-once
  ledger evidence.
- Issue 30: Friend Mesh invite/join/review/revoke flows, endpoint pinning, friend
  credentials, friends-only Private AI access, privacy erasure, and desktop deep links.
- Issue 20: Linux desktop process management, XDG paths, `.deb` packaging checks,
  install smoke workflow, and managed-node shutdown.

## Integration defects found and resolved

1. Grounded Ask and an initial empty conversation could share the same millisecond
   sort key after provider switching. The grounded conversation now receives a
   strictly newer timestamp.
2. Friend Mesh passed an empty `headers` argument to clients that predated header
   support, which could prevent streaming settlement delivery. Empty headers are no
   longer passed.
3. The friends-only policy originally authenticated the complete-response task path
   but not Issue 23's streaming task path. Streaming requests now carry a friend HMAC
   bound to the exact `/api/peer/llm/tasks/stream` path and request body, and the
   provider validates it before inference or capacity work.
4. The Tauri merge preserves both desktop requirements: single-instance is registered
   before deep-link handling, while Linux SIGTERM/SIGINT and normal exit all stop the
   managed node.
5. Provider discovery refresh failures now retain the last successful snapshot, and a
   first encrypted write failure in an empty target bucket releases switching without
   losing the original provider, history, or draft.
6. A grounded context too large for the selected model previously disabled Send without
   an explanation. The UI now tells the user to remove the article or select a model
   with a larger context window.
7. A proxy-specific endpoint-pinning refusal previously escaped Friend Join as an HTTP
   500. It now fails closed as `friend_join_failed` before an outbound client is built,
   a local relationship is stored, or the remote invite is consumed.

## Exact local evidence

### Feature and cross-feature suites

- Combined backend Friend/Transport/LLM/Linux-contract/stream verifier suite:
  `132 passed, 1 skipped`.
- Friend authorization across complete and streaming HTTP routes: included in the
  combined suite; cross-route replay and post-revocation use are rejected before the
  LLM service entry point.
- Web full suite: `17 files`, `85 tests passed`.
- Private AI combined focused suite: `22 passed`.
- Issue 25 browser-focused suite after evidence integration: `40 passed`.
- Web TypeScript lint: passed.
- Web production build: passed (`1775 modules`).
- Ruff: passed.
- `npm audit`: `0 vulnerabilities`.
- `cargo metadata --locked --no-deps`: passed.
- Four Linux shell scripts: `bash -n` passed and Git mode remained `100755`.
- CI and release workflow YAML parsing: passed.
- `git diff --check`: passed.
- Issue 25 browser evidence: four screenshots, three parseable JSON records, zero
  console warnings/errors, sanitized request hashes/paths, and a final URL containing
  only `peer`, `service`, and `network` parameters.

### Full backend suite

With Python 3.12 and UTF-8 mode on implementation/evidence HEAD `254f550`, the full
suite reached `589 passed, 3 skipped, 8 failed`. The failures are Windows/platform
limitations outside the selected feature paths:

- two POSIX executable-bit assertions on an NTFS checkout;
- one shell syntax test selecting a broken local WSL `bash.exe`;
- three POSIX `0600` mode assertions on Windows;
- one pre-existing Windows `os.replace` reader race in Signal50 media-ops; and
- one `select()`-on-pipe MCP smoke test unsupported by Windows.

No Issue 20, 23, 24, 25, 28, or 30 feature assertion failed.

## External acceptance gates

The following evidence is still required before release acceptance:

- run the exact branch in repository CI after it is pushed;
- run Issue 28 direct and encrypted-Relay E2E plus packaged-node and both desktop
  architecture jobs on the exact reviewed hardening commit;
- build, install, launch, and remove the Issue 20 `.deb` on Ubuntu 24.04 x86_64;
- verify installed-app QR/deep-link behavior on Windows, Linux, and macOS;
- run Issue 23 streaming and Issue 30 join/revoke convergence across two physical
  nodes, covering direct, fallback, and relay paths;
- record the validated DNS answer, connected socket peer, and preserved TLS SNI/Host
  for Friend Join, and make an explicit V1 product decision for outbound proxies;
- complete maintainer protocol/security review for the Friend Mesh authentication and
  revocation design;
- produce tagged release artifacts and checksums through the release workflow.

## Local toolchain limitation

`cargo check --locked` resolves dependencies and starts compilation, then stops because
the host does not have the MSVC linker:

```text
error: linker `link.exe` not found
the msvc targets depend on the msvc linker but `link.exe` was not found
```

Installing Visual Studio Build Tools with the Visual C++ workload will remove this
local compile gate. It does not replace the required Linux and macOS CI evidence.
