# NAS recovery without losing healthy settings

Date: 2026-11-15 (America/New_York). Incorporates the isolated NAS recovery work from Ziling source commit `5474b11d`, with additional transient-I/O corrections from this review. No deployment or production NAS access.

Malformed encrypted state, invalid JSON/key material and missing decryption keys are quarantined with collision-free backup names. Valid existing keys are reused. Recovery status exposes a bounded reason without credentials or paths.

A temporary read/permission/sharing error is not corruption: reads retry at most three times in the current call (150 ms total delay), then propagate the I/O error while preserving state and key bytes. Save-time key read failures also preserve the existing key. Failed quarantine aborts recovery rather than permitting later overwrites. This retains the original unavailable-storage behavior for persistent failures; it does not invent an empty successful configuration.

Tests cover restart round trips, corrupt/missing state and keys, transient/persistent key and state locks, save-time read failures and failed quarantine. The focused CI suite includes NAS and cloud-drive regressions. Real Windows file-lock and installed-app acceptance remain for Ziling; deterministic injected I/O tests run locally.

Local verification: **46 passed** across NAS recovery, NAS operations and cloud drives using an isolated test environment with the project SMB dependency. Ruff and diff checks passed. The first broader invocation found a missing local `smbclient` dependency; after installing it only in the isolated test environment, the full targeted set passed.
