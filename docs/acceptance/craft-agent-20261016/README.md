# Ryn office application tools — 2026-10-16

The Agent can operate on documents through the pinned upstream Craft engines,
in addition to discovering the enabled applications. This delivery targets the
four office plugins on Windows x64. Other Craft editors continue to report their
existing information-only capability.

## Delivered behavior

| Application | Connected operations |
| --- | --- |
| WordCraft v0.6.1 | New/open document, insert text, select and replace text, inspect/read back, save DOCX/PDF/text |
| GridCraft v0.6.1 | New/open workbook, read/write cells and ranges, evaluate formulas, format/sort/filter, tables/charts, save XLSX/CSV/TSV |
| DeckCraft v0.6.1 | New/open presentation, slides, shapes/text/tables/charts, inspect, save PPTX or export PDF |
| PdfCraft v0.7.1 | New/open PDF, extract/search text, inspect, page insertion/reordering/rotation/deletion, text/comments/forms, save PDF |
| Resume / Recruit / Desk | Discover complete operation schemas and fresh workspace context, then use the existing reviewed action/receipt path |
| NAS | Discover the exact authorized-resource tools; existing task scope and data-release requirements remain enforced |

`ryn_plugin_info` describes the installed component and the actual supported
formats. `ryn_craft_tools` discovers the pinned engine's exact schemas, filtered
through Ryn's reviewed allowlist. `ryn_craft_read` retrieves document data and
`ryn_craft_action` performs document operations. Generic upstream command/batch,
script, printer, desktop-control and arbitrary RPC entrypoints are not exposed.

The office component is a separate explicit installation, available from each
application's Agent dialog or `ryn_install_craft_automation` with exact approval.
Listing applications, opening Ryn and starting the node never download it.
The existing web-editor package and its native document component have separate
receipts inside the same plugin version directory. Explicit installation can
repair a damaged document component without replacing the web package.

## File and session behavior

- Native engines run in a separate, task-owned headless session. They do not
  access the open web editor's unsaved document. No desktop permission is needed.
- All input/output paths pass the existing workspace policy. Inputs are copied
  into private staging and matched to the approved hashes before the engine sees
  them. Saves require an explicit new pathname; staged exports are published
  without overwriting a concurrently created file.
- Successful exports include SHA-256, size and independent reopen evidence.
  Their confirmed receipts feed the existing artifact download/open flow.
- Edits in engine memory are temporary. After expiry, cancellation or backend
  restart, the Agent must reopen a saved file. Confirmed operations are recovered
  from the encrypted ledger; uncertain writes are not replayed.
- Native package archives and the complete extracted payload manifest are pinned
  in source. Updating the executable and forging the local receipt together is
  rejected. Menu status caches are metadata-sensitive; process startup always
  verifies the complete payload again.

PdfCraft's built-in-font text creation rejects Chinese characters. The actual
error is returned to the Agent; Chinese PDF output was verified through
WordCraft's PDF export. The listed operations are an integrated subset, not an
exhaustive acceptance of every upstream command or file-fidelity case.

## Validation

- Backend: 180 tests across 15 relevant suites passed, including native process
  bounds/cancellation, package integrity and repair, workspace/approval rules,
  staged export races, Chinese result paging, business operations, restart
  receipts, discovery and artifact downloads.
- Frontend: 160 relevant tests passed. One sending test initially exceeded its
  5-second timeout during concurrent native/model workloads; its full 23-test
  file passed when rerun alone. TypeScript/production-build results are recorded
  with the Windows package.
- Four actual official native packages: 29 RynTools operations and five exported
  files passed edit/save/reopen/hash/download checks. Chinese DOCX, Chinese PDF,
  calculated XLSX, PPTX and edited PDF were verified. No temporary staging paths
  appeared in the returned results.
- A real Codex-driven Agent task discovered GridCraft, produced a Chinese
  workbook, evaluated `SUM(B2:B3)=45`, saved and reopened it. An independent Grid
  process and direct XLSX XML checks confirmed the persisted formula and value.

Detailed local evidence is under
`output/craft-automation-acceptance-20261016/summary.json` and the adjacent run
directories. The real-model run's original evidence retains its initial harness
cleanup error; `recovered-summary.json` separately records the successful
business flow and cleanup status. Automatic command review refused recursive
removal of two verified temporary test-node directories with `blocked by policy`;
their test processes were closed and the residual paths were recorded.

No installed Ryn binaries, existing node configuration, existing application
records or current Agent conversations were changed by acceptance testing.

## Windows update package

Prepared installer: `output/Ryn-0.7.0-office-agent-20261016-windows-x64-setup.exe`
(42,683,538 bytes).

SHA-256:
`4e98c3dd4a08a8695e6ae3d75ec0493c47d7a9998be2af2fbf81c5284c848eda`

The package was built successfully and was not installed or launched. All 288
packaged Python source files and their frozen code objects, all 30 compiled UI
files, native transport and raw package metadata were checked against the build
inputs. The NSIS build's generated file directive and unchanged input hashes
link the verified sidecar to the final installer; the installer was not extracted
or executed. Package provenance is retained under
`output/craft-automation-acceptance-20261016/package/`.

After applying the update, open Applications, choose an office application's
Agent entry and select **Install Agent document component**. Each component is
downloaded only on that explicit action. Put source files in the Agent task's
workspace and request the desired document operation. The currently running Ryn
installation was not changed by this delivery.

## Follow-up: installed-node acceptance, 2026-10-16

At the owner's subsequent explicit request, the update was installed in the
existing Windows Ryn directory and the application was restarted through its
normal quit/start flow. The installed backend SHA-256 matches the verified
release (`07b153e963b53953e37370884c4bb59d5219602f257e56106223f905b95c977c`).
Previous program binaries were backed up before installation.

All four native components were then installed through the production owner's
`install-automation` API using the official online releases. The offline fallback
was not applied: its preflight refused an MSIX-virtualized pathname before any
write, and the normal online downloads completed successfully.

The installed node's configured real model (`gpt-6.1-sol`) completed four
synthetic document tasks through the normal Agent worker, permissions, Craft
tools, native engines and artifact routes. PDF, Word, Excel and PowerPoint all
passed create/edit/save/reopen/readback/download-hash checks. Independent actual
file parsing verified the contents; the changed Excel formula evaluates to 48.
The first PowerPoint attempt stopped at model-busy before any document write;
normal continuation of the same task after the other tests finished passed.
Original failure evidence was preserved separately.

Live installation records, task evidence, generated files and the combined ZIP
are retained under `output/live-office-install-20261016/`. Ryn remains running
on its Applications page. Existing user tasks and global model/permission
settings were not changed; the four new acceptance tasks and projects remain
available for inspection.
