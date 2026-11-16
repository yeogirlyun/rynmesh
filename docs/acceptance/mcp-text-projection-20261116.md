# One copy of exact duplicate MCP text in model context

Date: 2026-11-16 (America/New_York). Base: `033b6861`.

Text-only MCP results can contain the same text in `content` blocks and the
convenience `text` field. The single-call and parallel-read paths now omit the
flat field from the model-visible copy only when it exactly equals the newline
join of the text blocks. Mixed media, different explanatory text and malformed
blocks remain unchanged. All content blocks, annotations, structured data,
pagination, error flags and provenance stay available.

Durable receipts, events and encrypted result blobs retain their existing
response. No provider request, tool call, approval or permission change is added.
The existing large-result/reference limits and isolation remain unchanged.

## Validation

- 36 focused tests passed: result isolation, single and parallel MCP replies,
  extension execution, structured results, error continuation, read reliability
  and budget dispatch. Runtime tests confirm the next model request gets one
  copy while the stored receipt retains both originals.
- Ruff and `git diff --check` passed. The touched loop import was formatted for
  its existing lint check.
- A synthetic Chinese text-only result repeated in both fields measured
  **10,556 → 5,346 UTF-8 bytes (49.36% less)** in the model-visible result.
  This measures that fixture's serialized result, not total prompt tokens,
  billed cost, model-call count or end-to-end task success. Structured-only
  replies and replies without exact duplicates receive no saving.

No program was packaged, installed, restarted or deployed during validation.
