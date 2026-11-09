# Company-only release scope alongside the default Office bundle (2026-11-10)

Upstream `26721e2` / `2b2e9cd` restores a verified seven-plugin installer and
portable image reads. These changes are retained. This task's explicitly
requested delivery contains Ryn plus only company-workspace, company-dashboard,
and company-affiliate and their shared service.

## Explicit release mode

Run the normal committed-develop core build, then stage the company-only payload:

```powershell
& D:/code/rynmesh/.venv/Scripts/python.exe D:/code/rynmesh/scripts/prepare_full_package.py `
  --company-only `
  --output D:/code/rynmesh/artifacts/session-stability-20261110/company-only-payload `
  --company D:/code/rynmesh/artifacts/session-stability-20261110/company-build/dist/company-service `
  --company-source D:/code/_build-tools/company-develop-20261102/integrations/company-app
```

`build_full_windows.ps1` takes its existing arguments; it verifies the payload
before building and derives the NSIS `WITH_OFFICE` define from its exact plugin
set. No runtime patching or uncommitted helper variant is needed. The default
staging invocation without `--company-only` continues to bundle all seven.

## Scope guarantees

- The manifest must explicitly declare exactly the three company plugins or all
  seven known plugins, without duplicates. Partial Office sets and unknown IDs
  are rejected. A company-only payload containing an Office directory is rejected.
- Company-only staging does not inspect Office caches or download Office files.
- Upgrade verification and provisioning skip Office entirely in company-only
  mode. They do not instantiate its manager, finish its setup, repair resources,
  overwrite preferences, or change the enabled state of an existing Office app.
- The outer NSIS installer omits the Office selection registry write for the
  three-plugin package. Its inner core installer runs silently; the existing
  optional-selection hook does not write new choices in silent mode.
- Frozen-helper acceptance derives its expected components from the same verified
  manifest. In three-plugin mode it verifies no Office was newly registered and
  that an existing disabled Office registration and a synthetic document remain
  unchanged on repeat installation.

The unit tests exercise both manifest modes, rejection of invalid/extra Office
scope, company-only staging with Office access forbidden, fresh/repeat company
installation with Office/network operations rejected, and unchanged default
seven-plugin staging. Existing Office compatibility and repair tests remain.
Related upstream image-handle, image-route and saved-permission-mode tests are
run together; this Windows run does not claim real macOS-device acceptance.

Actual frozen-helper/package/desktop results are recorded separately in the
combined release acceptance after rebuilding the merged committed source.

Validation: 57 selected tests passed in 8.32 seconds; PowerShell build-script parsing and git diff whitespace checks passed. The local test log is artifacts/session-stability-20261110/company-only-and-upstream-regressions.log.
