# Small repairs and employee-service handoff

Owner-requested local work on 2026-11-02. Public review worktree:
`/Users/yeogirlyun/.codex/worktrees/pr-merge-fixes/rynmesh`.
Company review worktree:
`/Volumes/ExternalSSD/rynmesh-reviews/gshopper-20261101`.

## Delivered source and merge boundaries

- Company [PR #10](https://github.com/yeogirlyun/gshopper-ryn-node/pull/10)
  merged to `develop` as `e07fbe3`: initialize empty source tables on startup;
  preserve existing records; exercise Windows account-discovery logic with
  synthetic module-local platform/process fixtures on Mac.
- Company [PR #9](https://github.com/yeogirlyun/gshopper-ryn-node/pull/9)
  merged to `main` as `5fed1ee`: employee-service coverage/history contract and
  milestone assignments only. The complete application server is not on main.
- Public [PR #118](https://github.com/yeogirlyun/rynmesh/pull/118) targets
  `develop`, still under integration review. Restores responsive settings,
  Darwin resource handle resolution, missing math import, accepted Rust discovery
  and Undici 7.30.1 changes; builds the UI before the sidecar. Adds an encrypted
  MCP conversation/restart/context contract regression. Minor bilingual caption
  and byte-unit fixes address the broad web suite's localization finding.

No deployment, production migration, real message, browser automation or recurring
monitoring was performed. The owner's primary checkouts and pre-existing untracked
acceptance fixture were preserved. Runtime artifacts stay outside Git.

## Local evidence

Python 3.13, pytest in `/tmp/rynmesh-review-20261029`, Ruff 0.12.9 and Node 22:

- 141 public focused tests pass across settings, long-task continuation, JS
  resources, encrypted task storage, business plugin handoff, MCP runtime,
  plugin views and OAuth session revisions.
- 13 Rust-discovery/desktop entry-point tests pass with temporary compiler/build
  fixtures; shell syntax and changed Python lint checks pass.
- Company default-deny pre-import runner: 41 pass, 5 platform checks skipped
  across cold schema/reopen, WeCom delivery/queue/recency, Dashboard sessions/SSO
  and desktop runtime. These do not prove native Windows live delivery on Mac.
- A cold `npm ci --ignore-scripts` succeeds using the corrected lockfile.
  Typecheck and production build pass. Broad web tests initially found four
  untranslated literals (1351 pass / 1 fail); the follow-up fixes translate the
  captions and byte units rather than weakening the localization assertion.
  The final broad web run passes all 1352 tests across 167 files.

Remaining explicit gate: `tests/test_extension_data_policy_runtime.py` has
13 passes and 2 failures on this repaired source. Both expected automatic
compaction but hit `agent_tool_schema_limit` before compaction. They do not
establish a leak; they require capacity/menu/compaction compatibility work.
Whole-tree fixture lint findings and third-party browser redistribution boundary
also remain integration gates. A passing focused suite is not full release
acceptance or permission to overwrite main with the develop snapshot.

## Next owner-requested work

See [generic employee-service contract](../EMPLOYEE_SERVICE_WORKFLOWS.md) and
private `docs/EMPLOYEE_SERVICE_DELIVERY.md`. Ziling's Chinese assignments are on
[public #116](https://github.com/yeogirlyun/rynmesh/issues/116#issuecomment-6583800831)
and [private #4](https://github.com/yeogirlyun/gshopper-ryn-node/issues/4#issuecomment-6583800427).

Priorities: reconcile source/test ancestry; finish service schemas and an actual
workflow coverage map; correct the existing identity fallback without a large
auth redesign; link employee work history and bounded retrieval; demonstrate a
second employee's clean client completing report -> file -> reviewed WeCom
sharing -> confirmed result -> restart -> follow-up. Personal history remains
private. Keep the three-provider/six-to-ten-invitee pilot manual and bounded.
