# Company runtime packaging and restart acceptance — 2026-11-02

## Integrated source

- Company repository develop code: `89fbbace1061ab0dd5436d71adba63af02e98674`.
- Ryn repository develop code/native validation build: `2de256e80a36e73579275a93372a749c05ef7ecc`.
- Included concurrent develop work: company Dashboard session leases/HTTP reuse (`3ee3000`) and Ryn opaque OAuth session revisions (`4b51cc9`).
- Company integration checkout: `D:/code/_build-tools/company-develop-20261102`. The original chat-backend working tree and its unrelated uncommitted changes were retained.
- Both task branches were fast-forwarded into their repository's local develop. This record is documentation added after the verified code commits; no remote push is claimed.

## Faults addressed

1. Business DB/auth middleware and synchronous MCP dispatch could hold the event loop or block health checks. A dedicated outer ASGI runtime health endpoint bypasses business dependencies; synchronous middleware work and MCP dispatch run in worker threads. Existing authentication, origin, and snapshot restrictions remain on business routes.
2. The old installer moved the current runtime away before copying its replacement. Installation now verifies a complete file inventory, stages an immutable content-addressed directory, then atomically replaces services.json while retaining the old registry/runtime. Reinstalling the same content reuses its directory.
3. The service port was not explicit in the child environment. Registration now sets COMPANY_APP_PORT to the registered port.
4. Windows PowerShell launched from Python with an inherited PowerShell 7 module path failed to resolve Get-FileHash. Installation now hashes through .NET streams. UTF-8 registry reads preserve Chinese data paths.
5. Rebuilding could include old or redirected frontend output. Builds require committed source (develop for delivery), regenerate both web applications, clear output/debug overrides, and inventory the shell plus original Dashboard entrypoints.
6. An OS-owned data-directory lease prevents duplicate service processes; abnormal termination releases it without stale PID-file recovery.

Ryn's previously integrated configuration reload, sustained-failure recovery and error-page retry fixes remain present. This task adds actual frozen-runtime lifecycle acceptance to that supervisor.

## Verification

- Company backend: **39 passed** across desktop runtime/install, standalone boundary, home migration, SSO, SSO queue, request disconnect, and session lease suites.
  - Entry: `scripts/test_affiliate_isolated.py`; default-deny networking installed before application imports.
  - Installer cases exercise Windows PowerShell from Python, Chinese data paths, repeated installation, upgrade retention, corrupted/missing files, traversal, and unexpected private files.
  - Slow MCP identity lookup does not prevent concurrent runtime readiness.
- Company frontend: **21 passed** in dashboardSession and OriginalDashboardFrame through `scripts/test_web_isolated.mjs` with the VM network guard.
- Ryn TypeScript/Vite build: passed.
- Ryn native plugin manager: **5 passed**; two explicit fixture-only tests are ignored in ordinary runs.
- Explicit actual-EXE lifecycle test: **passed**, including reuse, child crash/restart, changed configuration replacement, orderly shutdown, and Windows owner-process crash/Job Object cleanup. The ignored owner helper is exercised by this test.
- Final company bundle built from develop using `scripts/build_desktop_service.py`, without `--candidate`.
- Final frozen bundle cold/abrupt restart verification: **3 passed**, ready in **3.313 / 1.109 / 1.110 seconds**, within the supervisor's 30-second startup deadline.
- Existing synthetic encryption key and encrypted setting remained readable after all restart cycles.
- No final acceptance service process remained running.

Dependency installation is separate from packaging; existing installations were reused only after matching the checked-in lock hashes. Optional npm audit/network waits were not used as a release gate. A normal FastAPI TestClient deprecation warning was observed; no test failures remained.

Initial actual-bundle verification exposed the PowerShell module-path defect. A subsequent synthetic fixture with an invalid empty finance-proxy configuration correctly failed startup; the fixture was corrected to a valid offline-only configuration without weakening application validation. An initial pytest default temporary-directory cleanup encountered a Windows permission error; final suites used a dedicated ASCII basetemp.

## Build artifacts (not committed)

- Company bundle: `D:/code/_build-tools/company-runtime-develop-20261102/dist/company-service`.
- Manifest SHA-256: `49EAA7AD59AF4673685E108E51E4EF47AE606B5B59270BC2230FC2EFE59F12B4`.
- Company EXE SHA-256: `ECE7BBA5D7AB1FA494A05E0537B9F8EDAC25CC0F298EE7F4162E420A449F9301`.
- Ryn release-mode native validation EXE: `D:/code/_build-tools/ryn-runtime-tests-20261102/manager-tests.exe`.
- Native validation EXE SHA-256: `AF9907B44039B138F63E818F308EB0787F16EAEDB18D3050D34C12B54AC1CB9B`.
- Final isolated verification: `D:/code/_build-tools/company-runtime-smoke-develop-20261102/verification.json`.
- Isolated registry/data live only under that verification directory; logs and binaries remain outside Git.

## Deployment boundary

**No replacement of the user's installed Ryn or company runtime was performed by this task.** No production database, account credentials, private configuration, or business actions were used for acceptance. The offline child blocks Python outbound networking before application import (with only asyncio's newly owned self-pipe allowed); this is not an OS-wide sandbox claim.

Runtime readiness indicates completed process startup, not the availability of external finance/WeCom/model services or a valid user login. This acceptance covers packaging, local connections, restart/lifecycle and preserved configuration; it is not a full production financial-function audit.

