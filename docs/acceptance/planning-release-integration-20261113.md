# Planning fix release integration — 2026-11-13

- Public integration: 451adf8c, develop; includes upstream PR 133 and preserves local completed updater/company connection changes.
- Private integration: 1b60464, develop, yeogirlyun/gshopper-ryn-node; PR 16 adds evidence replay and tests, not employee plugin runtime changes.
- Public targeted regression: 176 passed, 1 skipped (schedule, planning, table, read reliability, continuation, service updates, full package, installer trust).
- Private acceptance contracts/replay: 13 passed, with RYN_SOURCE_ROOT pointing to the integrated public checkout.
- Full upstream backend CI is not green: PR 133 run 43360731647 had 43 failures. All 43 test IDs also failed in preceding run 43212998870 (44 failures); no new failing test IDs. This comparison does not certify the whole suite or employee acceptance.
- Release intended through signed GitHub Actions from committed develop, version 0.9.1. Publishing/build and installation status must be checked separately.
- Original workspace uncommitted files preserved. No employee records or credentials included.
