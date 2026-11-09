# Kept plugin DOM order — 2026-11-10

## Observed defect

Installed main commit `0a400c1bc248fdc9e2c351b54684a435d8c717d4`: six native sidebar switches produced six OOPIF detach/attach pairs and six additional company account GETs. The DOM iframe identities remained unchanged. Passive local evidence is archived outside Git in `artifacts/session-stability-20261110/relocation-diagnostic.json`; no credentials or business data are included here.

The page keeper reordered its keyed React children on every revisit to track LRU. Moving an iframe ancestor preserves element identity but can recreate its browsing context. Element-identity-only tests missed this.

## Change

Keep rendered pages in insertion order. Track LRU independently in a ref, updated by show/drop. Revisiting an existing page changes visibility without moving its DOM sibling. Opening a fourth page still evicts the least recently used page, leaving surviving sibling order unchanged. Manifest updates retain the existing slot.

## Validation

Regression checks explicitly compare sibling order across revisits and verify that revisiting the first page causes the second page to be evicted when a fourth opens. Existing iframe visibility, manifest refresh, disabled-plugin removal, and service lifecycle tests remain applicable. Packaged browser-context validation is performed by the release owner after rebuilding committed develop; jsdom tests do not claim to verify a real WebView browsing context.

Validation result: targeted `KeptPluginPages`, `ExtensionWebApp`, and `PluginWorkspace` Vitest files passed, 30 tests in 50.20 seconds, using one threads worker. Initial fork workers failed to start (no tests ran); the threads rerun completed successfully. TypeScript `tsc -b --noEmit` and `git diff --check` passed.
