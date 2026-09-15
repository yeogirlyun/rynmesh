# Rynmesh v0.7.0 Release Notes

Rynmesh is alpha software. This release delivers the eight P2 "Friend Mesh"
user-facing features from `docs/product-briefs/`, plus 21 release-hardening
fixes (PR #57) found during pre-release review of that work. See
`docs/PR_57_RELEASE_HARDENING_WORK_PLAN.md` for the full finding-by-finding
record and `git log --oneline 094f03a..v0.7.0` for the exact commits.

## New user-facing features

1. **First reading experience** — see, read, and bookmark real content right
   after install.
2. **Pairing and sharing** — invite a friend to join and complete the first
   content share.
3. **Unified Ask Ryn** — ask questions, review history, and switch models
   from one entry point.
4. **AI setup and friend permissions** — enable local AI with one click and
   decide who can use it.
5. **Search and retrieve** — one search recovers content, shares, and chat
   history.
6. **Follow friends' content updates** — keep seeing new content friends
   choose to share.
7. **Offline reading** — read downloaded content without a network
   connection.
8. **Multi-device sync** — switch to another computer and keep reading and
   chatting.

## Hardening fixes in this release

### Ask Ryn

- Cancelling an Ask Ryn run now completes instead of getting stuck in
  "cancelling" forever.
- Ask Ryn's saved history is no longer rewritten on every status check when
  nothing changed, cutting unnecessary background disk writes while a run is
  active.
- A succeeded, paid Ask Ryn answer is kept until it is safely archived,
  instead of being lost and reported as "interrupted" if it vanishes before
  the next check.

### Device sync and pairing

- A single bad sync row from another device no longer stalls an entire sync
  scope; good rows keep syncing and the bad row is quarantined instead.
- The sending device now records which rows a peer rejected and keeps
  syncing the rest, instead of endlessly retrying the same failed batch.
- Local reading progress and bookmarks always save immediately on your own
  device, even when background sync capture fails.
- Revoking a friend's device link is never stuck "pending" forever, and
  retrying a stuck revocation no longer spams the friend's mailbox with
  duplicate notices.
- Device pairing storage no longer fills up with finished invites and pairs;
  the peer connection rate limiter now expires old entries instead of
  permanently locking out new devices.
- Approving a new device pairing now requires typing the verification code
  shown on the joining device, closing a race where another device could be
  approved under an attacker-chosen name.

### Friends and privacy

- Friend and content identifiers no longer appear in request URLs or access
  logs.
- Joining a friend now seals the invite secret to the inviter, closing a
  window where an on-path attacker could hijack a pairing.
- Privacy export payloads are now built field by field from an explicit
  allow-list, so a newly added field can no longer leak into an export
  silently.
- Search's "shared by a friend" attribution now requires a card that friend
  actually delivered, instead of trusting friend-supplied metadata alone.

### Search and offline reading

- Clearing offline content now forgets its title and URL too, not just the
  downloaded body.
- Temporary-file cleanup now also covers stale device-pairing state, not
  just source, replica, and search files.
- One oversized saved item can no longer disable search entirely; oversized
  rows are clipped instead of failing every query and rebuild.

### Web app

- Ask Ryn exports keep their downloadable file alive until the browser
  actually starts the download, fixing a broken-download risk in some
  browsers.
- Ask Ryn requests now time out instead of leaving the composer stuck when a
  request hangs, and cancelling a stuck request no longer dead-ends with no
  way to recover.
- Successful actions (removing a device, clearing offline content, resolving
  a sync conflict, unfollowing a friend) are no longer reported as failures
  just because the follow-up status refresh failed.
- Search keeps your current results when "load more" fails, and stops
  re-querying an index that is stuck instead of hammering it every few
  seconds.
- Cleanup panels now honor confirmed remote deletion instead of always
  claiming remote devices are unconfirmed; the friend AI list keeps its last
  known contents on a failed refresh instead of going blank; the content
  reader can be closed without saving reading progress.

## Wire-format changes — all nodes must upgrade together

- `POST /api/peer/friends/accept` now uses join kind v2: the invite is a
  sealed secret bound to the joining peer's own identity. Nodes on different
  protocol versions cannot complete a friend join with each other.
- Device-sync ACK receipts now carry an optional `rejected` marker so a
  sender can tell which rows a peer refused. Older peers do not populate or
  understand this marker.

## Known limitations

- macOS on-device acceptance and cross-NAT two-egress acceptance were
  skipped for this candidate, with the maintainer's agreement.
- No V100/CUDA hardware acceptance was run.
- Not all 95 product acceptance cases were executed manually on a device for
  this candidate; see `docs/product-briefs/README.md` and
  `docs/acceptance/candidate-delivery/` for what was and was not exercised.

## Flagged product decisions (unchanged in this release)

- The 20,000-entity device-sync cap stays as-is.
- The friend AI service catalogue continues to distinguish `revoked` from
  `not_authorized` when reporting why a friend cannot reach a service.
- A revoked friend's cached feed inbox is retained until you unsubscribe or
  run cleanup; it is not deleted automatically on revocation.
- All backend cleanup jobs continue to report remote copies as unconfirmed;
  the system does not claim cryptographic proof of remote deletion.

## Verification

Run locally on this machine (macOS, this worktree, `dist/` untracked per
`.gitignore`):

- **Backend:** `python3 -m pytest tests/ -q` — **1580 passed**, 0 failed (via
  `./scripts/build_release.zsh`).
- **Backend lint:** `python3 -m ruff check rynmesh/ tests/` — all checks
  passed.
- **Web app tests:** `npm test` (vitest) — **52 test files, 317 tests, all
  passed**.
- **Web app types:** `npx tsc -b --noEmit` — no errors.
- **Release build:** `./scripts/build_release.zsh` — production web build,
  wheel build, and UI-bundling checks all succeeded.
  - Wheel: `dist/rynmesh-0.7.0-py3-none-any.whl`
  - SHA-256: `109fcaaa6bd4dbca4a743acc157b3c558bd1976c9c625710af8e0474e665c0a5`

Not run on this machine — covered only by upstream CI:

- The packaged-node UI build/check.
- Desktop (Tauri/Rust) compilation for macOS Apple Silicon and Intel.
- The three end-to-end job(s) (LAN two-node, friend-mesh, and device-sync
  acceptance workflows) that require multiple running nodes.

No production code outside version metadata was changed to make the build
pass; the only test-suite failures encountered during this release step were
two release-version consistency checks
(`tests/test_version_consistency.py`, `tests/test_public_website.py`) that
needed their expected version string and the public website copy updated to
`0.7.0`, both fixed as part of this version bump.
