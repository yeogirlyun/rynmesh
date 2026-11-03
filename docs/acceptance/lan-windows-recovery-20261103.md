# LAN Windows recovery — 2026-11-03

## Observed failures

- LAN peer health was reachable, but the public registry timed out on both devices. The consumer service catalog returned no services. Existing space membership and sharing configuration were valid.
- The complete installer had been run through an administrator SSH session. Staged company and office plugin directories had administrator/OWNER RIGHTS ACLs without access for the ordinary desktop user. Company startup reported `plugin_service_files_missing`; office catalog returned 503.

## Recovery

- Backend commit `9b57a7340ea0a4468ac3614916b33fc0108144f5`, integrated into `develop`, discovers already-permitted space members through existing LAN endpoints. The ordinary signed/encrypted space catalog response remains authoritative. Removed and unauthorized members are excluded. Retries are bounded and rotate across peers.
- Service listing uses passive registry refresh so an unavailable public registry cannot block the display path.
- Granted only the existing desktop user access to the installed Ryn `plugins` and `plugin-services` trees. Original ACL exports were retained in that user's Downloads directory. No account, space, sharing policy, or global network configuration was changed.
- Replaced only the target Windows installation's `rynmesh-peer.exe`. Previous binary retained under `rollback/before-lan-recovery-20261103`. Original full installer was not rebuilt or overwritten.
- Installed sidecar SHA-256: `045694F55FA2138D364D5A976484AE9EE91A3E107765AC94E05DE37538C1C253`.

## Verification

- 18 tests passed: LAN catalog recovery, real signed space bindings, and peer health responsiveness.
- Frozen isolated sidecar health passed; service listing responded in 29 ms.
- Target discovered four verified, ready services: GLM, DeepSeek, Antigravity, Codex CLI. Desktop service page showed four ready services.
- A minimal DeepSeek request from the target succeeded over `peer_http_direct`, returning `OK`. This verifies one real inference path; other models were discovered but not invoked.
- Four office plugins returned enabled with local editing support. Company health returned ready; all three company application routes returned HTTP 200.
- Temporary launch task removed after verification. Ryn remains running in the existing desktop session.

Public registry reachability itself was not repaired. Cross-network discovery still depends on its available transports. Original full-package installer ACL behavior remains a separate packaging follow-up; this recovery corrected the existing target installation.
