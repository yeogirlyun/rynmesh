# Device sync follow-ups (#65)

## Changes

- Verification-code presentation is based on the pairing role. The inviter's
  public response carries an empty code in every state, and the UI independently
  hides it for the inviter. The joiner shows the code with instructions to enter
  it on the inviting computer. Existing pairing IDs and verification protocol are
  unchanged; this is a presentation fix, not a new secret or cryptographic claim.
- Quarantine copy refers to the original records and explicitly includes
  conversations, rather than claiming every record remains in reading history.
- Replica status caches only its bounded public projection. File identity, size,
  nanosecond modification/change timestamps and local writes invalidate it under
  the existing transaction lock. No decrypted replica body is cached. External
  atomic replacement, missing/corrupt files and caller mutation are tested.
- Refusal diagnostics share a 2 MiB plaintext budget across all pairs/scopes,
  further reduced to leave 64 KiB headroom under the 8 MiB pairing plaintext cap.
  Fair per-entry byte quotas retain detailed IDs first, then overflow IDs.
  Truncation yields minimum counts and a visible incomplete-diagnostics notice.
  Clearing the retained IDs cannot incorrectly claim every rejected row merged.
  Existing ACK/revision records and user content are not discarded by this budget.

## Evidence (2026-09-17)

Backend full regression: 1559 passed, 29 skipped. Frontend: 328 passed on Node 22.
TypeScript/build and Ruff passed. The final capacity test additionally passed with
256 active plus 256 retained revoked pairings, each carrying three maximum-sized
refusal sections before compaction. The encrypted file stays below its limit and
an unrelated policy update still commits and survives reload.

Installed-wheel browser acceptance used two fresh loopback nodes paired through
the real signed device protocol. The inviter's active device card showed pairing
confirmation and no verification code. [Screenshot](inviter-active.png) contains
only synthetic device identities. The quarantine and truncated-count states are
covered by component tests; they are not claimed as this browser run's state.

## Repeat

1. Install the packaged build and pair two disposable test nodes for an explicitly
   chosen scope. Enter the joining node's code on the inviter, then finish pairing.
2. Refresh both pages. The inviter must show no code before or after approval; the
   joining device retains its entry instructions. No AI permission is implied.
3. Run the cache regression with an unchanged file, a local write, another store
   instance, same-sized atomic replacement with restored mtime, deletion and damage.
4. Run the shared-budget test and verify lower-bound status after truncation and
   after all retained refusals are accepted. Never infer remote deletion from ACKs.

This patch preserves the v0.7.0 wire format and does not implement cross-device
erasure confirmation or claim physical cross-network acceptance.
