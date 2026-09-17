# Owned-device erasure confirmation

Status: proposed; deletion authority decision pending. No runtime implementation
or remote deletion claim is delivered by this document.

## Problem and evidence

The v0.7.0 `ConversationCleanup`, `ReadingCleanup`, `LibraryCleanup`, feed and
friend-card cleanup jobs expose `remote_confirmed: false`. This is truthful.
`ReplicaStore.acknowledge` records a received revision, including rejected rows;
it cannot attest that private source bodies, backup files, search documents,
retained task results or browser storage have disappeared on another computer.

Current cleanup plans also have different retention contracts. Conversation jobs
compact identifiers when all local steps finish; reading cleanup retains causal
barriers; downloaded documents and friend cards are not device-sync scopes.
A shared boolean cannot replace the missing target ledger and cleanup adapters.

## Decision needed before implementation

Choose one authority model for owned, explicitly paired devices:

1. **Receiving-owner review (recommended initial policy).** The initiating device
   proposes exact records and versions. The receiving device displays its own
   matching copies and asks its owner to approve deletion. A changed local plan
   requires a fresh review. Offline devices remain pending.
2. **Explicit remote-erasure capability.** A separate pairing permission allows
   the initiating owner to review targets and scope once and dispatch deletion.
   It must be granted on both devices and versioned independently of ordinary
   synchronization. Existing pairings do not gain it during upgrade.

Neither option includes friends' devices, copies independently saved by friends,
trusted roots, remote filesystems or arbitrary paths. Scope approval authorizes
only named records that the receiving node can resolve through typed adapters.
The user has been asked to choose; implementation of destructive dispatch waits
for that answer. Read-only inspection and this design require no additional grant.

## Proposed user flow

1. Review the local cleanup. Separately select owned devices and supported data
   categories. Show the exact target set, approximate record counts, pending active
   tasks and excluded copy classes. Selecting no targets cannot produce a remote
   confirmation. The local cleanup may still be completed independently.
2. Save an immutable request manifest before local plans compact identifiers.
   Each manifest binds the local cleanup ID/generation, category, stable record
   ID, reviewed revision, target peer/pairing epoch and requested copy classes.
3. Deliver an encrypted proposal. Under policy 1, the remote owner reviews a local
   preview and confirms; policy 2 additionally verifies its current explicit grant.
   A proposal cannot approve itself or expand scope after review.
4. Execute a durable remote job using local source/replica/backup/index/result
   adapters. Retry the same job ID after interruption; never create a broader job.
   Active paid tasks require their own cancellation/reconciliation first.
5. Return a signed, encrypted completion receipt only after every included phase
   commits. The initiator verifies target identity, request digest, epochs, exact
   scope and phase coverage before marking that target confirmed.
6. Show one row per original target: awaiting consent, queued, offline, running,
   partially cleared, scope changed, revoked, rejected, unsupported or confirmed.
   A lost response resends the same request or queries the same receipt.

## Proposed protocol and local APIs

Names below are sketches, not endpoints shipped in this PR.

- Owner API: `POST /api/local/device-erasure/preview` with local cleanup reference,
  selected pairing IDs and categories; returns a bounded preview/review token.
- Owner API: `POST /api/local/device-erasure/jobs` with that token; returns a
  durable job ID. Separate resume/status/cancel-uncommitted operations use the ID.
- Receiving-owner API: list pending proposals; preview one; approve the exact
  local review token or reject. These routes use the existing local-owner guard.
- Peer API: `POST /api/peer/device-erasure/proposal` and `/status`; exact versioned
  schemas, sealed payloads and signed receipts, with current pairing policy checks.
  Unknown protocol versions return unsupported, never success.

The transport envelope binds both peer IDs, pair ID, actor IDs, local/remote policy
revisions, request/job ID and canonical request digest. Status responses include
only phase/state/error codes; record titles, bodies, prompts and paths stay out of
URLs, logs, public manifests and acceptance artifacts. Authentication is repeated
before every destructive phase and before accepting the final receipt.

Initial bounds to validate: at most 16 selected devices, 100 records per request
chunk, 10,000 record identities per job and 32 retained jobs. Bound total encrypted
journal bytes and outstanding chunks, not just counts. Never truncate required
targets or deletion scope to fit; reject before starting and let the owner narrow
the review. Use one bounded worker per node, a five-second transport deadline and
completion-scheduled retry with capped backoff. No file locks span network calls.

## Confirmation semantics

`remote_confirmed` belongs to the immutable reviewed target set. It may be true
only for a nonempty set when all selected targets have verified completion receipts
for the exact included copy classes. Removing or revoking a target does not shrink
that set into success. A new pairing is a new identity and cannot satisfy the old
one. A post-review edit is scope-changed or excluded by revision, not silently erased.

Confirmation is an as-of receipt, not a claim about all future copies. Explicit
later recreation is a new generation; old receipts must not certify it. Restoring
a backup cannot silently resurrect reviewed data: deletion barriers and relevant
job generations must survive supported restore/migration workflows.

Browser history, exported archives, OS snapshots and friend-saved copies are not
remotely proven absent. The UI must say **selected node-managed copies confirmed**
and list exclusions. Existing source-only privacy receipts keep their original
meaning; do not rewrite them to imply a complete device wipe.

## Adapter rollout

1. Conversations and reading/bookmarks are the initial candidates because device
   sync already names their stable records. Extend local cleanup with exact-ID,
   revision-bound plans; the current all-local-records cleanup must not be reused
   to erase unrelated remote records. Preserve conflict/recovery branches until
   they are included in a review. Include retained task-result bodies explicitly.
2. Library documents, offline copies, friend-feed metadata and cards need their
   own stable identity/provenance mapping and consent scope. Until an adapter exists,
   report unsupported and keep its confirmation false. Do not discover remote
   files by path or delete everything in the same category as a shortcut.
3. Browser cleanup remains a separately acknowledged local browser phase. A node
   receipt cannot claim it completed while that browser is offline.

## Implementation tasks and acceptance gates

1. Settle authority model and exact initial copy classes; update pairing schema and
   compatibility tests if a new permission is chosen.
2. Add bounded encrypted manifest/receipt storage, monotonic generations, durable
   resume and compaction rules that preserve unfinished targets and anti-replay data.
3. Add typed selective-cleanup adapters and fail-safe preview comparison. Test
   source, replica, known backup, search and retained-result phases independently.
4. Add authenticated transport, per-phase authorization, replay/nonce binding and
   status recovery. Test wrong peer, wrong request, stale epoch, reused receipt,
   unsupported version, revoked permission and malicious identifiers/path strings.
5. Add owner review, per-device progress and explicit exclusions. Test changed
   scopes, offline/denied/partial states, zero targets and no false completion.
6. Run three-node real-HTTP acceptance: one confirms, one stays offline, then
   rejoins; crash after each phase and after receipt commit; replay without a second
   deletion; revoke during work; introduce a new record after review. Assert that
   unrelated remote records survive and completion never precedes all required
   acknowledgements. Repeat on packaged nodes and distinct networks before claiming
   physical acceptance. Record safe state codes and timing only.

This design is reviewable independently of the preceding feature PRs. It does not
close the implementation issue or change any cleanup flag by itself.
