from copy import deepcopy
from datetime import UTC, datetime

from test_friend_feed import publish, selected, setup

from rynmesh.friend_feed.weekly import weekly_recap

MONDAY = datetime(2026, 9, 14, tzinfo=UTC).timestamp()


def feed(rows, **kwargs):
    return {'peer_id': 'author', 'relationship_id': 'relation', 'node_name': 'Friend', 'checked_at': MONDAY,
            'next_cursor': '', 'error_code': '', 'rows': rows, **kwargs}


def row(identifier, stamp, revision=1):
    return {'id': identifier, 'published_at': stamp, 'revision': revision,
            'card': {'title': identifier, 'summary': 'private summary', 'source_url': 'private URL'}}


def test_week_starts_monday_utc_excludes_future_and_previous_week_and_deduplicates_versions():
    rows = [row('old', MONDAY - 1), row('monday', MONDAY), row('now', MONDAY + 3600), row('future', MONDAY + 3601)]
    result = weekly_recap([feed(rows), feed([row('monday', MONDAY, 2)])], now=MONDAY + 3600)
    assert [item['publication_id'] for item in result['items']] == ['now', 'monday']
    assert result['items'][1]['revision'] == 2
    assert result['week_start'] == '2026-09-14T00:00:00+00:00'
    assert result['timezone'] == 'UTC' and not result['incomplete']
    assert 'private summary' not in str(result) and 'private URL' not in str(result)
    next_week = weekly_recap([feed(rows)], now=MONDAY + 7 * 86400)
    assert next_week['items'] == []


def test_bounded_result_and_unconfirmed_pagination_do_not_claim_complete_recap():
    result = weekly_recap([feed([row(str(index), MONDAY + index) for index in range(101)])], now=MONDAY + 200)
    assert len(result['items']) == 100 and result['available_count'] == 101 and result['incomplete']
    result = weekly_recap([feed([row('one', MONDAY)], next_cursor='more', error_code='offline')], now=MONDAY)
    assert result['incomplete'] and result['items'][0]['access_unconfirmed']


def test_current_relationship_and_subscription_gate_each_projection_without_network_or_mutation(tmp_path):
    mesh, nodes, feeds, _, reference, _ = setup(tmp_path)
    author, bob = nodes[:2]
    a, b = feeds[:2]
    a.clock = b.clock = lambda: MONDAY + 60
    publication = publish(a, reference, selected(author, bob))
    rid = bob.store.relationship_for_peer(author.peer_id)['relationship_id']
    subscription = b.subscribe(rid, enabled=True, expected_revision=0)
    b.refresh(rid)
    before = deepcopy(b.store.read())
    mesh.online.clear()
    assert b.weekly()['items'][0]['publication_id'] == publication['id']
    assert b.store.read() == before
    b.subscribe(rid, enabled=False, expected_revision=subscription['revision'])
    assert b.weekly()['items'] == []
    # Restore the pre-unfollow cache: a revoked relationship still filters it.
    b.store.mutate(lambda data: data.update(before))
    bob.revoke(rid, notify=False)
    assert b.weekly()['items'] == []
