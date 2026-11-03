# Dashboard central identity and refresh — 2026-11-03

## Confirmed production correction

The initial diagnosis was incomplete: a Grafana mapping alone did not admit the recipient. Production Ryn member status was still pending, with no grant. The explicitly authorized member was approved in a transaction with an audit event and private before-image backup; a subsequent database read confirmed active. The exact WeCom-to-Grafana mapping was read back. No user grant or login was fabricated. Identity records and credentials are excluded from this document.

## Integrated code and deployment

- Company develop: 52d41b38394c65e4e409ac4e350c4dfc71e3284b.
- Node develop: 1b48bf1549446d1f74503b364d2e8e8f8261ba1b.
- Ryn package source: 73433f905c8de41702bc3726d3d31b6df082b50a; reused core efd9fa13f8c2b501f689b6b04deec754cd79b8fb after verifying no core source differences and installer SHA-256 FFB575D8BC7F0937A6BFD437B828598E76780A60D9D1CF10117454BD528AFE59.

The client forwards a short-lived host identity proof to the original authority; no per-PC signing secret or administrator database row is required. The server checks signature, live grant/member, employee validity, original enabled Grafana identity and role. Existing legacy full-scope boundaries remain. UI identity changes reach embedded frames; explicit recheck bypasses cached and pending session reads; business responses and fetches disable caching. Refresh Data remounts the current page and fetches its data. This does not promise upstream synchronization or independent verification of every business page.

## Validation

- Company Python: 36 distinct cases passed (35 in the main run; four central cases rerun after adding one case).
- Frontend: session cache 8, original frame 14, cross-frame session propagation 2 passed.
- Node: eight distinct assertions across proof, live grant, and original authority tests; six affected assertions rerun after scope correction.
- Synthetic isolated browser: denied access recovered after server permission change without process restart; recheck fetched a new proof; Refresh Data fetched a new proof and business requests; Journal and Bank Accounts rendered. No production financial writes.
- Real workstation host proof -> production current-session endpoint: HTTP 200 with that workstation's original Grafana identity. This is not a recipient-login test.
- Local installed company runtime at C:/Users/子陵/AppData/Local/Ryn/plugin-services/company-52d41b38394c65e4/company-service.exe; service manager switched the owned process without restarting Ryn. Health, desktop identity and session endpoints returned 200. Prior binary retained; registry backup services.before-central-physical-20261103.json. Existing data/configuration preserved. Initial offline initialize was refused by the existing runtime lease; registry was only switched after file integrity verification, and normal managed startup succeeded.
- Full package offline acceptance: fresh install, repeated install preserving identity/key/disabled plugins, four Office editors/native CLIs, three company pages and service health passed. No external network in acceptance.

## Rollout incident and limits

First server rollout (18:25–18:27 local) caused HTTP 500 because singleton middleware injected request-scoped services. It was immediately rolled back; configuration/member approval remained. The correction resolves services lazily from requestContext, was integrated and redeployed, and passed real host proof verification. Unauthenticated MCP requests no longer return 500; invalid proofs return 401. Backups remain in the server runtime directory.

Recipient desktop 172.16.8.74 was not remotely updated: Ryn management requires authentication and SSH/SMB/WinRM were unavailable. Production configuration is confirmed; a final read still showed zero recipient grants. Recipient login and delivery are not claimed. Complete R8 installer and its receipt are in output/full-package-20261103-r8 after packaging finishes.
