# Workspace integration status — 2026-11-01

The reviewed source is `362f73a`. The integration candidate now includes main
`20513ce`, retaining Undici 7.30.1 and the common Rust host resolver. This is
source integration, not a released Agent or completion of the invited-user pilot.

## Small fixes completed

- Desktop CI builds the web interface before the sidecar's new dist preflight.
- Sidecar discovery retains the shared RUSTC/PATH/install-fallback resolver.
- Darwin JS resource validation resolves opened descriptors with F_GETPATH;
  Linux /proc and Windows handle validation retain their own paths. No filename
  guessing or weaker resource authority was introduced.
- Reparse simulation uses lexical normalization rather than recursively calling
  lstat through resolve on Python 3.13. Its rejection assertion is unchanged.
- Imported pytest fixtures have precise F811 annotations; no global rule or test
  assertion was disabled. The shell fixture stages its explicit synthetic web UI.

After these repairs, JS-resource, Rust-discovery and fork-reference suites passed
39 tests. Full source/test Ruff 0.12.9 passed. Earlier review of the source had
144 focused Agent passes / 1 skip, 80 compatibility passes / 7 failures, 11 UI
passes, TypeScript and production frontend build success. These are scoped runs.

## Larger gates still owned by Ziling

A broad run before these small repairs stopped at its configured 15 failures:
214 passed, 4 skipped, 15 failed in 133 seconds. It is incomplete. The first two
failures are partial-usage settlement discrepancies (charged 55 versus a stored
reservation-derived 4103). Thirteen Q06 cases stop at
`fixture_connection_refusal_not_observed`, before their intended access checks.
Do not weaken settlement or access assertions merely to obtain a green run.

The native 4K tool-projection failure (`agent_tool_schema_limit`) and the prior
larger regression inventory also remain to classify and repair. Fresh full CI,+both desktop builds, Windows terminal acceptance and real invited-user journeys
are required before merging the full workspace. Browser distribution, coherent
Apps status, Word/PDF save/share/reopen, unsaved editor handoff and Grid formula
compatibility remain the priorities in #114–#116.

The smaller node/service terminology and desktop CI ordering can be reviewed
independently. No production service, database, account or release was changed.
