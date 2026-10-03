# Followed friend publications in For You (#61)

Based on v0.7.0 and upstream main f61c075. This is a review candidate, not a
release or a claim that the entire P2 milestone is complete.

## Behavior

The owner-only For You response combines public items with publications from
active, explicitly followed friends. Both enter the existing relevance,
recency, source preference, format balancing and Hide filters; friends are not
pinned above public sources. At most 20 recent cached publications per friend
and 500 overall enter this ranking. Friend updates remains the paginated view.

Cards show the sharing friend and serving node. Sharing provenance exposes the
authenticated peer identities, checked publication revision/time, expected
content hash and the source attribution supplied by the friend. It explicitly
distinguishes sharing identity from original authorship and content safety.

Opening a friend entry checks current access through the existing node-mediated
friend fetch, saves an independent copy and opens the existing content viewer.
It never opens the card's source URL directly. Offline and changed/withdrawn
publications use the existing safe error messages. Public viewer navigation
does not traverse private entries through its external-URL reader.

Unfollow and local revocation remove candidates on the next local projection.
Publisher withdrawal is learned on the existing authenticated feed refresh;
until then cached entries display their last check time, and opening checks
access again. The page refreshes its local slate every five seconds. Explicitly
saved copies remain available under the existing retention rules.

## Privacy boundaries

Private metadata stays in the existing encrypted friend-feed store and transient
owner response. The public digest cache, public recommendation adapter, automatic
AI enrichment, legacy Search & Ask, MCP digest and emailed recap retain their
public-only contract. A mixed slate omits the public brief because its numbered
references would refer to a different order; public per-item summaries remain.

Feedback stores opaque item/source identifiers and the label "Friend publication"
without copying the private title, body, peer identity or subject terms into the
plaintext preference history. Hide and Undo use the existing profile contract.
This changes no friend permissions, transport protocol or runtime dependency.

## Evidence (2026-09-17)

- Python 3.12.10 / Windows: 1562 backend tests passed, 29 skipped.
- Node 22.22.1: 327 frontend tests passed across 53 files; typecheck and build passed.
- Ruff and whitespace checks passed.
- `test_friend_for_you.py` covers cold start, mixed ranking, feedback/undo,
  restart, owner HTTP authentication, withdrawal/unfollow/revocation, offline
  provenance, damaged encrypted cache isolation, and absence of a planted
  private title in plaintext JSON, public views and model inputs.
- `Digest.friends.test.tsx` covers mixed rendering, provenance, saving through
  the node, failed access and Hide while retaining public entries.
- Built and installed a wheel into an isolated directory. The browser loaded
  its bundled UI from the node without Vite, displayed the mixed slate and
  inspectable provenance, then saved a copy and opened it in the real viewer.
  Fresh fixture identities used the existing authenticated/encrypted in-process
  test mesh. This browser check proves the packaged UI path, not cross-NAT
  connectivity. Screenshots were visually inspected during the test; repository
  evidence intentionally contains no message bodies or publication contents.

## Manual acceptance

1. Start two fresh nodes, pair them, and publish one saved item to the receiver.
2. On the receiver, follow the publisher. Open For You alongside a public source.
   Confirm both participate in one ordered slate and provenance names the correct
   sharing publisher and serving node.
3. Apply Less, Hide and Undo. Confirm public content remains usable.
4. Save a friend copy and open it. Confirm the normal viewer, saved-content list
   and private-copy designation; repeat after restarting the receiver.
5. Disconnect the publisher and refresh its feed. Confirm a last-checked entry
   and actionable failure on open. Reconnect and retry successfully.
6. Withdraw a publication, refresh, then unfollow or revoke. Verify the entry is
   removed and a prior saved copy remains. Check an old open request is denied
   after publisher withdrawal/revocation.
7. Inspect only counts and safe codes in exported acceptance evidence. Confirm
   private metadata never enters the public digest or automatic model prompts.

CI, maintainer review and physical-network acceptance are separate gates.
