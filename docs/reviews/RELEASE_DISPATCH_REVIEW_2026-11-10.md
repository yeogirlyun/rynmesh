# Release dispatcher review — 2026-11-10

Scope: public PR #123 (`e39a6fd7883514ec70279300e6541e7cacadc66b`), against main `20513ce07f15de1231c255c1ae9ed8677864d25d`; release implementation inspected at develop `ca2b9774`.

The PR registers the existing, manually invoked release dispatcher on the default branch. It checks out the chosen source branch (default `develop`), synchronizes version files, pushes a commit/tag, creates a draft, and dispatches both builds at that tag. It neither publishes a release nor changes installed applications when merged. This is a narrow main-branch registration, not acceptance of the entire develop application diff.

## Small fix and local verification

- Reproduced that `00.9.0` passed the workflow's version preflight while Cargo rejected it. The dispatcher and standalone version setter now require canonical ASCII X.Y.Z without leading zeros, before changing files or creating tags.
- Existing version-setter and desktop-manifest tests: **5 passed**. Invalid-version coverage also verifies write mode leaves all seven version files untouched.
- Executed the actual workflow preflight locally for **11 cases**, covering valid versions, leading zeros, non-ASCII digits, prerelease/prefix/newline input, duplicate tags and older versions.
- Rehearsed all inline dispatcher steps against a temporary local Git repository and bare local remote, with a fake GitHub client. Verified consistent version files, commit/tag correspondence, draft-only release notes, literal shell characters in notes, and both build handoffs using the same tag.
- Original PR head had nine successful CI checks. That result belongs to the original head; checks for a revised head must be assessed separately. No live release workflow, remote release tag, installer or installed runtime was used in the local rehearsal.

## Ziling's next update batch

1. Add an explicit preflight before version/tag mutation: selected branch contains the version script and both dispatch-capable build definitions; required signing configuration is available. Report the exact source SHA and release prerequisites without exposing secret values.
2. Design resumable draft builds. After a build or dispatch fails, allow retrying the same immutable tag, reusing/verifying matching draft assets and rebuilding only missing/failed targets. Never silently replace a published version or require inventing a higher version just to recover.
3. Exercise the real signed update path on an isolated installation: active Agent work, transfers, plugin calls, an open unsaved document, deferred installation, failure recovery, restart and preserved history/settings/plugin state. Include platform-specific evidence and distinguish Windows results from unverified macOS behavior.
4. Document and verify the first signed installation: older installations without the update public key cannot gain automatic updates merely by merging this workflow. Explain the one-time install clearly. Check both a personal node and an employee node with company plugins preserved.
5. Add a release-readiness summary tying the immutable source, successful package/signature jobs and complete assets together before the owner publishes. Keep publication manual.

These are delivery tasks supporting the paired personal/employee journeys in `docs/PRODUCT_DIRECTION.md`. Larger conversation continuation, durable context and ordinary-employee workflows remain separate assignments; this release plumbing does not establish their acceptance. Ryncoin stays outside the near-term batch.
