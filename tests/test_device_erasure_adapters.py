import pytest

from rynmesh.device_erasure.adapters import PHASES, CleanupAdapters


@pytest.mark.parametrize('category', list(PHASES))
def test_each_adapter_uses_real_durable_cleanup_and_replays_exact_review(tmp_path, category):
    if category == 'reading':
        from test_reading_cleanup import fixture
        cleanup = fixture(tmp_path)
    elif category == 'conversations':
        from test_conversation_cleanup import fixture
        cleanup, _ = fixture(tmp_path)
    elif category == 'documents':
        from test_library_cleanup import sample
        _, _, cleanup = sample(tmp_path)
    elif category == 'friend_feed':
        from test_friend_feed import publish, selected, setup

        from rynmesh.friend_feed.cleanup import FeedCleanup
        _, nodes, feeds, _, reference, _ = setup(tmp_path)
        publish(feeds[0], reference, selected(nodes[0], nodes[1]))
        cleanup = FeedCleanup(feeds[0].store)
    elif category == 'friend_cards':
        from test_friend_card_cleanup import seeded

        from rynmesh.friends.card_cleanup import CardCleanup
        alice, _, _ = seeded(tmp_path)
        cleanup = CardCleanup(alice.store)
    else:
        from test_offline_reading import download, fixture
        sample = fixture(tmp_path, images=False)
        download(sample)
        cleanup = sample.service.cleanup
    adapters = CleanupAdapters(None)
    adapters.target = lambda _: cleanup
    review = adapters.preview(category)
    assert 'review_token' in review and review['phases'] == PHASES[category]
    assert all(type(value) in {int, bool} for value in review['counts'].values())
    assert adapters.execute(category, review['review_token']) == PHASES[category]
    assert adapters.execute(category, review['review_token']) == PHASES[category]
