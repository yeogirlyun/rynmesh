# Issues 20, 23, 24, 25, 28, and 30 integration acceptance

Date: 2026-09-03

Branch: `codex/integration-issues-20-23-24-25-30`

Validated HEAD: `9c1c6fe`

## Decision

The combined branch is **locally accepted for integration**. Issue 20, 23, 24, 25,
28, and 30 changes coexist without a known feature regression in the available
Windows development environment.

Release acceptance remains **pending external evidence**. The remaining gates need
remote CI or hardware that is not available on this host; they are not unresolved
implementation defects found by the local integration suite.

## Included changes

- Issue 28: authenticated Transport POST and error hardening.
- Issue 24: Private AI provider switching, per-provider conversation buckets, and
  storage-failure recovery.
- Issue 25: grounded Ask handoff, bounded context, draft restoration, and stable
  conversation ordering when two conversations are created in the same millisecond.
- Issue 23: encrypted streaming, SSE delivery, Stop, sequence recovery, snapshot
  replacement, poll fallback, and terminal-only assistant persistence.
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

## Exact local evidence

### Feature and cross-feature suites

- Backend friends/Transport/LLM focused suite: `102 passed`.
- Web full suite: `17 files`, `77 tests passed`.
- Web TypeScript lint: passed.
- Web production build: passed (`1775 modules`).
- Ruff: passed.
- `npm audit`: `0 vulnerabilities`.
- `cargo metadata --locked --no-deps`: passed.
- Four Linux shell scripts: `bash -n` passed and Git mode remained `100755`.
- CI and release workflow YAML parsing: passed.
- `git diff --check`: passed.

### Full backend suite

With Python 3.12 and UTF-8 mode, the full suite reached `568 passed, 3 skipped,
8 failed`. One failure was a pre-existing Windows file-replacement race in the
Signal50 media-ops test and passed immediately when rerun alone. The seven stable
failures are Windows/platform limitations outside the selected feature paths:

- two POSIX executable-bit assertions on an NTFS checkout;
- one shell syntax test selecting a broken local WSL `bash.exe`;
- three POSIX `0600` mode assertions on Windows;
- one `select()`-on-pipe MCP smoke test unsupported by Windows.

No Issue 20, 23, 24, 25, 28, or 30 feature assertion failed.

## External acceptance gates

The following evidence is still required before release acceptance:

- run the exact branch in repository CI after it is pushed;
- build, install, launch, and remove the Issue 20 `.deb` on Ubuntu 24.04 x86_64;
- verify installed-app QR/deep-link behavior on Windows, Linux, and macOS;
- run Issue 23 streaming and Issue 30 join/revoke convergence across two physical
  nodes, covering direct, fallback, and relay paths;
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

