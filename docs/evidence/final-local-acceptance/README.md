# Final local acceptance screenshots

Generated: 2026-09-03

Integration branch: `codex/integration-issues-20-23-24-25-30`

Evidence/documentation HEAD before these screenshots: `72e4be6b4238c684b0e1482c010f1341df880475`

Validated implementation HEAD: `4bd2b8b56df6f69bd4aac2b995245b1205e98e0a`

These images are annotated evidence summaries. They do not replace the underlying
machine-readable records or original browser captures referenced below.

| Issue | Annotated screenshot | Primary source | SHA-256 |
| --- | --- | --- | --- |
| #30 | `issue-30-friend-mesh-accepted.png` | `../issue30-integration-two-node-e2e.json` | `ed4d6f4132aef8057bd5b05c91bc246fe6a3119ea049bd928d1b675e39de12e0` |
| #25 | `issue-25-ask-about-item-accepted.png` | `../issue-25/02-grounded-truncation.png`, `../issue-25/browser-session.json`, `../issue-25/request-evidence.json` | `099421f063d9d7f76d40fc7dbe110fbde5fb425a4bdf83703ed29be809b605cf` |
| #24 | `issue-24-provider-switching-accepted.png` | `../issue-24/provider-comparison.png`, `../../ISSUE_24_ACCEPTANCE.md` | `7ee3d2637cde77753e60071fd0754ec60a21dfc09342bd3b52f8fd7df6623355` |
| #23 | `issue-23-streaming-accepted.png` | `../../../deploy/llm-e2e/results/local-stream-result.json`, `local-fallback-result.json`, `../../ISSUE_23_ACCEPTANCE.md` | `30cc3e5f1d00f675fd340a9d1b255a5ef13af826cf627732ea447c850f4c36ca` |
| #20 | `issue-20-linux-desktop-accepted.png` | fresh local command results plus `../../ISSUE_20_ACCEPTANCE.md` | `b7e80c85cd89568eb91a2a5796e8984fe2b3ce96c61727fb99412adf061be9a0` |

## Verification represented in the images

- #30: two independent node processes, real HTTP Registry/peer transport,
  authorized Friend `stream-v1`, online revoke, offline restart/retry convergence,
  denial before inference, zero invite/link secret occurrences, and process cleanup.
- #25: real browser article handoff, visible truncation, local-Consumer-only route,
  successful grounded response, context removal, and zero console warnings/errors.
- #24: visible Provider/model comparison, compound service identity, isolated
  conversation history, and refresh/encrypted-storage recovery tests.
- #23: four real local HTTP processes, first delta before terminal, capability
  fallback, exactly-once accounting, persistent-file privacy scan, and cleanup.
- #20: eight Linux desktop artifact contract tests, four shell syntax checks,
  workflow YAML parsing, Web tests/typecheck/build, packaging/architecture contracts,
  and preserved macOS verification wiring. CI execution and Release publication are
  intentionally identified as release-stage evidence rather than local development
  acceptance.

`proof.html` is the local, reproducible rendering source used to produce the five
annotated PNG screenshots. It references the committed original browser screenshots
for #24 and #25 and contains only sanitized evidence values for #20, #23, and #30.
