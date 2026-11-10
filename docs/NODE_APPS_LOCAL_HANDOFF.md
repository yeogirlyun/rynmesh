# Rynmesh node apps — local pilot handoff, 2026-10-12

This work was explicitly authorized by the owner: review Ziling's update and local Clawpad, migrate useful apps into Rynmesh or an individual node such as MS-2. All coding, testing and packaging ran locally. No cloud coding task, recurring automation or monitoring was created.

## Exact source and status

- Active local repository: `/Users/yeogirlyun/.codex/worktrees/clawpad-node-apps/rynmesh`, branch `codex/clawpad-node-apps`.
- Installed implementation: `916724512c02994453b807a36eb0b5cd192fcea0`; later commits contain documentation/evidence only.
- Budget fix: `a4ae97f`. This branch depends on open PR #103 head `c55b9df3dd0a361e2c7a4444a86ba49cac3e47ef`.
- Primary `/Volumes/ExternalSSD/rynmesh` remains clean at `9db06f436e64d87c7060db0934392768c42c8187`. No current change is merged or pushed.
- Review-only checkout: `/Users/yeogirlyun/.codex/worktrees/ziling-pr103-review/rynmesh`.
- Clawpad `/Volumes/ExternalSSD/clawpad` was reviewed at `1bf21d840`; its existing uncommitted work was preserved.
- Installed wheel: `rynmesh-0.7.0-py3-none-any.whl`, SHA256 `f75a65f3662da682030b1e07d39515090069c449aa0eb071d9a8de43b70093aa`. This is a local build, not a published release.

## Using the apps

On MS-1, open `http://127.0.0.1:8796/apps`. Owner browser operations must follow the display rules: Dev Display for this non-account app; never open a browser on the main display. Select MS-2 Apps as provider, then Resume, Recruit or Desk. Each starts empty. Review the exact action before confirming it. Resume supports sections, experience, cover letters and Markdown export; Recruit supports jobs and candidate stage progression; Desk supports requests, comments and resolution. A lost reply appears as a pending original operation; use explicit Resume to recover the same operation without duplication.

Both nodes have separate pilot identities, paired through the existing authenticated friend protocol. MS-2 shares all three apps only with the new MS-1 identity. It does not share the provider's local workspace. Hosted records are processed by MS-2, whose operator possesses the storage keys. These are private workspaces, not public job advertisements, consultant discovery or marketplace listings.

The standalone hosts expose app UI, owner-gated app/friend APIs and authenticated peer app transport. They start no model, registry, assistant, Exchange or polling worker. The assistant tools are implemented and tested in the integrated Rynmesh source, but are not running on these standalone hosts. Usage and platform commission are zero. A Recruit budget records a Ryncoin quote; it does not transfer coins. Paid transactions still require the existing full-node Exchange workflow.

## Physical deployment

| Host | Separate installation root | Observed endpoint | Initial app PID |
| --- | --- | --- | --- |
| MS-1.local | `/Users/yeogirlyun/Library/Application Support/RynmeshApps/pilot-20261012/ms1` | `http://172.30.1.5:8796` | 54554 |
| MS-2.local | `/Users/yeogirlyun/Library/Application Support/RynmeshApps/pilot-20261012/ms2` | `http://172.30.1.9:8795` | 14545 |

Each root contains its own `venv`, `node` home, installed wheel, `source-manifest.json`, `service.json` and `service.log`. All 250 installed Python module hashes matched the source manifest before startup. Addresses and PIDs are observations, not permanent identifiers: inspect the current process command and listener before any lifecycle action. Use the owner's canonical `MS-2.local` SSH configuration; do not change its pinned host/direct-link setup.

MS-1's localhost UI is the intended client. Direct remote owner API access requires its bearer token or an authenticated session; opening the MS-2 URL remotely alone is insufficient. There is no new remote browser login UI. Manage MS-2 via canonical SSH and its localhost API, or use the browser locally on that host following its display policy. Owner tokens remain in the private node home and must never be printed, copied into reports or placed in URLs. Remote peer app access uses the paired identity and explicit app grants, not owner tokens.

No ordinary `.rynmesh` home, existing app, credentials, firewall or launch settings were changed. No launchd, cron or automatic restart is installed. If a pilot process exits or the machine restarts, the apps will be unavailable until a deliberate local lifecycle action. Do not run the launcher again blindly: it rejects an existing deployment record and port. Preserve the original node home, identities, encrypted intents and receipts when recovering; resetting them would break pending-operation guarantees. Do not alter existing application/trading services, create recurring checks or enable autostart without explicit owner instruction.

## Verification and remaining review

Final focused regression suite: 573 backend tests passed; frontend: 664 tests passed plus TypeScript/Vite build on Node 22.23.2. Real two-node headless browser workflows passed. Physical MS-1/MS-2 checks passed for pairing, sharing, all three business flows and exact retry without duplicate writes. The installed MS-1 browser selected the real MS-2 provider and showed all three empty owner workspaces. No global UI input, model calls or coin ledger were used. Acceptance tests used a separate temporary identity; it was revoked and its listener stopped. Its encrypted provider records remain as isolated audit evidence.

The wider Mac backend run was not green and did not complete: 48 failed, 670 passed, 21 skipped before stopping. The first fixture's timeout-versus-refused issue reproduces on untouched #103; remaining failures require individual triage. The Ziling review also calls for an origin/license inventory and corrections to merged-versus-proposed feature claims before upstream approval. Read [the review](reviews/ZILING_UPDATE_2026-10-12.md) and [acceptance evidence](acceptance/node-apps-20261012/README.md). This is not a full backend, independent security or live model-provider acceptance.

Pilot limits: 128 KiB per workspace, 64 entries in creation collections/comments, 96 workspaces/grants, and 128 durable operations/intents per workspace/consumer store. There is no automatic archive/rotation or public discovery. Preserve pending original operations; an unavailable provider can leave their outcome unknown.

Future work must continue in the exact local repository above, using this Markdown handoff. Do not delegate to cloud workspaces or run automated follow-ups.
