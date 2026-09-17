"""Typed adapters reuse exact local review tokens and existing durable phases.

The proposal names a category. Only the receiving owner can review that node's
entire category and bind its immutable local plan. Arbitrary record IDs and paths
are never accepted from the requesting device.
"""
from ..friend_feed.cleanup import FeedCleanup
from ..friends.card_cleanup import CardCleanup
from ..offline_reading.cleanup import receipt
from ..services.library_cleanup import LibraryCleanup
from .store import ErasureError

PHASES = {
    'conversations': ['source', 'replica', 'backups', 'search', 'orders'],
    'reading': ['source', 'replica', 'backups', 'search'],
    'documents': ['source', 'files'],
    'friend_feed': ['source', 'backups'],
    'friend_cards': ['source', 'legacy_files'],
    'offline': ['metadata', 'files'],
}


class CleanupAdapters:
    def __init__(self, app):
        self.app = app

    def target(self, category):
        state = self.app.state
        if category == 'conversations':
            return state.conversation_cleanup.factory()
        if category == 'reading':
            return state.reading_cleanup.factory()
        if category == 'documents':
            return LibraryCleanup(state.friends.content.imports)
        if category == 'friend_feed':
            return FeedCleanup(state.friend_feed.service.store)
        if category == 'friend_cards':
            return CardCleanup(state.friends.service.store)
        if category == 'offline':
            return state.offline_reading.service.cleanup
        raise ErasureError('erasure_scope_unsupported')

    def preview(self, category):
        value = self.target(category).preview()
        return {'review_token': value['review_token'],
                'counts': {key: item for key, item in value.items() if type(item) in {int, bool}},
                'phases': PHASES[category]}

    def execute(self, category, token):
        target = self.target(category)
        if category == 'friend_cards':
            result = target.begin(token)
            complete = result.get('complete') is True
        elif category == 'offline':
            target.clear(review_token=token)
            result = receipt(target.store.read())
            complete = bool(result and result['review_token'] == token and result['done'] is True)
        else:
            result = target.begin(review_token=token)
            complete = result.get('id') == token and result.get('local_copies_complete') is True and result.get('done') == PHASES[category] and not result.get('cancelled')
        if not complete:
            raise ErasureError('erasure_local_incomplete')
        return list(PHASES[category])
