# Reviewed source fixes — 2026-11-09

Base: public `cd1296dd9d8ba77d2d625be96e8d9ac872d2dbd0`; paired private
`88d244026114900aeec3050a17b5e7586681f7a2`. These are source integration checks,
not a new installed or published release. Owner requested small fixes and merges;
larger continuation/employee work stays with Ziling.

## Changes

- Image inspection and native screenshot revalidation now resolve macOS open
  file descriptors using F_GETPATH, as the JS resource reader already does.
  Containment, file identity, bounded reads and image hashes remain checked.
- Image route tests first discover lazy tools. Text-only conversation tests
  verify omitted image bytes, retained text and a visible omission event, while
  explicit image inspection still rejects unsupported routes. Real image payload,
  provider request linkage, output limits and budget assertions remain in place.
- Fresh installations still use automatic mode. Ambiguous legacy workspace
  settings keep their saved value instead of silently being migrated; current
  explicitly saved choices and task modes remain unchanged.
- Restore the complete Windows installer's pre-56f3405 verified Office staging,
  pre-uninstall validation, CJK/CLI provisioning, repair backups and reinstall
  preservation tests. Keep the new online download code for later release.
  Office is bundled again in the complete installer; the small WebView2
  bootstrapper and unrelated package build optimizations remain.

## Why restore bundled Office

The required `office-cjk-1` release API returned HTTP 404 during this review.
No trusted release archives were found in the reviewed local repositories.
The complete installer must not promise Office only through unavailable downloads.
The restored builder requires verified font builds and CLI payloads rather than
silently producing a broken package. The four online assets still need publishing
and actual clean-install/download acceptance by Ziling. No release uploaded here.

## Verification

The local Python 3.13 test environment is `/tmp/ryn-review-20261109`.
Focused suites cover image routes/host files, model parity, automatic-mode
provenance, Office provisioning/repair, JS resources, error/cancel/unknown-outcome
continuation and plugin catalog freshness: **581 passed, 8 skipped**.
Ruff passes on all changed Python files; `git diff --check` passes.
Optional QuickJS worker cases require a prepared development profile;
the new host-image tests run independently of that profile.

Earlier review of the unchanged frontend: 123 focused UI tests passed;
TypeScript and production build passed. Company source at the paired SHA:
40 guarded Python and 20 guarded UI tests passed, 75-tool contract passed,
scoped central Ryn typecheck reported zero diagnostics. No real models, company
business writes or WeCom sends were used.

No Windows system installer or new native desktop build was executed here.
The archived Windows acceptance record describes its original build SHA only.
The full backend suite and broader main promotion remain separate work.

## Held work / handoff

Do not promote the whole develop branch to main: automatic continuation can reset
owner budgets, mode, scope and response preferences. Ziling owns structured,
recoverable task-chain continuation and durable employee workflow/history.
The updater mechanisms are absent from the supplied develop delivery and need
separate source PRs and upgrade/rollback acceptance.

Detailed Chinese assignments are in the private canonical repository:
`docs/assignments/NEXT_BATCH_2026-11-09_ZH.md`.
PR #112 has passing checks but a normal merge was rejected by GitHub's required
review policy. No administrator override or automatic-merge rule was used.
