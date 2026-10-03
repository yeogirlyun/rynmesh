"""Real encrypted storage at the maximum live-pair cardinality."""
from copy import deepcopy

from test_device_sync_pairing import Devices

from rynmesh.crypto import canonical_json
from rynmesh.device_sync.diagnostic_budget import MAX_REJECTION_BYTES, bound_rejections
from rynmesh.device_sync.pair_store import MAX_PLAINTEXT, MAX_RECORDS
from rynmesh.device_sync.records import fingerprint
from rynmesh.device_sync.transfer import MAX_BATCH, OVERFLOW_LIMIT, DeviceTransfer


def test_all_pair_scopes_fit_the_shared_file_budget_and_unrelated_updates_continue(tmp_path):
    a, _, pair_id = Devices(tmp_path).pair()
    refusal = {}
    DeviceTransfer._rejected(refusal, 'reading', {fingerprint(str(index)): 'sync_device_limit'
                                               for index in range(MAX_BATCH + OVERFLOW_LIMIT)})
    entry = refusal['rejected']['reading']

    def fill(data):
        template = data['pairs'][pair_id]
        for index in range(MAX_RECORDS * 2):
            identifier = pair_id if index == 0 else fingerprint(f'pair-{index}')
            row = deepcopy(template)
            row['id'] = identifier
            if index >= MAX_RECORDS:
                row['status'] = 'revoked'
            row['transfer'] = {'rejected': {scope: deepcopy(entry) for scope in ['reading', 'bookmarks', 'conversations']}}
            data['pairs'][identifier] = row

    a.store.mutate(fill)
    state = a.store.snapshot()
    assert len(state['pairs']) == MAX_RECORDS * 2
    assert len(canonical_json(state)) < MAX_PLAINTEXT
    sections = [row['transfer']['rejected'] for row in state['pairs'].values()]
    assert sum(len(canonical_json(section)) for section in sections) <= MAX_REJECTION_BYTES
    for section in sections:
        for value in section.values():
            assert value['overflow_truncated'] is True
            assert value['count'] == len(value['rows']) + len(value.get('overflow', []))
    # A subsequent unrelated setting still commits and survives decrypt/reload.
    a.store.mutate(lambda data: data['pairs'][pair_id]['local_policy'].update(paused=True))
    assert a.store.snapshot()['pairs'][pair_id]['local_policy']['paused'] is True
    assert b'sync_device_limit' not in a.store.path.read_bytes()


def test_truncated_counts_never_claim_full_resolution_and_budget_adapts_to_core_state():
    state = {'pairs': {'pair': {'transfer': {'rejected': {'reading': {
        'count': 2, 'rows': {'a': 'sync_dot_conflict', 'b': 'sync_dot_conflict'}, 'overflow_truncated': True,
    }}}}}}
    transfer = state['pairs']['pair']['transfer']
    DeviceTransfer._rejected(transfer, 'reading', {'a': '', 'b': ''})
    assert transfer['rejected']['reading']['count'] == 0
    assert transfer['rejected']['reading']['overflow_truncated'] is True
    bound_rejections(state, max_plaintext=100)
    assert 'rejected' not in transfer
    assert transfer['rejected_truncated'] is True
