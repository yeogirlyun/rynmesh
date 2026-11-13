# Planning fix release integration — 2026-11-13

- Public integration: 451adf8c, develop; includes upstream PR 133 and preserves local completed updater/company connection changes.
- Private integration: 1b60464, develop, yeogirlyun/gshopper-ryn-node; PR 16 adds evidence replay and tests, not employee plugin runtime changes.
- Public targeted regression: 176 passed, 1 skipped (schedule, planning, table, read reliability, continuation, service updates, full package, installer trust).
- Private acceptance contracts/replay: 13 passed, with RYN_SOURCE_ROOT pointing to the integrated public checkout.
- Full upstream backend CI is not green: PR 133 run 43360731647 had 43 failures. All 43 test IDs also failed in preceding run 43212998870 (44 failures); no new failing test IDs. This comparison does not certify the whole suite or employee acceptance.
- Release intended through signed GitHub Actions from committed develop, version 0.9.1. Publishing/build and installation status must be checked separately.
- Original workspace uncommitted files preserved. No employee records or credentials included.

## Published result

- Published v0.9.1 as GitHub Latest on 2026-11-13 05:48:33 UTC (13:48:33 Asia/Hong_Kong).
- Build commit: f408936f830bac157a298622b97238d2a5cf61a7; private develop: 1b60464.
- Release: https://github.com/yeogirlyun/rynmesh/releases/tag/v0.9.1 (20 assets).
- Release package run 43371956166: success, including wheel install check, macOS signature/sidecar checks and Linux installation smoke test.
- Signed desktop build run 43371958575: success on Windows x86_64 and macOS arm64/x86_64.
- Independent signature/hash verification run 43373727709 attempt 3: success. Earlier retries corrected draft metadata/notes formatting; binaries were unchanged.
- Extra version/manifest regression: 5 passed. Downloaded wheel contains exact committed planning, schedule and table modules; downloaded Windows installer matches GitHub SHA256.
- Publishing enables update discovery; no claim that employee devices have already installed the release or completed business acceptance.
