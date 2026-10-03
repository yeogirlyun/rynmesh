# Private friend week-to-date recap (#75)

The live For You briefing now includes Friends shared this week. The owner-only, no-store endpoint projects the authenticated friend-feed cache independently of the public digest. It uses Monday 00:00 UTC through the current node time, deduplicates publisher/publication identities at the latest revision, and returns at most 100 entries. Future timestamps and prior weeks are excluded. Unfollowed and locally revoked friendships disappear on the next read; missing pages and failed remote refreshes are disclosed. A local status error clears retained rows.

This is a deterministic local list, not a model summary. The public digest cache, scheduled recap email/PDF and model prompt paths do not consume it. Open friend updates leads to the existing current-access check before saving content. A remote permission change that has not reached this node is not claimed to be known.

Validation: 11 backend tests passed, including authenticated route denial, UTC boundaries, deduplication, bounds, no mutation/network during aggregation and unfollow/revocation filtering. Node 22 frontend: 326 tests passed across 53 files; TypeScript and production build passed; Ruff passed. No physical-device acceptance is claimed by these service/component tests.
